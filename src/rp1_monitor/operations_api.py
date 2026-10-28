"""Authenticated monitoring operations; no robot actuation or arbitrary commands."""

import hashlib
from datetime import datetime
from threading import BoundedSemaphore
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi import Path as PathParameter
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .assistant import ReadOnlyAssistant
from .board_diagnostics import summarize_board_reports
from .diagnostics import canonical_bytes, checks_for, export_bundle
from .model_providers import ModelConnection, ModelError
from .models import MetricKey
from .monitor_service import MonitorService
from .report_store import read_report, records


class EvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metrics: list[MetricKey] = Field(default_factory=list, max_length=8)
    start: datetime | None = None
    end: datetime | None = None


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class AssistantQuery(EvidenceQuery):
    question: str = Field(min_length=1, max_length=2000)
    use_model: bool = True
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)
    connection: ModelConnection | None = None


class PrivateDiagnosticsRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                # FastAPI's default error includes raw input, which may contain API keys.
                raise HTTPException(422, "诊断请求无效，请核对字段、长度与查询范围") from exc

        return handle


def operations_router(service: MonitorService, read_auth, assistant=None):
    router = APIRouter(
        tags=["diagnostics"], dependencies=[read_auth], route_class=PrivateDiagnosticsRoute
    )
    assistant = assistant or ReadOnlyAssistant(service)
    ai_gate = BoundedSemaphore(2)

    def scope(robot_id):
        try:
            return service.snapshot(robot_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/robot-monitors/{robot_id}/capabilities")
    def capabilities(robot_id: str) -> dict:
        scope(robot_id)
        return service.capabilities(robot_id)

    @router.get("/robot-monitors/{robot_id}/joints/{joint}/temperature")
    def joint_temperature(
        robot_id: str,
        joint: Annotated[int, PathParameter(ge=0, le=63)],
        minutes: Annotated[int, Query(ge=1, le=1440)] = 10,
    ) -> dict:
        try:
            return service.joint_temperature(robot_id, joint, minutes)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/robot-monitors/{robot_id}/doctor")
    def doctor(robot_id: str) -> dict:
        snapshot = scope(robot_id)
        return {
            "robot_id": robot_id,
            "checks": checks_for(snapshot, service.profile(snapshot)),
            "scope": "已上报遥测及档案；中央主机环境不能代替机器人环境",
        }

    @router.get("/robot-monitors/{robot_id}/board-diagnostics")
    def board_reports(robot_id: str) -> dict:
        scope(robot_id)
        latest = None
        with service.store._read_lock, service.store.engine.connect() as connection:
            # Stream the existing evidence so infrequently run checks survive newer runs.
            rows = (
                connection.execution_options(yield_per=16)
                .execute(
                    select(records.c.report_id, records.c.payload, records.c.sha256)
                    .where(
                        (records.c.robot_id == robot_id)
                        & (records.c.payload["report_type"].as_string() == "robopi_tui")
                    )
                    .order_by(records.c.created_at.desc(), records.c.report_id.desc())
                )
                .mappings()
            )

            def verified_reports():
                nonlocal latest
                for row in rows:
                    report = row["payload"]
                    if (
                        not isinstance(report, dict)
                        or hashlib.sha256(canonical_bytes(report)).hexdigest() != row["sha256"]
                        or report.get("robot_id") != robot_id
                        or report.get("report_id") != row["report_id"]
                    ):
                        raise HTTPException(409, "板端诊断记录身份或内容校验失败")
                    if latest is None:
                        latest = report
                    yield report

            checks = summarize_board_reports(verified_reports())
        return {
            "robot_id": robot_id,
            "latest": latest,
            "checks": checks,
            "detail": "历史板端自检报告；网页只读。请在本机上位机中发起新的指定被动检查。",
        }

    @router.get("/robot-monitors/{robot_id}/diagnostics")
    def list_reports(robot_id: str, limit: Annotated[int, Query(ge=1, le=100)] = 30) -> dict:
        scope(robot_id)
        with service.store._read_lock, service.store.engine.connect() as connection:
            rows = connection.execute(
                select(records.c.report_id, records.c.created_at, records.c.sha256)
                .where(records.c.robot_id == robot_id)
                .order_by(records.c.created_at.desc())
                .limit(limit)
            ).mappings()
            return {"items": [dict(row) for row in rows]}

    @router.post("/robot-monitors/{robot_id}/diagnostics", status_code=201)
    def create_report(robot_id: str, query: EvidenceQuery) -> dict:
        scope(robot_id)
        try:
            return service.record(robot_id, query.metrics, query.start, query.end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def saved_report(robot_id, report_id):
        scope(robot_id)
        try:
            return read_report(service.store, robot_id, report_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/robot-monitors/{robot_id}/diagnostics/{report_id}")
    def report(robot_id: str, report_id: str) -> dict:
        return saved_report(robot_id, report_id)

    @router.get("/robot-monitors/{robot_id}/diagnostics/{report_id}/bundle")
    def bundle(robot_id: str, report_id: str) -> Response:
        saved = saved_report(robot_id, report_id)
        return Response(
            export_bundle(saved),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="rp1-diagnostics-{saved["report_id"]}.zip"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/robot-monitors/{robot_id}/assistant")
    def ask(robot_id: str, query: AssistantQuery) -> dict:
        scope(robot_id)
        if not ai_gate.acquire(blocking=False):
            raise HTTPException(429, "当前诊断查询较多，请稍后重试", headers={"Retry-After": "5"})
        try:
            return assistant.ask(robot_id, query)
        except ModelError as exc:
            raise HTTPException(422, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "模型服务请求失败；证据与自检仍可单独查询") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "查询范围或模型响应无效；请检查时间、指标与模型配置") from exc
        finally:
            ai_gate.release()

    @router.get("/assistant/models")
    def models() -> dict:
        return assistant.registry.catalog()

    @router.post("/assistant/test")
    def probe(connection: ModelConnection) -> dict:
        if not ai_gate.acquire(blocking=False):
            raise HTTPException(429, "当前模型查询较多，请稍后重试")
        try:
            return assistant.probe(connection)
        except ModelError as exc:
            raise HTTPException(422, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, "模型服务请求失败，请核对服务地址与 API 协议") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "模型测试响应无效") from exc
        finally:
            ai_gate.release()

    return router
