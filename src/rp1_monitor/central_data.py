"""Atomic key-curve projection, additive usage accounting and copy-only archives."""

import gzip
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from .central_tables import archive_manifests, daily_usage, key_windows, policies
from .models import metric_spec
from .storage import history, utc


def json_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def key_projection(window, policy):
    return {
        key: {
            **value.model_dump(mode="json"),
            "unit": metric_spec(key)["unit"],
            "aggregation": metric_spec(key)["aggregation"],
        }
        for key, value in window.metrics.items()
        if policy.selects(key)
    }


def envelope_matches(record):
    def stamp(value):
        return datetime.fromisoformat(value) if isinstance(value, str) else utc(value)

    payload = record["payload"]
    return all(
        payload[key] == record[key] for key in ("robot_id", "stream_id", "sequence")
    ) and all(stamp(payload[key]) == stamp(record[key]) for key in ("window_start", "window_end"))


def register_policy(connection, policy, now):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    builder = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
    connection.execute(
        builder(policies)
        .values(policy_sha256=policy.digest, created_at=now, payload=policy.model_dump())
        .on_conflict_do_nothing(index_elements=["policy_sha256"])
    )


def project_window(connection, window_id, window, policy, now, raw_payload=None):
    selected = key_projection(window, policy)
    key_size = len(json_bytes(selected))
    raw_size = len(
        json_bytes(raw_payload if raw_payload is not None else window.model_dump(mode="json"))
    )
    connection.execute(
        insert(key_windows).values(
            window_id=window_id,
            robot_id=window.robot_id,
            window_end=window.window_end,
            policy_sha256=policy.digest,
            metrics=selected,
            json_bytes=key_size,
        )
    )
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    condition = (daily_usage.c.robot_id == window.robot_id) & (daily_usage.c.received_day == day)
    # Ingest holds the robot row lock, so an absent daily row has a single creator.
    result = connection.execute(
        update(daily_usage)
        .where(condition)
        .values(
            windows=daily_usage.c.windows + 1,
            raw_json_bytes=daily_usage.c.raw_json_bytes + raw_size,
            key_json_bytes=daily_usage.c.key_json_bytes + key_size,
        )
    )
    if not result.rowcount:
        connection.execute(
            insert(daily_usage).values(
                robot_id=window.robot_id,
                received_day=day,
                windows=1,
                raw_json_bytes=raw_size,
                key_json_bytes=key_size,
            )
        )


def usage_report(store):
    with store._read_lock, store.engine.connect() as connection:
        rows = (
            connection.execute(
                select(daily_usage).order_by(
                    daily_usage.c.received_day.desc(), daily_usage.c.robot_id
                )
            )
            .mappings()
            .all()
        )
        if connection.dialect.name == "postgresql":
            physical = connection.exec_driver_sql(
                "SELECT pg_database_size(current_database())"
            ).scalar_one()
            scope = "PostgreSQL表/索引/TOAST；不含集群WAL、库外归档和备份"
        else:
            path = store.engine.url.database
            physical = (
                sum(
                    p.stat().st_size
                    for p in (Path(path), Path(path + "-wal"), Path(path + "-shm"))
                    if p.exists()
                )
                if path and path != ":memory:"
                else None
            )
            scope = "SQLite数据库及WAL/SHM；不含归档和备份"
    by_robot = {}
    for row in rows:
        item = by_robot.setdefault(
            row["robot_id"],
            {"robot_id": row["robot_id"], "windows": 0, "raw_json_bytes": 0, "key_json_bytes": 0},
        )
        for key in ("windows", "raw_json_bytes", "key_json_bytes"):
            item[key] += row[key]
    for item in by_robot.values():
        item["estimated_30day_1hz_json_bytes"] = {
            kind: round(item[kind + "_json_bytes"] / item["windows"] * 30 * 86400)
            if item["windows"]
            else None
            for kind in ("raw", "key")
        }
    return {
        "policy_sha256": store.policy.digest,
        "policy": store.policy.model_dump(),
        "items": list(by_robot.values()),
        "days": [{**row, "received_day": utc(row["received_day"]).isoformat()} for row in rows],
        "physical_bytes": physical,
        "physical_scope": scope,
        "central_disk": store.storage_mounts.observe()
        if getattr(store, "storage_mounts", None)
        else {
            "state": "unknown",
            "free_bytes": None,
            "detail": "PostgreSQL/归档实际挂载盘的剩余容量需部署后接入；数据库大小不能代表磁盘剩余量。",
        },
        "meaning": "按接收日累计去重窗口的逻辑JSON字节；raw与key均保留，不能相减当作实际节省。估算假定1Hz连续30天，排除索引/WAL/备份/归档。旧历史需显式回填统计。",
    }


def backfill_key_windows(store, robot_id):
    """Explicit additive replay of old raw windows; never rewrite already-projected rows."""
    from .models import TelemetryWindow
    from .storage import robots

    count, cursor = 0, 0
    with store._read_lock, store.engine.connect() as connection:
        high_water = (
            connection.execute(
                select(func.max(history.c.id)).where(history.c.robot_id == robot_id)
            ).scalar_one()
            or 0
        )
    while cursor < high_water:
        with store._lock, store.engine.begin() as connection:
            register_policy(connection, store.policy, datetime.now(timezone.utc))
            if (
                connection.execute(
                    select(robots.c.robot_id).where(robots.c.robot_id == robot_id).with_for_update()
                ).first()
                is None
            ):
                raise ValueError("机器人不存在")
            rows = (
                connection.execute(
                    select(history)
                    .outerjoin(key_windows, key_windows.c.window_id == history.c.id)
                    .where(
                        (history.c.robot_id == robot_id)
                        & (history.c.id > cursor)
                        & (history.c.id <= high_water)
                        & key_windows.c.window_id.is_(None)
                    )
                    .order_by(history.c.id)
                    .limit(100)
                )
                .mappings()
                .all()
            )
            if not rows:
                break
            for row in rows:
                expected = hashlib.sha256(
                    json.dumps(row["payload"], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if row["payload_hash"] != expected:
                    raise ValueError("原始摘要哈希不一致；当前回填批次回滚")
                if not envelope_matches(row):
                    raise ValueError("原始信封与数据库身份不一致；当前回填批次回滚")
                project_window(
                    connection,
                    row["id"],
                    TelemetryWindow.model_validate(row["payload"]),
                    store.policy,
                    utc(row["received_at"]),
                    raw_payload=row["payload"],
                )
                cursor = row["id"]
                count += 1
    return {
        "projected_windows": count,
        "high_water_id": high_water,
        "policy_sha256": store.policy.digest,
        "source_rewritten": False,
    }


def retention_preview(store, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=store.policy.retention_days)
    archived = (
        select(archive_manifests.c.archive_id)
        .where(
            (archive_manifests.c.robot_id == history.c.robot_id)
            & (history.c.id <= archive_manifests.c.high_water_id)
            & (history.c.window_end >= archive_manifests.c.window_start)
            & (history.c.window_end < archive_manifests.c.window_end)
        )
        .exists()
    )
    from sqlalchemy import case

    query = (
        select(
            history.c.robot_id,
            func.count().label("windows"),
            func.min(history.c.window_end).label("first_window"),
            func.max(history.c.window_end).label("last_window"),
            func.sum(case((archived, 1), else_=0)).label("archive_manifest_covered_windows"),
        )
        .where(history.c.window_end < cutoff)
        .group_by(history.c.robot_id)
    )
    with store._read_lock, store.engine.connect() as connection:
        rows = connection.execute(query).mappings().all()
    return {
        "dry_run": True,
        "deletion_enabled": False,
        "cutoff": cutoff.isoformat(),
        "retention_days": store.policy.retention_days,
        "items": [
            {
                **row,
                "first_window": utc(row["first_window"]).isoformat(),
                "last_window": utc(row["last_window"]).isoformat(),
            }
            for row in rows
        ],
        "meaning": "按窗口结束时间计算到期候选；时钟与归档文件仍需人工核对，清单覆盖不代表此刻文件可读。无删除接口。",
    }


def archive_range(store, robot_id, start, end, directory):
    if start.tzinfo is None or end.tzinfo is None or not 0 < (end - start).total_seconds() <= 86400:
        raise ValueError("归档范围须带时区、大于0且不超过24小时，按UTC小时分段")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with store._read_lock, store.engine.connect() as connection:
        high_water = (
            connection.execute(
                select(func.max(history.c.id)).where(history.c.robot_id == robot_id)
            ).scalar_one()
            or 0
        )
        policy_ids = select(key_windows.c.policy_sha256).where(
            (key_windows.c.robot_id == robot_id)
            & (key_windows.c.window_end >= start)
            & (key_windows.c.window_end < end)
            & (key_windows.c.window_id <= high_water)
        )
        policy_snapshots = {
            row["policy_sha256"]: row["payload"]
            for row in connection.execute(
                select(policies).where(policies.c.policy_sha256.in_(policy_ids))
            ).mappings()
        }
    manifests = []
    begin = start
    while begin < end:
        finish = min(end, begin.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        archive_id = str(uuid4())
        name = f"rp1-{archive_id}.jsonl.gz"
        path = directory / name
        count, cursor = 0, 0
        try:
            with path.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
                    compressed.write(
                        json_bytes(
                            {
                                "format": "rp1-monitor-archive-v1",
                                "robot_id": robot_id,
                                "start": begin.isoformat(),
                                "end": finish.isoformat(),
                                "high_water_id": high_water,
                                "policies": policy_snapshots,
                            }
                        )
                        + b"\n"
                    )
                    while True:
                        with store._read_lock, store.engine.connect() as connection:
                            rows = (
                                connection.execute(
                                    select(
                                        history,
                                        key_windows.c.metrics.label("key_metrics"),
                                        key_windows.c.policy_sha256,
                                    )
                                    .outerjoin(key_windows, history.c.id == key_windows.c.window_id)
                                    .where(
                                        (history.c.robot_id == robot_id)
                                        & (history.c.id > cursor)
                                        & (history.c.id <= high_water)
                                        & (history.c.window_end >= begin)
                                        & (history.c.window_end < finish)
                                    )
                                    .order_by(history.c.id)
                                    .limit(200)
                                )
                                .mappings()
                                .all()
                            )
                        if not rows:
                            break
                        for row in rows:
                            digest = hashlib.sha256(
                                json.dumps(
                                    row["payload"], sort_keys=True, separators=(",", ":")
                                ).encode()
                            ).hexdigest()
                            if digest != row["payload_hash"]:
                                raise ValueError("原始摘要哈希不一致，归档中止")
                            record = {
                                **row,
                                **{
                                    k: utc(row[k]).isoformat()
                                    for k in ("window_start", "window_end", "received_at")
                                },
                            }
                            compressed.write(json_bytes(record) + b"\n")
                            count += 1
                            cursor = row["id"]
                output.flush()
                os.fsync(output.fileno())
            if not count:
                path.unlink()  # Empty file created by this command only; no source data.
                begin = finish
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024**2), b""):
                    digest.update(chunk)
            manifest = dict(
                archive_id=archive_id,
                robot_id=robot_id,
                created_at=datetime.now(timezone.utc),
                window_start=begin,
                window_end=finish,
                high_water_id=high_water,
                windows=count,
                compressed_bytes=path.stat().st_size,
                sha256=digest.hexdigest(),
                file_name=name,
            )
            verify_archive(path, manifest)
            descriptor = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            with store._lock, store.engine.begin() as connection:
                connection.execute(insert(archive_manifests).values(**manifest))
            manifests.append(
                {
                    **manifest,
                    **{
                        k: manifest[k].isoformat()
                        for k in ("created_at", "window_start", "window_end")
                    },
                }
            )
        except Exception:
            # Keep incomplete output for inspection; never certify it with a manifest.
            raise
        begin = finish
    return {"items": manifests, "source_deleted": False, "high_water_id": high_water}


def verify_archive(path, manifest):
    """Verify checksum, gzip CRC, row identity/range and every original payload hash."""
    path = Path(path)
    manifest = dict(manifest)
    for key in ("window_start", "window_end"):
        if isinstance(manifest[key], str):
            manifest[key] = datetime.fromisoformat(manifest[key])
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024**2), b""):
            digest.update(chunk)
    if (
        digest.hexdigest() != manifest["sha256"]
        or path.stat().st_size != manifest["compressed_bytes"]
    ):
        raise ValueError("归档文件校验值或长度不一致")
    count, previous_id = 0, 0
    with gzip.open(path, "rb") as handle:
        header = json.loads(handle.readline())
        if (
            header["format"] != "rp1-monitor-archive-v1"
            or header["robot_id"] != manifest["robot_id"]
            or header["high_water_id"] != manifest["high_water_id"]
        ):
            raise ValueError("归档头与清单不一致")
        begin, finish = (datetime.fromisoformat(header[key]) for key in ("start", "end"))
        for policy_digest, policy in header.get("policies", {}).items():
            actual = hashlib.sha256(
                json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if actual != policy_digest:
                raise ValueError("归档策略快照哈希不一致")
        if begin != utc(manifest["window_start"]) or finish != utc(manifest["window_end"]):
            raise ValueError("归档时间范围不一致")
        for line in handle:
            record = json.loads(line)
            expected = hashlib.sha256(
                json.dumps(record["payload"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if (
                record["payload_hash"] != expected
                or record["robot_id"] != manifest["robot_id"]
                or not previous_id < record["id"] <= manifest["high_water_id"]
                or not begin <= datetime.fromisoformat(record["window_end"]) < finish
            ):
                raise ValueError("归档数据身份、顺序、时间或原始哈希不一致")
            if not envelope_matches(record):
                raise ValueError("归档原始信封与数据库身份不一致")
            if record.get("policy_sha256") and record["policy_sha256"] not in header["policies"]:
                raise ValueError("归档缺少对应策略快照")
            if record.get("policy_sha256"):
                from .central_policy import CentralPolicy
                from .models import TelemetryWindow

                projected = key_projection(
                    TelemetryWindow.model_validate(record["payload"]),
                    CentralPolicy.model_validate(header["policies"][record["policy_sha256"]]),
                )
                if projected != record["key_metrics"]:
                    raise ValueError("归档关键指标与原始摘要及策略不一致")
            previous_id = record["id"]
            count += 1
    if count != manifest["windows"]:
        raise ValueError("归档窗口数与清单不一致")
    return {"verified": True, "windows": count, "source_deleted": False}
