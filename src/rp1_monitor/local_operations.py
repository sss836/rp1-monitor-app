"""Loopback robot diagnostics with a durable, credential-free local journal."""

import hashlib
import importlib.metadata
import importlib.util
import json
import shutil
import sqlite3
from contextlib import contextmanager

from fastapi import HTTPException, Request, Response

from .capabilities import evaluate_capabilities
from .diagnostics import build_report, canonical_bytes, export_bundle


def register_local_operations(app, runtime):
    from .board_diagnostics import register_board_routes

    register_board_routes(app, runtime)
    path = runtime.settings.state_dir / "diagnostics.sqlite"

    @contextmanager
    def journal():
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS reports (report_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, sha256 TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def report():
        snapshot = runtime.snapshot().model_dump(mode="json")
        result = build_report(snapshot, runtime.profile)
        available = {
            "python_packages": {},
            "free_disk_bytes": shutil.disk_usage(runtime.settings.state_dir).free,
        }
        distribution = "rp1-monitor-robot" if __package__ == "rp1_robot" else "rp1-monitor"
        if distribution == "rp1-monitor":
            try:
                importlib.metadata.version("rp1-monitor-pc")
                distribution = "rp1-monitor-pc"
            except importlib.metadata.PackageNotFoundError:
                pass
        for package in (distribution, "pydantic", "fastapi", "httpx"):
            try:
                available["python_packages"][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                available["python_packages"][package] = "unavailable"
        available["rclpy_module_discoverable"] = importlib.util.find_spec("rclpy") is not None
        source = getattr(runtime, "source", None)
        available["ros_observer_active"] = hasattr(source, "node")
        available["scope"] = (
            "上位机转发进程所在电脑；本机软件和磁盘检查不代表机器人环境"
            if getattr(runtime, "role", "") == "upper_computer"
            else "采集进程所在主机；模拟模式不代表机器人环境"
        )
        if hasattr(source, "diagnostics"):
            try:
                available["ros_graph"] = source.diagnostics()
            except RuntimeError:
                available["ros_graph"] = {"state": "unavailable", "detail": "ROS上下文当前不可用"}
        reserve = getattr(runtime.settings, "reserve_bytes", 256 * 1024**2)
        result["checks"].append(
            {
                "key": "disk",
                "result": "pass" if available["free_disk_bytes"] >= reserve else "attention",
                "detail": f"当前主机可用空间 {available['free_disk_bytes']} 字节，预留 {reserve} 字节",
                "next_step": "空间不足时归档或扩容；自检不会删除原始数据",
            }
        )
        result["environment"] = available
        return result

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        if request.url.path.startswith(("/api/v1/",)) and request.headers.get("origin"):
            return Response("只允许本机原生客户端或SSH隧道访问", status_code=403)
        return await call_next(request)

    @app.get("/api/v1/capabilities")
    def capabilities() -> dict:
        snapshot = runtime.snapshot().model_dump(mode="json")
        return {
            "profile": runtime.profile.model_dump(),
            "items": evaluate_capabilities(snapshot, runtime.profile),
        }

    @app.get("/api/v1/doctor")
    def doctor() -> dict:
        return report()

    @app.post("/api/v1/diagnostics", status_code=201)
    def create_report() -> dict:
        data = report()
        content = canonical_bytes(data)
        digest = hashlib.sha256(content).hexdigest()
        with journal() as connection:
            connection.execute(
                "INSERT INTO reports VALUES (?,?,?,?)",
                (data["report_id"], data["created_at"], digest, content.decode()),
            )
        return {"report_id": data["report_id"], "sha256": digest, "report": data}

    @app.get("/api/v1/diagnostics")
    def list_reports() -> dict:
        with journal() as connection:
            rows = connection.execute(
                "SELECT report_id,created_at,sha256 FROM reports ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        return {"items": [dict(zip(("report_id", "created_at", "sha256"), row)) for row in rows]}

    @app.get("/api/v1/diagnostics/{report_id}/bundle")
    def bundle(report_id: str) -> Response:
        with journal() as connection:
            row = connection.execute(
                "SELECT payload,sha256 FROM reports WHERE report_id=?", (report_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "诊断记录不存在")
        data = json.loads(row[0])
        if hashlib.sha256(canonical_bytes(data)).hexdigest() != row[1]:
            raise HTTPException(409, "诊断记录校验失败")
        return Response(
            export_bundle({"report": data}),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="rp1-diagnostics-{data["report_id"]}.zip"',
                "Cache-Control": "no-store",
            },
        )
