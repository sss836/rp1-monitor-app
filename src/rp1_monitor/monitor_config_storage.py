"""Backend-only table declaration, excluded from the core eleven-table automatic DDL."""

from sqlalchemy import JSON, BigInteger, Column, DateTime, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

configuration_versions = Table(
    "monitor_robot_config_versions",
    MetaData(),
    Column("id", BigInteger, primary_key=True),
    Column("robot_id", Text, nullable=False),
    Column("previous_id", BigInteger),
    Column("configuration", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor_db_login", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
