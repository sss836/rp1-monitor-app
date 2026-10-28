"""Durable central diagnostic records, isolated from robot-side dependencies."""

import hashlib
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Table, Text, insert, select

from .diagnostics import canonical_bytes
from .storage import metadata

records = Table(
    "monitor_diagnostic_records",
    metadata,
    Column("report_id", Text, primary_key=True),
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("sha256", Text, nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_monitor_diagnostics_robot_time", records.c.robot_id, records.c.created_at)


def save_report(store, report):
    digest = hashlib.sha256(canonical_bytes(report)).hexdigest()
    with store._lock, store.engine.begin() as connection:
        connection.execute(
            insert(records).values(
                report_id=report["report_id"],
                robot_id=report["robot_id"],
                created_at=datetime.fromisoformat(report["created_at"]),
                sha256=digest,
                payload=report,
            )
        )
    return {"report_id": report["report_id"], "sha256": digest, "report": report}


def read_report(store, robot_id, report_id):
    with store._read_lock, store.engine.connect() as connection:
        row = (
            connection.execute(
                select(records).where(
                    (records.c.report_id == report_id) & (records.c.robot_id == robot_id)
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise LookupError("诊断记录不存在")
    if hashlib.sha256(canonical_bytes(row["payload"])).hexdigest() != row["sha256"]:
        raise ValueError("诊断记录校验失败")
    return {"report_id": report_id, "sha256": row["sha256"], "report": row["payload"]}


def save_board_report(store, report, actor):
    from .storage import IngestConflict, robots

    if (report["robot_id"], report["source_kind"]) != (actor["robot_id"], actor["source_kind"]):
        raise PermissionError("设备凭据只能上传绑定机器人的板端报告")
    digest = hashlib.sha256(canonical_bytes(report)).hexdigest()
    with store._lock, store.engine.begin() as connection:
        registered = (
            connection.execute(
                select(robots).where(robots.c.robot_id == actor["robot_id"]).with_for_update()
            )
            .mappings()
            .first()
        )
        if (
            not registered
            or not registered["enabled"]
            or registered["token_digest"] != actor["token_digest"]
            or registered["source_kind"] != actor["source_kind"]
        ):
            raise PermissionError("设备凭据已撤销或变更")
        previous = (
            connection.execute(select(records).where(records.c.report_id == report["report_id"]))
            .mappings()
            .first()
        )
        if previous:
            if previous["robot_id"] != actor["robot_id"] or previous["sha256"] != digest:
                raise IngestConflict("板端报告 ID 冲突，拒绝覆盖")
        else:
            connection.execute(
                insert(records).values(
                    report_id=report["report_id"],
                    robot_id=report["robot_id"],
                    created_at=datetime.fromisoformat(report["created_at"]),
                    sha256=digest,
                    payload=report,
                )
            )
    return {"report_id": report["report_id"], "sha256": digest}
