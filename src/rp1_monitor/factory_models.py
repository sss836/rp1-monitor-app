"""Local factory segment contract; SN is a label, never a transport identity."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from .models import Model, RobotId, SourceKind

FACTORY_SEGMENT_SCHEMA = "rp1.factory.segment.v1"
FactoryTestType = Literal["standing", "walking", "running", "stunt"]
FactorySegmentState = Literal["recording", "sealed", "interrupted", "fault"]


class FactorySegmentStart(Model):
    segment_id: UUID
    sn: str = Field(min_length=1, max_length=96)
    robot_id: RobotId
    source_kind: SourceKind
    test_type: FactoryTestType
    policy: str = Field(default="", max_length=256)
    policy_source: Literal["operator_label"] = "operator_label"
    note: str = Field(default="", max_length=2000)

    @field_validator("sn")
    @classmethod
    def clean_sn(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("SN不能为空或包含控制字符")
        return value


class FactorySegmentInfo(FactorySegmentStart):
    schema_version: Literal["rp1.factory.segment.v1"] = FACTORY_SEGMENT_SCHEMA
    state: FactorySegmentState
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    sample_counts: dict[str, int] = Field(default_factory=lambda: {"motion": 0, "health": 0})
    valid_counts: dict[str, int] = Field(default_factory=lambda: {"motion": 0, "health": 0})
    missing_counts: dict[str, int] = Field(default_factory=dict)
    event_count: int = 0
    fault: str = ""
    sampling_hz: dict[str, int] = Field(default_factory=lambda: {"motion": 10, "health": 1})


class FactorySegmentManifest(FactorySegmentInfo):
    data_file: Literal["segment.sqlite"] = "segment.sqlite"
    bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
