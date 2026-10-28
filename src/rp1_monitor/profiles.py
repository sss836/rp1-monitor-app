"""Versioned, non-secret hardware declarations; observations remain separate."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MotorBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    joint_name: str = Field(min_length=1, max_length=120)
    bus: str = Field(min_length=1, max_length=64)
    motor_id: int = Field(ge=0, le=65535)
    protocol: Literal["lro", "rp"]


class ProcessWatch(BaseModel):
    """Exact Linux comm in the collector's PID namespace; never a shell command."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    label: str = Field(min_length=1, max_length=64)
    comm: str = Field(min_length=1, max_length=15, pattern=r"^[^/\x00-\x1f]+$")

    @field_validator("comm")
    @classmethod
    def kernel_name_length(cls, value):
        if len(value.encode("utf-8")) > 15:
            raise ValueError("Linux comm不能超过15字节；请填写/proc/PID/comm实际内容")
        return value


class RobotProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    robot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    hardware_revision: str = Field(default="unknown", max_length=128)
    inference_revision: str = Field(default="unknown", max_length=128)
    driver_revision: str = Field(default="unknown", max_length=128)
    bms_type: Literal["unknown", "journal", "rp_server"] = "unknown"
    motor_health_topic: str = Field(
        default="/rp1/diagnostics", pattern=r"^/[A-Za-z0-9_/]+$", max_length=128
    )
    motors: list[MotorBinding] = Field(default_factory=list, max_length=64)
    processes: list[ProcessWatch] = Field(default_factory=list, max_length=16)
    physical_mapping_verified: bool = False

    @model_validator(mode="after")
    def unique_mapping(self):
        if len({p.key for p in self.processes}) != len(self.processes):
            raise ValueError("进程监测key不能重复")
        if len({p.comm for p in self.processes}) != len(self.processes):
            raise ValueError("进程监测comm不能重复；同名多个实例应显示冲突")
        if len({m.joint_name for m in self.motors}) != len(self.motors):
            raise ValueError("关节映射名称不能重复")
        if len({(m.bus, m.motor_id) for m in self.motors}) != len(self.motors):
            raise ValueError("总线和电机ID组合不能重复")
        if self.physical_mapping_verified and not self.motors:
            raise ValueError("确认物理映射前必须提供电机清单")
        return self


def load_profile(path: Path | None, robot_id: str) -> RobotProfile:
    profile = (
        RobotProfile.model_validate_json(path.read_text())
        if path
        else RobotProfile(robot_id=robot_id)
    )
    if profile.robot_id != robot_id:
        raise ValueError("设备档案与机器人ID不一致")
    return profile


def profile_json(profile: RobotProfile) -> str:
    return json.dumps(profile.model_dump(), ensure_ascii=False, indent=2) + "\n"
