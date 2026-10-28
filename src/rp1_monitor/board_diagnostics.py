"""Durable board reports and explicit local-only passive check jobs."""

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict

from .diagnostics import canonical_bytes
from .robopi_tui import PASSIVE_PLUGINS, PLUGIN_GROUPS, BoardReport, TuiRunner


def summarize_board_reports(reports):
    """Keep each plugin's newest verified result; reports must be newest first."""
    checks = {
        identity: {
            "id": identity,
            "label": label,
            "group": PLUGIN_GROUPS[identity],
            "latest": None,
        }
        for identity, label in PASSIVE_PLUGINS.items()
    }
    remaining = set(checks)
    for item in reports:
        try:
            report = BoardReport.model_validate(item).model_dump(mode="json")
        except (ValueError, KeyError, TypeError, AttributeError):
            # Old protocols, unknown results and adapter failures are not passes.
            continue
        raw = report["board_diagnostics"]
        for row in raw["results"]:
            identity = row["id"]
            if identity not in remaining:
                continue
            checks[identity]["latest"] = {
                "report_id": report["report_id"],
                "robot_id": report["robot_id"],
                "created_at": report["created_at"],
                "source_kind": report["source_kind"],
                "validation_scope": report["validation_scope"],
                "tui_version": raw["tui_version"],
                "result": row["result"],
                "message": row["message"],
                "details": row.get("details", {}),
            }
            remaining.remove(identity)
        if not remaining:
            break
    return list(checks.values())


class BoardJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs (sequence INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL, payload TEXT NOT NULL, uploaded INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO meta VALUES ('journal_id', ?)", (str(uuid4()),))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def append(self, report, db=None):
        report = BoardReport.model_validate(report).model_dump(mode="json")
        content = canonical_bytes(report)
        digest = hashlib.sha256(content).hexdigest()
        if db is None:
            with self.connection() as connection:
                return self.append(report, connection)
        old = db.execute(
            "SELECT sha256 FROM runs WHERE report_id=?", (report["report_id"],)
        ).fetchone()
        if old and old[0] != digest:
            raise ValueError("板端报告 ID 冲突，拒绝覆盖原始记录")
        db.execute(
            "INSERT OR IGNORE INTO runs(report_id,sha256,payload) VALUES (?,?,?)",
            (report["report_id"], digest, content.decode()),
        )

    def page(self, after=0):
        with self.connection() as db:
            identity = db.execute("SELECT value FROM meta WHERE key='journal_id'").fetchone()[0]
            rows = db.execute(
                "SELECT sequence,payload,sha256 FROM runs WHERE sequence>? ORDER BY sequence LIMIT 2",
                (after,),
            ).fetchall()
        return {
            "journal_id": identity,
            "items": [{"sequence": n, "report": self.decode(p, digest)} for n, p, digest in rows],
        }

    @staticmethod
    def decode(payload, digest):
        report = json.loads(payload)
        if hashlib.sha256(canonical_bytes(report)).hexdigest() != digest:
            raise ValueError("本地板端报告校验失败，拒绝传输损坏内容")
        return report

    def latest(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT payload,sha256 FROM runs ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        return self.decode(*row) if row else None

    def checks(self):
        with self.connection() as db:
            rows = db.execute("SELECT payload,sha256 FROM runs ORDER BY sequence DESC")
            return summarize_board_reports(self.decode(*row) for row in rows)

    def cursor(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
        return json.loads(row[0]) if row else {"journal_id": None, "after": 0}

    def receive_page(self, page, robot_id, source_kind):
        from uuid import UUID

        UUID(page["journal_id"])
        cursor = self.cursor()
        if cursor["journal_id"] not in (None, page["journal_id"]):
            with self.connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('cursor', ?)",
                    (json.dumps({"journal_id": page["journal_id"], "after": 0}),),
                )
            return
        if not isinstance(page.get("items"), list) or len(page["items"]) > 2:
            raise ValueError("板端报告分页格式无效")
        with self.connection() as db:
            after = cursor["after"]
            for item in page["items"]:
                sequence = item["sequence"]
                report = BoardReport.model_validate(item["report"]).model_dump(mode="json")
                if type(sequence) is not int or sequence <= after:
                    raise ValueError("板端报告游标乱序")
                if (report["robot_id"], report["source_kind"]) != (robot_id, source_kind):
                    raise ValueError("板端报告与上位机绑定身份不一致")
                self.append(report, db)
                after = sequence
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('cursor', ?)",
                (json.dumps({"journal_id": page["journal_id"], "after": after}),),
            )

    def pending(self):
        with self.connection() as db:
            return [
                self.decode(*row)
                for row in db.execute(
                    "SELECT payload,sha256 FROM runs WHERE uploaded=0 ORDER BY sequence LIMIT 2"
                )
            ]

    def acknowledge(self, report_id, digest):
        with self.connection() as db:
            row = db.execute("SELECT sha256 FROM runs WHERE report_id=?", (report_id,)).fetchone()
            if row is None or row[0] != digest:
                raise ValueError("中央报告确认与本地原始数据不一致")
            db.execute("UPDATE runs SET uploaded=1 WHERE report_id=?", (report_id,))


class BoardDiagnostics:
    def __init__(self, settings):
        self.settings = settings
        self.journal = BoardJournal(settings.state_dir / "board-diagnostics.sqlite")
        self.runner = (
            TuiRunner(settings.robopi_tui_executor) if settings.robopi_tui_executor else None
        )
        self.lock = threading.Lock()
        self.job = None
        self.thread = None
        self.last_started = -float("inf")

    def status(self):
        with self.lock:
            job = dict(self.job) if self.job else None
            cooldown = math.ceil(max(0, 10 - (time.monotonic() - self.last_started)))
        return {
            "robot_id": self.settings.robot_id,
            "source_kind": self.settings.source_kind,
            "enabled": self.runner is not None,
            "job": job,
            "cooldown_remaining_seconds": cooldown,
            "items": [{"id": key, "label": value} for key, value in PASSIVE_PLUGINS.items()],
            "latest": self.journal.latest(),
            "checks": self.journal.checks(),
            "detail": "仅按需运行指定被动检查；不会更新实时曲线或健康阈值"
            if self.runner
            else "未启用 robopi_TUI；需配置执行器路径，不会自动安装或升级依赖",
        }

    def start(self, plugin_id):
        if plugin_id not in PASSIVE_PLUGINS:
            raise HTTPException(422, "仅允许指定的被动检查")
        if self.runner is None:
            raise HTTPException(503, "未配置 robopi_TUI 执行器")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise HTTPException(409, "已有板端检查运行中")
            if time.monotonic() - self.last_started < 10:
                raise HTTPException(
                    429, "两次板端检查至少间隔 10 秒", headers={"Retry-After": "10"}
                )
            self.last_started = time.monotonic()
            self.job = {
                "job_id": str(uuid4()),
                "plugin_id": plugin_id,
                "state": "running",
                "error": "",
            }
            self.thread = threading.Thread(
                target=self._run, args=(plugin_id,), name="robopi-tui-check", daemon=True
            )
            self.thread.start()
            return dict(self.job)

    def _run(self, plugin_id):
        try:
            raw = self.runner.run(plugin_id)
            report = BoardReport(
                report_id=raw["run_id"],
                robot_id=self.settings.robot_id,
                source_kind=self.settings.source_kind,
                created_at=raw["finished_at"],
                validation_scope="simulated"
                if self.settings.source_kind == "simulated"
                else "software_observation_not_hardware_acceptance",
                board_diagnostics=raw,
            ).model_dump(mode="json")
            self.journal.append(report)
            with self.lock:
                self.job.update(state="completed", report_id=report["report_id"])
        except (ValueError, OSError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
            with self.lock:
                self.job.update(state="error", error=f"板端检查未完成：{str(exc)[:180]}")

    def stop(self):
        if self.runner:
            self.runner.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)


class BoardRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    robot_id: str
    plugin_id: str


def register_board_routes(app, runtime):
    @app.get("/api/v1/board-diagnostics")
    def board_status() -> dict:
        if getattr(runtime, "role", "") == "upper_computer":
            return runtime.board_status()
        board = getattr(runtime, "board_diagnostics", None)
        return (
            board.status()
            if board
            else {
                "enabled": False,
                "items": [],
                "latest": None,
                "checks": summarize_board_reports(()),
                "detail": "采集版本未启用板端自检",
            }
        )

    @app.post("/api/v1/board-diagnostics/runs", status_code=202)
    def board_run(body: BoardRunRequest) -> dict:
        if body.robot_id != runtime.settings.robot_id or body.plugin_id not in PASSIVE_PLUGINS:
            raise HTTPException(422, "机器人身份或被动检查编号无效")
        if getattr(runtime, "role", "") == "upper_computer":
            return runtime.start_board_run(body.model_dump())
        board = getattr(runtime, "board_diagnostics", None)
        if board is None:
            raise HTTPException(503, "采集版本未启用板端自检")
        return board.start(body.plugin_id)

    @app.get("/api/v1/board-diagnostics/reports")
    def board_reports(after: int = Query(default=0, ge=0)) -> dict:
        board = getattr(runtime, "board_diagnostics", None)
        if board is None:
            raise HTTPException(503, "该采集端不提供板端报告")
        return board.journal.page(after)
