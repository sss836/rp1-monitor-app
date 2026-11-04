"""Fixed factory routes for the existing PC relay; never proxy arbitrary paths."""

import tempfile
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .factory_client import FactoryClient, FactoryClientError
from .factory_models import FactorySegmentInfo, FactorySegmentManifest, FactorySegmentStart


def register_factory_proxy_routes(app, runtime, *, client_factory=FactoryClient):
    """Inherit the preview app's Origin guard and existing tunnel configuration."""
    router = APIRouter(prefix="/api/v1/factory/segments", tags=["factory-segments"])

    def call(operation):
        try:
            client = client_factory(runtime.collector_url)
            state = client.probe()
            runtime.check_identity(state["robot_id"], state["source_kind"])
            return operation(client)
        except FactoryClientError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, "工厂片段身份或状态不匹配；请核对绑定机器人") from exc
        except OSError as exc:
            raise HTTPException(507, "电脑临时下载失败；已有原始日志保留") from exc

    @router.get("")
    def segments() -> dict:
        return call(lambda client: client.list_segments())

    @router.post("")
    def begin(request: FactorySegmentStart) -> FactorySegmentInfo:
        return call(lambda client: client.start(request))

    @router.get("/{segment_id}")
    def info(segment_id: UUID) -> FactorySegmentInfo:
        return call(lambda client: client.get_segment(segment_id))

    @router.post("/{segment_id}/seal")
    def seal(segment_id: UUID) -> FactorySegmentInfo:
        return call(lambda client: client.seal(segment_id))

    @router.get("/{segment_id}/manifest")
    def manifest(segment_id: UUID) -> FactorySegmentManifest:
        return call(lambda client: client.manifest(segment_id))

    @router.get("/{segment_id}/data")
    def data(segment_id: UUID) -> FileResponse:
        temporary = None
        try:
            temporary = tempfile.TemporaryDirectory(prefix=".factory-proxy-", dir=runtime.settings.state_dir)
            _, path = call(lambda client: client.stage_segment(segment_id, temporary.name))
            return FileResponse(
                path, filename=f"{segment_id}.sqlite", media_type="application/vnd.sqlite3",
                headers={"Cache-Control": "no-store"},
                background=BackgroundTask(temporary.cleanup),
            )
        except OSError as exc:
            if temporary is not None:
                temporary.cleanup()
            raise HTTPException(507, "电脑临时下载失败；已有原始日志保留") from exc
        except BaseException:
            if temporary is not None:
                temporary.cleanup()
            raise

    app.include_router(router)
