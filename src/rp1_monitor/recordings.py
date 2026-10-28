"""PC monitoring recordings, separate from formal tests and from robot control."""

import hashlib
import json
import math
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    case,
    cast,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from .models import MetricKey, PreviewFrame, RobotId, metric_max_age_ms, metric_spec
from .storage import JSON_DOCUMENT, metadata, robots, utc

sessions = Table(
    "monitor_recordings",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_number", Text, nullable=False, unique=True),
    Column("robot_id", Text, ForeignKey(robots.c.robot_id), nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("case_number", Text, nullable=False),
    Column("robot_number", Text, nullable=False),
    Column("site", Text, nullable=False),
    Column("frequency_hz", Float, nullable=False),
    Column("metrics", JSON_DOCUMENT, nullable=False),
    Column("source_context", JSON_DOCUMENT, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    CheckConstraint("source_kind IN ('simulated','ros2')", name="ck_recording_source"),
    CheckConstraint("frequency_hz >= 0.001 AND frequency_hz <= 10", name="ck_recording_hz"),
)
Index("ix_recording_robot_time", sessions.c.robot_id, sessions.c.started_at)
samples = Table(
    "monitor_recording_samples",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("recording_id", Text, ForeignKey(sessions.c.id), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("payload_hash", Text, nullable=False),
    UniqueConstraint("recording_id", "sequence", name="uq_recording_sample"),
    CheckConstraint("sequence > 0", name="ck_recording_sequence"),
    CheckConstraint("length(payload_hash) = 64", name="ck_recording_hash"),
)
Index("ix_recording_sample_time", samples.c.recording_id, samples.c.observed_at)


class RecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    robot_id: RobotId
    case_number: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
    robot_number: RobotId
    site: str = Field(min_length=1, max_length=40, pattern=r"^[\w\u4e00-\u9fff.-]+$")
    frequency_hz: float = Field(default=1, ge=0.001, le=10)
    metrics: list[MetricKey] = Field(min_length=1, max_length=256)

    @field_validator("metrics")
    @classmethod
    def distinct(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("保存指标不能重复")
        return value


def run_number(query, now):
    stamp = now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%y%m%d%H%M")
    return f"{query.case_number}_{stamp}_{query.robot_number}_{query.site}"


def serial(row):
    return {k: utc(v).isoformat() if isinstance(v, datetime) else v for k, v in dict(row).items()}


class RecordingStore:
    def __init__(self, store):
        self.store = store

    def start(self, query, source_kind, now=None, source_context=None):
        now = now or datetime.now(timezone.utc)
        base = run_number(query, now)
        for suffix in range(1, 101):
            row = dict(
                id=str(uuid4()),
                run_number=base if suffix == 1 else f"{base}_{suffix:02}",
                **query.model_dump(),
                source_kind=source_kind,
                started_at=now,
                ended_at=None,
                source_context=source_context
                or {"catalog": [metric_spec(k) for k in query.metrics]},
            )
            try:
                with self.store._lock, self.store.engine.begin() as c:
                    robot = (
                        c.execute(
                            select(robots)
                            .where(robots.c.robot_id == query.robot_id)
                            .with_for_update()
                        )
                        .mappings()
                        .first()
                    )
                    if not robot or not robot["enabled"] or robot["source_kind"] != source_kind:
                        raise ValueError("机器人未登记、已撤销或来源不一致")
                    c.execute(insert(sessions).values(**row))
                return serial(row)
            except IntegrityError as exc:
                constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
                collision = (
                    constraint == "monitor_recordings_run_number_key"
                    or "UNIQUE constraint failed: monitor_recordings.run_number" in str(exc.orig)
                )
                if not collision:
                    raise
                if suffix == 100:
                    raise ValueError("同一分钟内编号冲突过多，请稍后重试") from None
        raise ValueError("无法生成记录编号")

    def stop(self, recording_id, now=None):
        with self.store._lock, self.store.engine.begin() as c:
            c.execute(
                update(sessions)
                .where(sessions.c.id == recording_id, sessions.c.ended_at.is_(None))
                .values(ended_at=now or datetime.now(timezone.utc))
            )

    def append(self, recording_id, sequence, observed_at, payload):
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        with self.store._lock, self.store.engine.begin() as c:
            owner = c.execute(
                select(robots.c.enabled)
                .select_from(sessions.join(robots))
                .where(sessions.c.id == recording_id)
                .with_for_update(of=robots)
            ).first()
            if not owner or not owner.enabled:
                raise ValueError("记录对应的机器人不存在或已撤销")
            old = c.execute(
                select(samples.c.payload_hash).where(
                    samples.c.recording_id == recording_id, samples.c.sequence == sequence
                )
            ).first()
            if old:
                if old.payload_hash != digest:
                    raise ValueError("同一采样序号的内容发生冲突")
                return
            c.execute(
                insert(samples).values(
                    recording_id=recording_id,
                    sequence=sequence,
                    observed_at=observed_at,
                    payload=payload,
                    payload_hash=digest,
                )
            )

    def list(self, robot_id=None, limit=200, before=None):
        q = (
            select(sessions)
            .order_by(sessions.c.started_at.desc(), sessions.c.id.desc())
            .limit(limit)
        )
        if robot_id:
            q = q.where(sessions.c.robot_id == robot_id)
        if before:
            # Cursor is a session ID; order remains stable for equal start timestamps.
            with self.store.engine.connect() as c:
                marker = (
                    c.execute(select(sessions).where(sessions.c.id == before)).mappings().first()
                )
            if not marker:
                raise ValueError("记录分页游标不存在")
            from sqlalchemy import and_, or_

            q = q.where(
                or_(
                    sessions.c.started_at < marker["started_at"],
                    and_(sessions.c.started_at == marker["started_at"], sessions.c.id < before),
                )
            )
        with self.store._read_lock, self.store.engine.connect() as c:
            return [serial(row) for row in c.execute(q).mappings()]

    def curve(self, robot_id, metrics, recording_id=None, start=None, end=None, max_points=800):
        if not metrics or len(metrics) > 12 or len(metrics) != len(set(metrics)):
            raise ValueError("每次请选择1至12个不同指标")
        relation = samples.join(sessions, sessions.c.id == samples.c.recording_id)
        conditions = [sessions.c.robot_id == robot_id]
        if recording_id:
            conditions.append(sessions.c.id == recording_id)
        if start:
            conditions.append(samples.c.observed_at >= start)
        if end:
            conditions.append(samples.c.observed_at <= end)
        with self.store._read_lock, self.store.engine.connect() as c:
            bound = c.execute(
                select(
                    func.min(samples.c.observed_at),
                    func.max(samples.c.observed_at),
                    func.count(),
                    func.count(func.distinct(sessions.c.id)),
                )
                .select_from(relation)
                .where(*conditions)
            ).one()
            source_kinds = list(
                c.execute(
                    select(sessions.c.source_kind)
                    .select_from(relation)
                    .where(*conditions)
                    .distinct()
                ).scalars()
            )
            related = (
                c.execute(
                    select(sessions)
                    .select_from(relation)
                    .where(*conditions)
                    .distinct()
                    .order_by(sessions.c.started_at)
                    .limit(200)
                )
                .mappings()
                .all()
            )
            details = dict(
                metrics=[metric_spec(k) for k in metrics],
                session_count=bound[3],
                source_kinds=source_kinds,
                sessions=[serial(r) for r in related],
                sessions_truncated=bound[3] > 200,
            )
            if not bound[2]:
                return dict(points=[], count=0, interval_seconds=0, **details)
            low, high = utc(bound[0]), utc(bound[1])
            width = max((high - low).total_seconds() / max(1, max_points - 1), 0.001)
            order = [samples.c.observed_at, samples.c.id]
            payload = samples.c.payload
            if c.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import JSONB

                payload = cast(payload, JSONB)
            source = (
                select(
                    samples.c.observed_at,
                    samples.c.recording_id,
                    payload.label("document"),
                    sessions.c.frequency_hz,
                    func.lag(samples.c.observed_at).over(order_by=order).label("previous"),
                    func.lag(samples.c.recording_id).over(order_by=order).label("previous_session"),
                )
                .select_from(relation)
                .where(*conditions)
                .cte("recorded_points")
            )
            if c.dialect.name == "postgresql":
                source = source.prefix_with("MATERIALIZED", dialect="postgresql")

            def epoch(column):
                return (
                    func.extract("epoch", column)
                    if c.dialect.name == "postgresql"
                    else (func.julianday(column) - 2440587.5) * 86400
                )

            bucket = func.floor(
                (epoch(source.c.observed_at) - low.timestamp()) / width + 0.00001
            ).label("bucket")
            gap = case(
                (
                    (source.c.previous_session != source.c.recording_id)
                    | (
                        epoch(source.c.observed_at) - epoch(source.c.previous)
                        > 2.5 / source.c.frequency_hz + 0.1
                    ),
                    1,
                ),
                else_=0,
            )
            columns = [
                bucket,
                func.min(source.c.observed_at).label("time"),
                func.count().label("n"),
                func.max(gap).label("gap"),
                func.sum(gap).label("gap_count"),
                func.min(case((gap == 1, source.c.observed_at))).label("gap_time"),
            ]
            for i, key in enumerate(metrics):
                value = source.c.document["values"][key].as_float()
                columns += [
                    func.avg(value).label(f"v{i}"),
                    func.min(value).label(f"lo{i}"),
                    func.max(value).label(f"hi{i}"),
                    func.count(value).label(f"n{i}"),
                ]
            rows = c.execute(select(*columns).group_by(bucket).order_by(bucket)).mappings().all()
        return dict(
            count=bound[2],
            start=low.isoformat(),
            end=high.isoformat(),
            interval_seconds=width,
            **details,
            points=[
                dict(
                    time=utc(r["time"]).isoformat(),
                    gap_before=bool(r["gap"]),
                    values={
                        key: dict(
                            value=r[f"v{i}"]
                            if r[f"n{i}"] == r["n"]
                            and not (
                                r["gap_count"] > 1 or r["gap_time"] and r["gap_time"] > r["time"]
                            )
                            else None,
                            minimum=r[f"lo{i}"],
                            maximum=r[f"hi{i}"],
                            valid=r[f"n{i}"],
                            total=r["n"],
                        )
                        for i, key in enumerate(metrics)
                    },
                )
                for r in rows
            ],
        )


def selected_payload(frame, query, observed_at):
    """Keep original source time and ages; never substitute battery voltage for joints."""
    valid = bool(
        frame
        and frame.get("robot_id") == query.robot_id
        and frame.get("upload", {}).get("collector_connected", False)
    )
    values, ages, missing = {}, {}, {}
    for key in query.metrics:
        age = frame.get("ages_ms", {}).get(key) if frame else None
        value = frame.get("values", {}).get(key) if frame else None
        good = valid and isinstance(age, (int, float)) and 0 <= age <= metric_max_age_ms(key)
        values[key] = (
            float(value)
            if (
                good
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
            else None
        )
        if values[key] is None:
            missing[key] = (
                "disconnected_or_wrong_robot"
                if not valid
                else "not_provided"
                if value is None
                else "stale_or_invalid"
            )
        ages[key] = (
            age if isinstance(age, (int, float)) and math.isfinite(age) and age >= 0 else None
        )
    return dict(
        values=values,
        ages_ms=ages,
        source_at=frame.get("sampled_at") if frame else None,
        observed_at=observed_at.isoformat(),
        stream_id=frame.get("stream_id") if frame else None,
        connected=valid,
        source_kind=frame.get("source_kind") if frame else None,
        time_basis="pc_sampled",
        clock_quality={
            key: frame.get("upload", {}).get(key)
            for key in ("clock_basis", "clock_offset_seconds", "clock_uncertainty_ms")
            if frame and key in frame.get("upload", {})
        },
        configuration_revision=frame.get("monitoring_configuration", {}).get("revision")
        if frame and frame.get("monitoring_configuration")
        else None,
        missing_reasons=missing,
    )


class Recorder:
    """Capture never waits for the PostgreSQL uploader; cache ACK follows commit."""

    def __init__(self, store, path, source, *, allow_sqlite=False):
        self.history, self.source = RecordingStore(store), source
        is_pg = store.engine.dialect.name == "postgresql"
        inspector = inspect(store.engine)
        self.schema_ready = all(inspector.has_table(t.name) for t in (sessions, samples))
        self.enabled = self.schema_ready and (is_pg or allow_sqlite)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pending (recording_id TEXT, sequence INTEGER,
                observed_at TEXT, payload TEXT, PRIMARY KEY(recording_id,sequence));
            CREATE TABLE IF NOT EXISTS active (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT);
            CREATE TABLE IF NOT EXISTS ending (recording_id TEXT PRIMARY KEY, ended_at TEXT);
            CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), database_id TEXT);
        """)
        identity = hashlib.sha256(
            store.engine.url.render_as_string(hide_password=True).encode()
        ).hexdigest()
        row = self.db.execute("SELECT database_id FROM binding WHERE id=1").fetchone()
        if (
            row
            and row[0] != identity
            and any(
                self.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                for table in ("pending", "active", "ending")
            )
        ):
            self.db.close()
            raise ValueError("记录缓存绑定另一历史库且仍有待传数据，拒绝切换")
        self.db.execute("INSERT OR REPLACE INTO binding VALUES(1,?)", (identity,))
        self.db.commit()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.threads = []
        self.capture_error = self.upload_error = ""
        row = self.db.execute("SELECT payload FROM active WHERE id=1").fetchone()
        self.active = json.loads(row[0]) if row else None
        self.pending_bytes = self.db.execute(
            "SELECT coalesce(sum(length(payload)),0) FROM pending"
        ).fetchone()[0]
        self.next_sample = time.monotonic()
        self.last_capture = None

    def start_thread(self):
        for name, target in (
            ("pc-record-sampler", self.loop),
            ("pc-record-uploader", self.upload_loop),
        ):
            worker = threading.Thread(target=target, name=name, daemon=True)
            self.threads.append(worker)
            worker.start()

    def state(self):
        with self.lock:
            count = self.db.execute("SELECT count(*) FROM pending").fetchone()[0]
            endings = self.db.execute("SELECT count(*) FROM ending").fetchone()[0]
            pg = self.history.store.engine.dialect.name == "postgresql"
            note = (
                "PostgreSQL 唯一长期历史库"
                if self.enabled and pg
                else "软件验证用SQLite；模拟记录，非生产长期库"
                if self.enabled
                else "PostgreSQL尚未配置"
                if not pg
                else "PostgreSQL缺少监控记录迁移006"
            )
            return dict(
                active=self.active,
                pending_samples=count,
                pending_stops=endings,
                pending_bytes=self.pending_bytes,
                last_capture_at=self.last_capture,
                error="；".join(e for e in (self.capture_error, self.upload_error) if e),
                storage="postgresql" if pg else "sqlite",
                enabled=self.enabled,
                schema_ready=self.schema_ready,
                history_database=note,
            )

    def start(self, query):
        with self.lock:
            if not self.enabled:
                raise ValueError("请先审核数据库迁移006并配置PostgreSQL唯一长期库")
            if self.active:
                raise ValueError("已有监控记录正在运行，请先结束该次记录")
            frame = self.source()
            if (
                not frame
                or frame.get("robot_id") != query.robot_id
                or not frame.get("upload", {}).get("collector_connected")
            ):
                raise ValueError("所选机器人的电脑relay尚未接通，不能开始记录")
            if (
                self.history.store.engine.dialect.name != "postgresql"
                and frame["source_kind"] != "simulated"
            ):
                raise ValueError("SQLite仅用于模拟软件验证，真机长期记录必须使用PostgreSQL")
            snapshot = {
                k: frame.get(k)
                for k in ("joint_names", "device_profile", "monitoring_configuration")
            }
            snapshot["catalog"] = [metric_spec(k) for k in query.metrics]
            session = self.history.start(query, frame["source_kind"], source_context=snapshot)
            self.active = dict(session=session, query=query.model_dump(), sequence=0)
            try:
                with self.db:
                    self.db.execute("INSERT INTO active VALUES(1,?)", (json.dumps(self.active),))
            except Exception:
                self.active = None
                stopped_at = datetime.now(timezone.utc)
                try:
                    with self.db:
                        self.db.execute(
                            "INSERT OR IGNORE INTO ending VALUES(?,?)",
                            (session["id"], stopped_at.isoformat()),
                        )
                except Exception:
                    # If local storage itself failed, never continue with only an in-memory active job.
                    try:
                        self.history.stop(session["id"], stopped_at)
                    except Exception:
                        self.upload_error = "记录建立未完成，存在空的数据库记录待核对；未开始采样"
                raise
            self.next_sample = time.monotonic()
            self.capture_error = ""
            return session

    def stop(self):
        with self.lock:
            if not self.active:
                raise ValueError("没有正在运行的监控记录")
            # A disconnected database must not stop the user from ending capture.
            with self.db:
                self.db.execute(
                    "INSERT INTO ending VALUES(?,?)",
                    (self.active["session"]["id"], datetime.now(timezone.utc).isoformat()),
                )
                self.db.execute("DELETE FROM active WHERE id=1")
            self.active = None

    def capture(self, now=None):
        with self.lock:
            if not self.active or not self.enabled:
                return
            if (
                shutil.disk_usage(self.path.parent).free < 512 * 1024**2
                or self.pending_bytes >= 256 * 1024**2
            ):
                raise OSError("电脑待传缓存空间不足；保留已有数据并暂停采样")
            query = RecordingRequest.model_validate(self.active["query"])
            frame = self.source()
            if frame and frame.get("source_kind") != self.active["session"]["source_kind"]:
                frame = None
            now = now or datetime.now(timezone.utc)
            payload = json.dumps(selected_payload(frame, query, now), allow_nan=False)
            sequence = self.active["sequence"] + 1
            changed = {**self.active, "sequence": sequence}
            if self.pending_bytes + len(payload.encode()) > 256 * 1024**2:
                raise OSError("电脑待传缓存达到容量上限")
            with self.db:
                self.db.execute(
                    "INSERT INTO pending VALUES(?,?,?,?)",
                    (self.active["session"]["id"], sequence, now.isoformat(), payload),
                )
                self.db.execute("UPDATE active SET payload=? WHERE id=1", (json.dumps(changed),))
            self.pending_bytes += len(payload.encode())
            self.active = changed
            self.last_capture = now.isoformat()

    def drain(self, limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT recording_id,sequence,observed_at,payload FROM pending ORDER BY observed_at LIMIT ?",
                (limit,),
            ).fetchall()
        for identifier, sequence, stamp, payload in rows:
            self.history.append(
                identifier, sequence, datetime.fromisoformat(stamp), json.loads(payload)
            )
            with self.lock, self.db:
                self.db.execute(
                    "DELETE FROM pending WHERE recording_id=? AND sequence=?",
                    (identifier, sequence),
                )
                self.pending_bytes -= len(payload.encode())
        with self.lock:
            ending = self.db.execute(
                "SELECT recording_id,ended_at FROM ending WHERE NOT EXISTS (SELECT 1 FROM pending WHERE pending.recording_id=ending.recording_id)"
            ).fetchall()
        for identifier, stamp in ending:
            self.history.stop(identifier, datetime.fromisoformat(stamp))
            with self.lock, self.db:
                self.db.execute("DELETE FROM ending WHERE recording_id=?", (identifier,))

    def loop(self):
        while not self.stop_event.wait(0.02):
            try:
                if time.monotonic() >= self.next_sample:
                    self.capture()
                    with self.lock:
                        period = 1 / self.active["query"]["frequency_hz"] if self.active else 1
                    self.next_sample += period
                    if self.next_sample < time.monotonic():
                        self.next_sample = time.monotonic() + period
                    self.capture_error = ""
            except Exception:
                self.capture_error = "采样暂停或未完成，请检查接收链路与电脑磁盘；已有缓存保留"
                self.next_sample = time.monotonic() + 1

    def upload_loop(self):
        while not self.stop_event.wait(0.5):
            try:
                if self.schema_ready:
                    self.drain()
                    self.upload_error = ""
            except Exception:
                self.upload_error = "PostgreSQL入库未确认，待传缓存保留并重试"
                self.stop_event.wait(2.5)

    def close(self):
        self.stop_event.set()
        for worker in self.threads:
            worker.join(timeout=20)
        if any(worker.is_alive() for worker in self.threads):
            return
        self.db.close()


def relay_frame(url):
    """GET only. This never consumes or acknowledges a robot transfer queue."""
    try:
        with httpx.Client(timeout=0.7, trust_env=False, follow_redirects=False) as c:
            r = c.get(url + "/api/v1/doctor")
            r.raise_for_status()
            frame = PreviewFrame.model_validate(r.json()["snapshot"]).model_dump(mode="json")
            return frame if frame.get("upload", {}).get("stage") == "upper_computer" else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def recording_catalog(frame):
    entries = {entry["key"]: entry for entry in frame.get("catalog", [])} if frame else {}
    count = len(frame.get("joint_names", [])) if frame else 0
    if not count and frame:
        count = max(
            [int(k.split(".")[1]) + 1 for k in frame.get("values", {}) if k.startswith("joint.")]
            or [0]
        )
    if frame:
        configuration = (frame.get("monitoring_configuration") or {}).get("configuration") or {}
        declared_dof = configuration.get("dof")
        if type(declared_dof) is int and declared_dof in (24, 28):
            # A configured joint can be selected before its first feedback arrives.
            count = max(count, declared_dof)
    for key in ("battery.soc", "battery.voltage", "battery.current", "battery.temperature"):
        entries.setdefault(key, metric_spec(key))
    for j in range(min(count, 28)):
        for quantity in (
            "motor_temperature",
            "mos_temperature",
            "torque",
            "voltage",
            "position",
            "velocity",
        ):
            key = f"joint.{j}.{quantity}"
            entries.setdefault(key, metric_spec(key))
    values = frame.get("values", {}) if frame else {}
    return [
        {**entry, "available": values.get(key) is not None}
        for key, entry in sorted(entries.items())
    ]
