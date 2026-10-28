"""Monitoring-only additive tables, shared by SQLite verification and PostgreSQL DDL."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from .storage import metadata

DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
IDENTITY = BigInteger().with_variant(Integer, "sqlite")

policies = Table(
    "monitor_policy_versions",
    metadata,
    Column("policy_sha256", Text, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)

key_windows = Table(
    "monitor_key_windows",
    metadata,
    Column("window_id", BigInteger, ForeignKey("monitor_windows.id"), primary_key=True),
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column(
        "policy_sha256", Text, ForeignKey("monitor_policy_versions.policy_sha256"), nullable=False
    ),
    Column("metrics", DOCUMENT, nullable=False),
    Column("json_bytes", BigInteger, nullable=False),
    CheckConstraint("json_bytes >= 0", name="ck_monitor_key_bytes"),
)
Index("ix_monitor_key_robot_time", key_windows.c.robot_id, key_windows.c.window_end)
Index("ix_monitor_key_policy", key_windows.c.policy_sha256)

daily_usage = Table(
    "monitor_daily_usage",
    metadata,
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), primary_key=True),
    Column("received_day", DateTime(timezone=True), primary_key=True),
    Column("windows", BigInteger, nullable=False),
    Column("raw_json_bytes", BigInteger, nullable=False),
    Column("key_json_bytes", BigInteger, nullable=False),
    CheckConstraint(
        "windows >= 0 AND raw_json_bytes >= 0 AND key_json_bytes >= 0",
        name="ck_monitor_usage_nonnegative",
    ),
)
Index("ix_monitor_usage_day", daily_usage.c.received_day)

relay_status = Table(
    "monitor_relay_status",
    metadata,
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), primary_key=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("session_id", Text, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("payload", DOCUMENT, nullable=False),
    CheckConstraint("sequence > 0", name="ck_monitor_relay_sequence"),
)

alert_heads = Table(
    "monitor_alert_heads",
    metadata,
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), primary_key=True),
    Column("rule_key", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("is_open", Boolean, nullable=False),
    Column("condition_since", DateTime(timezone=True)),
    Column("opened_at", DateTime(timezone=True)),
    Column("evaluated_at", DateTime(timezone=True), nullable=False),
    Column(
        "policy_sha256", Text, ForeignKey("monitor_policy_versions.policy_sha256"), nullable=False
    ),
    Column("evidence", DOCUMENT, nullable=False),
    CheckConstraint(
        "state IN ('normal','pending','active','unknown')", name="ck_monitor_alert_state"
    ),
)
Index(
    "ix_monitor_alert_open",
    alert_heads.c.robot_id,
    alert_heads.c.evaluated_at,
    postgresql_where=alert_heads.c.is_open.is_(True),
    sqlite_where=alert_heads.c.is_open.is_(True),
)
Index("ix_monitor_alert_policy", alert_heads.c.policy_sha256)

alert_events = Table(
    "monitor_alert_events",
    metadata,
    Column("id", IDENTITY, primary_key=True, autoincrement=True),
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), nullable=False),
    Column("rule_key", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("event_kind", Text, nullable=False),
    Column(
        "policy_sha256", Text, ForeignKey("monitor_policy_versions.policy_sha256"), nullable=False
    ),
    Column("evidence", DOCUMENT, nullable=False),
    CheckConstraint(
        "event_kind IN ('opened','recovered','observation_lost','observation_restored')",
        name="ck_monitor_alert_event",
    ),
)
Index("ix_monitor_alert_events_robot_id", alert_events.c.robot_id, alert_events.c.id)
Index("ix_monitor_alert_events_policy", alert_events.c.policy_sha256)

archive_manifests = Table(
    "monitor_archive_manifests",
    metadata,
    Column("archive_id", Text, primary_key=True),
    Column("robot_id", Text, ForeignKey("monitor_robots.robot_id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("high_water_id", BigInteger, nullable=False),
    Column("windows", BigInteger, nullable=False),
    Column("compressed_bytes", BigInteger, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("file_name", Text, nullable=False),
    UniqueConstraint("file_name", name="uq_monitor_archive_file"),
    CheckConstraint(
        "window_end > window_start AND windows > 0 AND compressed_bytes > 0",
        name="ck_monitor_archive_range",
    ),
)
Index(
    "ix_monitor_archive_robot_time", archive_manifests.c.robot_id, archive_manifests.c.window_start
)
