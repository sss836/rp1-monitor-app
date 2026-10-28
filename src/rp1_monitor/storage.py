"""Monitoring-only SQL tables. No dependency on formal execution or MTBF tables."""

import hashlib
import json
import math
import threading
from contextlib import nullcontext
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    case,
    cast,
    create_engine,
    event,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.pool import StaticPool

from .aggregation import TOPICS
from .health_view import build_health
from .models import TelemetryBatch, TelemetryWindow, metric_max_age_ms, metric_spec
from .process_monitor import build_process_view

metadata = MetaData()
JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
robots = Table(
    "monitor_robots",
    metadata,
    Column("robot_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("token_digest", Text, nullable=False, unique=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("joint_names", JSON_DOCUMENT, nullable=True),
    CheckConstraint("source_kind IN ('simulated','ros2')", name="ck_monitor_robot_source"),
    CheckConstraint("length(token_digest) = 64", name="ck_monitor_token_digest"),
)
latest = Table(
    "monitor_latest",
    metadata,
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), primary_key=True),
    Column("stream_id", Text, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("stream_first_seen", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON_DOCUMENT, nullable=False),
    CheckConstraint("sequence > 0", name="ck_monitor_latest_sequence"),
)
history = Table(
    "monitor_windows",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), nullable=False, index=True),
    Column("stream_id", Text, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("payload_hash", Text, nullable=False),
    # JSON preserves signed zero for canonical evidence hashes; JSONB is derived-only.
    Column("payload", JSON, nullable=False),
    UniqueConstraint("robot_id", "stream_id", "sequence", name="uq_monitor_window"),
    CheckConstraint("sequence > 0 AND window_end > window_start", name="ck_monitor_window_range"),
    CheckConstraint("length(payload_hash) = 64", name="ck_monitor_window_hash"),
)
Index("ix_monitor_robot_time", history.c.robot_id, history.c.window_end)
Index("ix_monitor_windows_retention", history.c.window_end, postgresql_using="brin")


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class IngestConflict(Exception):
    pass


class Store:
    def __init__(self, url: str, initialize=True, policy=None):
        from . import central_tables, recordings  # noqa: F401 - register additive monitoring tables
        from .central_policy import CentralPolicy
        from .motor_map import MotorMapConfig

        self.policy = policy or CentralPolicy()
        self.motor_map_config = MotorMapConfig()
        options = {}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 15}
            if url.endswith(":memory:"):
                options["poolclass"] = StaticPool
        if url.startswith("postgresql"):
            options["connect_args"] = {"connect_timeout": 5}
        self.engine = create_engine(url, pool_pre_ping=True, **options)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection, _):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")

        self._lock = threading.RLock()
        # Only StaticPool shares one SQLite connection; PostgreSQL reads use MVCC.
        self._read_lock = self._lock if isinstance(self.engine.pool, StaticPool) else nullcontext()
        if initialize:
            metadata.create_all(self.engine)

    def configure(self, devices, *, authoritative=True):
        with self._lock, self.engine.begin() as connection:
            if authoritative:
                connection.execute(update(robots).values(enabled=False))
            for device in devices:
                values = {
                    "name": device["name"],
                    "source_kind": device["source_kind"],
                    "token_digest": token_digest(device["token"]),
                    "enabled": True,
                }
                old = connection.execute(
                    select(robots).where(robots.c.robot_id == device["robot_id"])
                ).first()
                if old:
                    if old.source_kind != device["source_kind"]:
                        raise ValueError("已有机器人不能混用模拟和ROS2来源，请使用不同ID")
                    if not authoritative:
                        # Management mode: files bootstrap new IDs, never undo human edits/revocation.
                        continue
                    connection.execute(
                        update(robots)
                        .where(robots.c.robot_id == device["robot_id"])
                        .values(**values)
                    )
                else:
                    connection.execute(insert(robots).values(robot_id=device["robot_id"], **values))

    def device_for_digest(self, digest):
        with self._read_lock, self.engine.connect() as connection:
            row = connection.execute(
                select(robots.c.robot_id, robots.c.source_kind, robots.c.token_digest).where(
                    (robots.c.token_digest == digest) & robots.c.enabled
                )
            ).first()
            return dict(row._mapping) if row else None

    def _monitoring_configuration(self, connection, robot_id, source_kind, *, has_table=None):
        from .monitor_config import (
            MonitoringConfiguration,
            local_configuration,
        )
        from .monitor_config_storage import configuration_versions

        if has_table is None:
            has_table = inspect(connection).has_table(configuration_versions.name)
        if has_table:
            row = (
                connection.execute(
                    select(configuration_versions.c.id, configuration_versions.c.configuration)
                    .where(configuration_versions.c.robot_id == robot_id)
                    .order_by(configuration_versions.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row:
                configuration = MonitoringConfiguration.model_validate(row["configuration"])
                configuration.validate_source(source_kind)
                return {
                    "robot_id": robot_id,
                    "source_kind": source_kind,
                    "revision": row["id"],
                    "configuration": configuration.model_dump(),
                    "source": "database",
                }
        return local_configuration(robot_id, source_kind, self.motor_map_config, self.policy)

    def effective_monitoring_configuration(self, robot_id, source_kind=None):
        """Read registered identity and latest explicit configuration, including disabled robots."""
        with self._read_lock, self.engine.connect() as connection:
            row = connection.execute(
                select(robots.c.source_kind).where(robots.c.robot_id == robot_id)
            ).first()
            if row is None:
                raise KeyError(robot_id)
            if source_kind is not None and source_kind != row.source_kind:
                raise ValueError("监测配置与登记来源不一致")
            return self._monitoring_configuration(connection, robot_id, row.source_kind)

    def monitoring_policy(self, snapshot):
        from .monitor_config import monitoring_runtime

        _, policy = monitoring_runtime(
            snapshot.get("monitoring_configuration"),
            snapshot["robot_id"],
            snapshot["source_kind"],
            self.motor_map_config,
            self.policy,
        )
        return policy

    def ingest(self, batch: TelemetryBatch, device: dict) -> dict:
        if any(
            window.robot_id != device["robot_id"] or window.source_kind != device["source_kind"]
            for window in batch.windows
        ):
            raise PermissionError("设备凭据只能写绑定的机器人及来源")
        receipts, inserted, duplicates, live_ignored = [], 0, 0, 0
        with self._lock, self.engine.begin() as connection:
            from .central_data import register_policy

            now = datetime.now(timezone.utc)
            # Consistent lock order with the alert worker: policy first, robot second.
            register_policy(connection, self.policy, now)
            registered = (
                connection.execute(
                    select(robots).where(robots.c.robot_id == device["robot_id"]).with_for_update()
                )
                .mappings()
                .first()
            )
            if (
                not registered
                or not registered["enabled"]
                or registered["token_digest"] != device["token_digest"]
                or registered["source_kind"] != device["source_kind"]
            ):
                raise PermissionError("设备凭据已撤销或变更")
            for window in batch.windows:
                if window.source_kind == "ros2" and window.quality.joint_names:
                    if (
                        registered["joint_names"] is not None
                        and registered["joint_names"] != window.quality.joint_names
                    ):
                        raise IngestConflict(
                            "机器人关节规范名单变化，拒绝把不同物理指标混入同一历史；请核对映射"
                        )
                    if registered["joint_names"] is None:
                        connection.execute(
                            update(robots)
                            .where(robots.c.robot_id == window.robot_id)
                            .values(joint_names=window.quality.joint_names)
                        )
                        registered = {**registered, "joint_names": window.quality.joint_names}
                payload = window.model_dump(mode="json")
                digest = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                key = (
                    (history.c.robot_id == window.robot_id)
                    & (history.c.stream_id == str(window.stream_id))
                    & (history.c.sequence == window.sequence)
                )
                old = (
                    connection.execute(select(history.c.payload_hash, history.c.payload).where(key))
                    .mappings()
                    .first()
                )
                if old is not None:
                    # Old queue entries acquire empty optional health fields when
                    # parsed by the new contract. Compare validated facts on a
                    # hash mismatch without rewriting the stored evidence/hash.
                    if (
                        old["payload_hash"] != digest
                        and TelemetryWindow.model_validate(old["payload"]) != window
                    ):
                        raise IngestConflict("相同采集身份的内容不一致")
                    duplicates += 1
                else:
                    inserted_row = connection.execute(
                        insert(history).values(
                            robot_id=window.robot_id,
                            stream_id=str(window.stream_id),
                            sequence=window.sequence,
                            window_start=window.window_start,
                            window_end=window.window_end,
                            received_at=now,
                            payload_hash=digest,
                            payload=payload,
                        )
                    )
                    from .central_data import project_window

                    project_window(
                        connection, inserted_row.inserted_primary_key[0], window, self.policy, now
                    )
                    inserted += 1
                    from .transport_clock import live_metadata

                    transport = live_metadata(batch.live_observation, window, now)
                    if (
                        batch.delivery == "live"
                        and (
                            transport is not None
                            or abs((now - window.window_end).total_seconds()) <= 5
                        )
                        and window.quality.clock_status != "WALL_CLOCK_CHANGED"
                    ):
                        current = (
                            connection.execute(
                                select(latest).where(latest.c.robot_id == window.robot_id)
                            )
                            .mappings()
                            .first()
                        )
                        if current and current["stream_id"] == str(window.stream_id):
                            first_seen = utc(current["stream_first_seen"])
                            should_replace = window.sequence > current["sequence"]
                        else:
                            first_seen = utc(
                                connection.execute(
                                    select(func.min(history.c.received_at)).where(
                                        (history.c.robot_id == window.robot_id)
                                        & (history.c.stream_id == str(window.stream_id))
                                    )
                                ).scalar_one()
                            )
                            should_replace = current is None or first_seen > utc(
                                current["stream_first_seen"]
                            )
                        if current and window.window_end < datetime.fromisoformat(
                            current["payload"]["window_end"]
                        ):
                            should_replace = False
                        if should_replace:
                            values = {
                                "stream_id": str(window.stream_id),
                                "sequence": window.sequence,
                                "stream_first_seen": first_seen,
                                "received_at": now,
                                "payload": {**payload, "_live_transport": transport}
                                if transport
                                else payload,
                            }
                            if current:
                                connection.execute(
                                    update(latest)
                                    .where(latest.c.robot_id == window.robot_id)
                                    .values(**values)
                                )
                            else:
                                connection.execute(
                                    insert(latest).values(robot_id=window.robot_id, **values)
                                )
                        else:
                            live_ignored += 1
                    elif batch.delivery == "live":
                        live_ignored += 1
                receipts.append({"stream_id": str(window.stream_id), "sequence": window.sequence})
        return {
            "receipts": receipts,
            "inserted": inserted,
            "duplicates": duplicates,
            "live_ignored": live_ignored,
        }

    def snapshots(self):
        now = datetime.now(timezone.utc)
        with self._read_lock, self.engine.connect() as connection:
            from .monitor_config import monitoring_runtime
            from .monitor_config_storage import configuration_versions

            has_configurations = inspect(connection).has_table(configuration_versions.name)
            rows = connection.execute(
                select(
                    robots.c.robot_id,
                    robots.c.name,
                    robots.c.source_kind,
                    robots.c.enabled,
                    latest.c.received_at,
                    latest.c.payload,
                )
                .select_from(robots.outerjoin(latest, robots.c.robot_id == latest.c.robot_id))
                .order_by(robots.c.robot_id)
            ).mappings()
            result = []
            for row in rows:
                payload = row["payload"]
                age = (
                    max(0, (now - utc(row["received_at"])).total_seconds())
                    if row["received_at"]
                    else None
                )
                quality = payload["quality"] if payload else {}
                topic_ages = quality.get("topic_ages_ms", {})
                clock_offset = (
                    (
                        utc(row["received_at"]) - datetime.fromisoformat(payload["window_end"])
                    ).total_seconds()
                    if payload
                    else None
                )
                from .transport_clock import snapshot_transport

                transport = (
                    snapshot_transport(payload, utc(row["received_at"]), age) if payload else None
                )
                transport_verified = bool(transport and transport["freshness_verified"])
                clock_usable = (
                    transport_verified
                    if transport is not None
                    else (clock_offset is not None and abs(clock_offset) <= 5)
                )
                fresh = (
                    age is not None
                    and clock_usable
                    and all(
                        topic_ages.get(topic) is not None
                        and topic_ages[topic]
                        + (
                            transport["elapsed_seconds"]
                            if transport is not None
                            else age + max(0, clock_offset or 0)
                        )
                        * 1000
                        <= 1500
                        for topic in TOPICS
                    )
                )
                fresh = fresh and (
                    row["source_kind"] != "ros2" or quality.get("target_alignment") != "unverified"
                )
                metrics = payload["metrics"] if payload else {}
                elapsed_ms = (
                    transport["elapsed_seconds"]
                    if transport is not None
                    else (age or 0) + max(0, clock_offset or 0)
                ) * 1000
                fresh = fresh and all(
                    metric["count"] > 0
                    and metric.get("age_ms") is not None
                    and metric["age_ms"] + elapsed_ms <= 1500
                    for key, metric in metrics.items()
                    if metric_max_age_ms(key) <= 1000
                )
                available = [key for key, value in metrics.items() if value["count"] > 0]
                health_values = quality.get("health_values", {})
                health_ages = {
                    key: value + elapsed_ms
                    for key, value in quality.get("health_ages_ms", {}).items()
                }
                # Motion keys identify the observed joint roster; only health observations
                # use the explicit last-observed snapshot, never forward-fill window facts.
                health_values = {
                    **{
                        key: value.get("last")
                        for key, value in metrics.items()
                        if metric_max_age_ms(key) <= 1000
                    },
                    **health_values,
                }
                health = build_health(
                    health_values,
                    health_ages,
                    quality.get("health_sources", {}),
                    quality.get("health_sensors", {}),
                    quality.get("joint_names", []),
                    available=bool(row["enabled"])
                    and age is not None
                    and age <= 5
                    and clock_usable
                    and quality.get("clock_status") != "WALL_CLOCK_CHANGED",
                )
                from .motor_map import attach_motor_map, build_motor_map

                configuration = self._monitoring_configuration(
                    connection, row["robot_id"], row["source_kind"], has_table=has_configurations
                )
                mapping, effective_policy = monitoring_runtime(
                    configuration,
                    row["robot_id"],
                    row["source_kind"],
                    self.motor_map_config,
                    self.policy,
                )

                motor_map = build_motor_map(
                    row["robot_id"],
                    row["source_kind"],
                    health,
                    mapping,
                    effective_policy.temperature_limits_c,
                )
                attach_motor_map(health, motor_map)
                process_rows = [
                    {
                        **item,
                        "age_ms": item["age_ms"] + elapsed_ms
                        if item.get("age_ms") is not None
                        else None,
                    }
                    for item in quality.get("processes", [])
                ]
                health["processes"] = build_process_view(
                    process_rows,
                    health_values,
                    health_ages,
                    available=bool(row["enabled"])
                    and age is not None
                    and age <= 5
                    and clock_usable
                    and quality.get("clock_status") != "WALL_CLOCK_CHANGED",
                )
                fresh = (
                    fresh
                    and any(key.endswith(".position") for key in available)
                    and any(key.endswith(".policy_target") for key in available)
                    and any(key.startswith("imu.quaternion.") for key in available)
                )
                result.append(
                    {
                        "robot_id": row["robot_id"],
                        "name": row["name"],
                        "source_kind": row["source_kind"],
                        "monitoring_configuration": configuration,
                        "enabled": row["enabled"],
                        "online": bool(row["enabled"]) and age is not None and age <= 5,
                        "data_fresh": bool(row["enabled"])
                        and bool(fresh)
                        and quality.get("invalid_values", 0) == 0,
                        "received_at": utc(row["received_at"]).isoformat()
                        if row["received_at"]
                        else None,
                        "age_seconds": age,
                        "clock_offset_seconds": clock_offset,
                        "transport": transport,
                        "clock_unverified": clock_offset is None
                        or abs(clock_offset) > 5
                        or quality.get("clock_status") == "WALL_CLOCK_CHANGED",
                        "metrics": payload["metrics"] if payload else {},
                        "stream_id": payload["stream_id"] if payload else None,
                        "sequence": payload["sequence"] if payload else None,
                        "quality": quality,
                        "health": health,
                        "captured_at": payload["window_end"] if payload else None,
                        "window_duration_seconds": (
                            datetime.fromisoformat(payload["window_end"])
                            - datetime.fromisoformat(payload["window_start"])
                        ).total_seconds()
                        if payload
                        else None,
                        "catalog": [metric_spec(key) for key in sorted(payload["metrics"])]
                        if payload
                        else [],
                    }
                )
            return {"items": result, "as_of_at": now.isoformat()}

    def curves(
        self,
        robot_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        max_points: int,
        tier="raw",
        *,
        component=False,
    ):
        if tier not in {"raw", "key"}:
            raise ValueError("曲线层必须为raw或key")
        from .central_tables import key_windows
        from .component_history import component_windows, metric_expressions

        spec = metric_spec(metric)
        width = max((end - start).total_seconds() / max_points, 0.001)
        windows = component_windows(self, robot_id, start, end) if component else history
        document = windows.c.payload["metrics"] if tier == "raw" else key_windows.c.metrics
        source = (
            windows
            if tier == "raw"
            else windows.outerjoin(key_windows, windows.c.id == key_windows.c.window_id)
        )
        value, low, high, count, squares = metric_expressions(document, windows.c.payload, metric)
        bucket = cast(
            func.floor(
                (cast(func.extract("epoch", windows.c.window_end), Float) - start.timestamp())
                / width
            ),
            Integer,
        )
        base = (
            select(
                windows.c.window_start.label("start"),
                windows.c.window_end.label("time"),
                value.label("value"),
                low.label("low"),
                high.label("high"),
                func.coalesce(count, 0).label("count"),
                squares.label("sum_squares"),
                bucket.label("bucket"),
            )
            .select_from(source)
            .where(
                (windows.c.robot_id == robot_id)
                & (windows.c.window_end >= start)
                & (windows.c.window_end < end)
            )
            .cte("samples")
        )
        ranked = select(
            base,
            func.row_number()
            .over(partition_by=base.c.bucket, order_by=base.c.time.desc())
            .label("rank"),
            func.lag(base.c.time).over(order_by=base.c.time).label("previous_time"),
        ).cte("ranked")
        query = (
            select(
                func.max(ranked.c.time).label("time"),
                func.max(case((ranked.c.rank == 1, ranked.c.value), else_=None)).label("last"),
                func.min(ranked.c.low).label("minimum"),
                func.max(ranked.c.high).label("maximum"),
                func.sum(ranked.c.count).label("samples"),
                func.sum(ranked.c.sum_squares).label("sum_squares"),
                func.sum(case((ranked.c.count == 0, 1), else_=0)).label("missing_windows"),
                func.sum(
                    case(
                        (
                            (
                                cast(func.extract("epoch", ranked.c.start), Float)
                                - cast(func.extract("epoch", ranked.c.previous_time), Float)
                            )
                            > 0.2,
                            1,
                        ),
                        else_=0,
                    )
                ).label("gap_count"),
            )
            .group_by(ranked.c.bucket)
            .order_by(ranked.c.bucket)
        )
        with self._read_lock, self.engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        points = []
        tracking = metric.endswith(".tracking_rms")
        for row in rows:
            point = dict(row)
            sum_squares = point.pop("sum_squares")
            if tracking:
                point["last"] = (
                    math.sqrt(sum_squares / point["samples"])
                    if point["samples"] and sum_squares is not None
                    else None
                )
                point["sum_squares"] = sum_squares
            point.update(
                time=utc(row["time"]).isoformat(),
                minimum=row["minimum"] if spec["aggregation"] == "range" and not tracking else None,
                maximum=row["maximum"] if spec["aggregation"] == "range" and not tracking else None,
            )
            points.append(point)
        return {
            "robot_id": robot_id,
            "metric": metric,
            "unit": spec["unit"],
            "resolution_seconds": width,
            "kind": "one_second_summary",
            "storage_tier": tier,
            "points": points,
        }
