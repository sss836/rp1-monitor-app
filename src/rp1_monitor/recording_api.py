"""Shared read-only recording API and separate PC control routes."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from .models import MetricKey, RobotId
from .recordings import RecordingRequest, RecordingStore, recording_catalog, samples, sessions


class CurveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    robot_id: RobotId
    metrics: Annotated[list[MetricKey], Query(min_length=1, max_length=12)]
    recording_id: str | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    max_points: Annotated[int, Query(ge=50, le=2000)] = 800


class StopRecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    robot_id: RobotId | None = None


def read_router(store, read_auth, prefix="/api/v1/monitor-recordings"):
    history = RecordingStore(store)
    router = APIRouter(prefix=prefix, tags=["monitor-recordings"], dependencies=[read_auth])

    def ready():
        inspector = inspect(store.engine)
        return all(inspector.has_table(t.name) for t in (sessions, samples))

    @router.get("/context")
    def context(robot_id: RobotId | None = None) -> dict:
        recorder = getattr(store, "recorder", None)
        if recorder:
            if hasattr(recorder, "context"):
                return recorder.context(robot_id)
            frame = recorder.source()
            if robot_id and frame and frame.get("robot_id") != robot_id:
                frame = None
            state = recorder.state()
            if robot_id and (state.get("active") or {}).get("session", {}).get("robot_id") != robot_id:
                state = {**state, "active": None, "pending_samples": 0, "error": ""}
            return dict(
                **state,
                robot_id=robot_id or (frame.get("robot_id") if frame else None),
                source_kind=frame.get("source_kind") if frame else None,
                connected=bool(frame and frame.get("upload", {}).get("collector_connected")),
                catalog=recording_catalog(frame),
            )
        schema = ready()
        pg = store.engine.dialect.name == "postgresql"
        return dict(
            active=None,
            pending_samples=0,
            error="",
            storage="postgresql" if pg else "sqlite",
            schema_ready=schema,
            enabled=pg and schema,
            robot_id=None,
            source_kind=None,
            connected=False,
            catalog=[],
            control_status_unknown=True,
            history_database="PostgreSQL 历史数据查询" if pg else "模拟/旧版SQLite历史查询",
        )

    @router.get("")
    def listing(
        robot_id: RobotId | None = None,
        before: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 200,
    ) -> dict:
        if not ready():
            raise HTTPException(503, "监控记录迁移006尚未部署，请先审核数据库设计")
        try:
            rows = history.list(robot_id, limit, before)
            return {"items": rows, "next_cursor": rows[-1]["id"] if len(rows) == limit else None}
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @router.get("/curves")
    def curves(query: Annotated[CurveRequest, Query()]) -> dict:
        if query.start and query.end and query.start >= query.end:
            raise HTTPException(422, "开始时间必须早于结束时间")
        if not ready():
            raise HTTPException(503, "监控记录迁移006尚未部署，请先审核数据库设计")
        try:
            return history.curve(**query.model_dump())
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    return router


def register_recordings(app, recorder, source, reader, control):
    app.state.store.recorder = recorder
    # The native app uses this alias; the website uses monitor-recordings GET only.
    app.include_router(read_router(app.state.store, Depends(reader), "/api/v1/station/recordings"))
    router = APIRouter(
        prefix="/api/v1/station/recordings",
        tags=["pc-recording-controls"],
        dependencies=[Depends(reader), Depends(control)],
    )

    @router.post("", status_code=201)
    def start(query: RecordingRequest) -> dict:
        try:
            return recorder.start(query)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        except (SQLAlchemyError, OSError, sqlite3.Error):
            raise HTTPException(
                503, "PostgreSQL记录建立未确认，请检查连接、迁移006及权限"
            ) from None

    @router.post("/stop")
    def stop(query: StopRecordingRequest | None = None) -> dict:
        try:
            robot_id = query.robot_id if query else None
            if hasattr(recorder, "context"):
                recorder.stop(robot_id)
                state = recorder.state(robot_id)
            else:
                if robot_id and (recorder.state().get("active") or {}).get("session", {}).get("robot_id") != robot_id:
                    raise ValueError("所选机器人没有正在运行的监控记录")
                recorder.stop()
                state = recorder.state()
            return {"status": "stopped", **state}
        except ValueError as e:
            raise HTTPException(409, str(e)) from None

    app.include_router(router)
