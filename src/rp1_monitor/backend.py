"""Standalone central API; router can be mounted with existing platform read auth."""

import hmac
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from .central_data import retention_preview, usage_report
from .central_policy import load_central_policy
from .models import BatchAck, DeviceRegistration, MetricKey, RelayStatus, RobotId, TelemetryBatch
from .monitor_alerts import AlertMonitor, accept_relay_status
from .monitor_service import MonitorService
from .operations_api import operations_router
from .profiles import RobotProfile
from .storage import IngestConflict, Store, token_digest


@dataclass
class BackendSettings:
    database_url: str
    device_config: Path
    viewer_token: str = field(default="", repr=False)
    demo_read: bool = False
    initialize: bool = True
    profiles: dict = field(default_factory=dict)
    policy_path: Path | None = None
    admin_config: Path | None = None
    database_managed: bool = False
    storage_mount_config: Path | None = None
    motor_map_path: Path | None = None


def make_router(store: Store, settings: BackendSettings, read_dependency=None):
    monitor = AlertMonitor(store)
    store.alert_monitor = monitor

    def viewer(authorization: Annotated[str | None, Header()] = None):
        if settings.demo_read:
            return
        provided = (authorization or "").removeprefix("Bearer ")
        if not settings.viewer_token or not hmac.compare_digest(provided, settings.viewer_token):
            raise HTTPException(401, "需要只读访问凭据")

    def device(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "需要设备凭据")
        found = store.device_for_digest(token_digest(authorization[7:]))
        if found is None:
            raise HTTPException(401, "设备凭据无效")
        return found

    router = APIRouter(prefix="/api/v1", tags=["robot-monitoring"])
    read_auth = Depends(read_dependency or viewer)

    @router.post("/robot-telemetry/batches", response_model=BatchAck)
    async def ingest(request: Request, actor: Annotated[dict, Depends(device)]) -> BatchAck:
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "遥测批次必须使用application/json")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise HTTPException(413, "遥测批次超过1MiB，请拆分")
        try:
            batch = TelemetryBatch.model_validate_json(body)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_input=False)) from exc
        try:
            result = await run_in_threadpool(store.ingest, batch, actor)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except IngestConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return BatchAck.model_validate(result)

    @router.get("/robot-monitors", dependencies=[read_auth])
    def monitors() -> dict:
        return store.snapshots()

    @router.post("/robot-telemetry/board-diagnostics")
    async def ingest_board_report(
        request: Request, actor: Annotated[dict, Depends(device)]
    ) -> dict:
        from .report_store import save_board_report
        from .robopi_tui import MAX_REPORT_BYTES, BoardReport

        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "板端报告必须使用 application/json")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REPORT_BYTES * 2:
                raise HTTPException(413, "板端报告封装超过 512 KiB")
        try:
            report = BoardReport.model_validate_json(body).model_dump(mode="json")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(422, "板端报告协议、插件或验证范围无效") from exc
        try:
            return await run_in_threadpool(save_board_report, store, report, actor)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except IngestConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/robot-telemetry/configuration")
    def relay_configuration(actor: Annotated[dict, Depends(device)]) -> dict:
        return store.effective_monitoring_configuration(actor["robot_id"], actor["source_kind"])

    @router.get("/robot-monitors/{robot_id}/configuration", dependencies=[read_auth])
    def monitoring_configuration(robot_id: str) -> dict:
        try:
            return store.effective_monitoring_configuration(robot_id)
        except KeyError:
            raise HTTPException(404, "机器人不存在") from None

    @router.post("/robot-telemetry/relay-status")
    def relay_observation(message: RelayStatus, actor: Annotated[dict, Depends(device)]) -> dict:
        try:
            return accept_relay_status(store, message, actor)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/monitor-storage/usage", dependencies=[read_auth])
    def storage_usage() -> dict:
        return usage_report(store)

    @router.get("/monitor-storage/retention-preview", dependencies=[read_auth])
    def expiry_preview() -> dict:
        return retention_preview(store)

    def require_robot(robot_id):
        if not any(item["robot_id"] == robot_id for item in store.snapshots()["items"]):
            raise HTTPException(404, "机器人不存在")

    @router.get("/robot-monitors/{robot_id}/alerts", dependencies=[read_auth])
    def current_alerts(robot_id: str) -> dict:
        require_robot(robot_id)
        return monitor.current(robot_id)

    @router.get("/robot-monitors/{robot_id}/alert-events", dependencies=[read_auth])
    def alert_history(
        robot_id: str,
        before_id: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict:
        require_robot(robot_id)
        return monitor.events(robot_id, before_id, limit)

    @router.get("/robot-monitors/{robot_id}/telemetry", dependencies=[read_auth])
    def telemetry(
        robot_id: str,
        metric: MetricKey,
        start: datetime | None = None,
        end: datetime | None = None,
        max_points: Annotated[int, Query(ge=10, le=4000)] = 1200,
        tier: Literal["raw", "key"] = "raw",
    ) -> dict:
        end = end or datetime.now(timezone.utc)
        start = start or end - timedelta(minutes=10)
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or not 0 < (end - start).total_seconds() <= 31 * 86400
        ):
            raise HTTPException(422, "时间必须带时区，查询范围大于0且不超过31天")
        return store.curves(robot_id, metric, start, end, max_points, tier=tier)

    @router.get("/robot-monitors/{robot_id}/component-history", dependencies=[read_auth])
    def component_history(
        robot_id: RobotId,
        component: Literal["battery", "joints", "host"],
        joint: Annotated[int, Query(ge=0, le=63)] = 0,
        thermal: MetricKey | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        max_points: Annotated[int, Query(ge=10, le=4000)] = 300,
    ) -> dict:
        from .component_history import component_history as build_component_history

        end = end or datetime.now(timezone.utc)
        start = start or end - timedelta(minutes=10)
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or not 0 < (end - start).total_seconds() <= 31 * 86400
        ):
            raise HTTPException(422, "时间必须带时区，查询范围大于0且不超过31天")
        try:
            return build_component_history(
                store, robot_id, component, joint, start, end, max_points, thermal=thermal
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    from .recording_api import read_router

    router.include_router(read_router(store, read_auth, "/monitor-recordings"))
    router.include_router(operations_router(MonitorService(store, settings.profiles), read_auth))
    return router


def create_app(settings: BackendSettings, read_dependency=None):
    raw_devices = json.loads(settings.device_config.read_text())
    managed = settings.database_managed or settings.admin_config is not None
    if not isinstance(raw_devices, list) or (not raw_devices and not managed):
        raise ValueError("必须通过设备配置注册至少一个机器人")
    devices = []
    for raw in raw_devices:
        device = DeviceRegistration.model_validate(raw)
        if device.profile is not None:
            profile = RobotProfile.model_validate(device.profile)
            if profile.robot_id != device.robot_id:
                raise ValueError("设备档案与登记ID不一致")
            settings.profiles[device.robot_id] = profile.model_dump()
        devices.append(
            {**device.model_dump(exclude={"token"}), "token": device.token.get_secret_value()}
        )
    if len({device["robot_id"] for device in devices}) != len(devices) or len(
        {device["token"] for device in devices}
    ) != len(devices):
        raise ValueError("机器人ID和设备凭据必须分别唯一")
    if settings.demo_read and any(device["source_kind"] != "simulated" for device in devices):
        raise ValueError("匿名演示仅允许模拟设备")
    if not settings.demo_read and read_dependency is None and not settings.viewer_token:
        raise ValueError("非演示服务必须配置只读凭据或平台登录依赖")
    if settings.viewer_token and (
        len(settings.viewer_token) < 32
        or any(device["token"] == settings.viewer_token for device in devices)
    ):
        raise ValueError("只读凭据至少32字符，且不能与设备写入凭据相同")
    store = Store(
        settings.database_url,
        initialize=settings.initialize,
        policy=load_central_policy(settings.policy_path),
    )
    from .motor_map import MotorMapConfig

    store.motor_map_config = MotorMapConfig.load(settings.motor_map_path)
    if settings.demo_read:
        from sqlalchemy import select

        from .storage import robots

        with store.engine.connect() as connection:
            if connection.execute(
                select(robots.c.robot_id).where(robots.c.source_kind != "simulated").limit(1)
            ).first():
                store.engine.dispose()
                raise ValueError("匿名演示禁止复用包含ROS2来源历史的数据库；请使用独立演示库")
    store.configure(devices, authoritative=not managed)
    if settings.storage_mount_config:
        from .storage_mounts import StorageMounts

        store.storage_mounts = StorageMounts.load(settings.storage_mount_config, store.engine.url)
    administration = None
    if settings.admin_config is not None:
        from .admin import Administration

        administration = Administration(settings.admin_config, store, settings.demo_read)

    @asynccontextmanager
    async def lifespan(_):
        store.alert_monitor.start()
        try:
            yield
        finally:
            store.alert_monitor.stop()
            if administration is not None:
                administration.close()
            store.engine.dispose()

    app = FastAPI(title="RP1 Whole Robot Monitor", version="0.4.0", lifespan=lifespan)
    app.state.store = store
    app.include_router(make_router(store, settings, read_dependency))
    if administration is not None:
        app.include_router(administration.router())
        app.state.administration = administration

    @app.middleware("http")
    async def management_headers(request, call_next):
        response = await call_next(request)
        if "/api/v1/admin" in request.url.path or request.url.path.endswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(_, exc):
        # Validation responses never echo password/key input or server error contexts.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
        )

    @app.exception_handler(OperationalError)
    async def database_unavailable(_, _exc):
        return JSONResponse(status_code=503, content={"detail": "监控数据库暂不可用，请重试"})

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": "0.4.0",
            "demo": settings.demo_read,
            "management": administration is not None,
        }

    web = Path(__file__).parent / "web"
    app.mount("/assets", StaticFiles(directory=web, check_dir=False), name="assets")

    def render_shell(request: Request) -> HTMLResponse:
        # The app may be mounted below a platform prefix. Keep asset/API links
        # anchored there even when the browser directly opens a nested workspace.
        prefix = request.scope.get("root_path", "").strip("/")
        base = escape(f"/{prefix}/" if prefix else "/", quote=True)
        content = (web / "index.html").read_text()
        content = content.replace(
            "<!-- RECORDINGS_PANEL -->", (web / "recordings-panel.html").read_text(), 1
        )
        base_tag = f'<base href="{base}">'
        if "<!-- APP_BASE -->" in content:
            content = content.replace("<!-- APP_BASE -->", base_tag, 1)
        else:
            content = content.replace("<head>", f"<head>\n  {base_tag}", 1)
        if administration is not None:
            content = content.replace(
                "<!-- ADMIN_LINK -->", '<a class="admin-link" href="./admin">设备管理 ↗</a>'
            )
        return HTMLResponse(content, headers={"Cache-Control": "no-store"})

    @app.get("/recordings", include_in_schema=False)
    def monitor_recordings_page(request: Request):
        return render_shell(request)

    @app.get("/", include_in_schema=False)
    @app.get("/robots", include_in_schema=False)
    def dashboard(request: Request):
        return render_shell(request)

    @app.get("/robots/{robot_id}/{view}", include_in_schema=False)
    def robot_workspace(request: Request, robot_id: RobotId, view: str):
        if view not in {
            "overview",
            "temperature",
            "history",
            "processes",
            "storage",
            "diagnostics",
            "battery",
            "joints",
            "host",
            "recordings",
        }:
            raise HTTPException(404, "监控页面不存在")
        # This is a nonsecret shell, not an existence lookup. All robot data
        # continues to be fetched through the existing authenticated API.
        return render_shell(request)

    if administration is not None:

        @app.get("/admin", include_in_schema=False)
        def management_page():
            return FileResponse(web / "admin.html")

    return app
