"""Versioned, per-robot display and thermal criteria; never changes robot control."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .motor_map import JointLocation, MotorMapConfig, RobotMotorMap


class MonitoringConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    hardware_revision: str = Field(default="unknown", min_length=1, max_length=120)
    dof: Literal[24, 28]
    physical_mapping_verified: bool = False
    scale_min_c: float = 20
    scale_max_c: float = 100
    joints: list[JointLocation] = Field(min_length=24, max_length=28)
    temperature_limits_c: dict[str, float] = Field(default_factory=dict, max_length=56)

    @field_validator("hardware_revision")
    @classmethod
    def nonblank_revision(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("机型版本不能为空白")
        return value

    @model_validator(mode="after")
    def complete_and_unique(self):
        if self.scale_max_c <= self.scale_min_c:
            raise ValueError("色阶最高温度必须大于最低温度；色阶不是安全限值")
        if sorted(item.index for item in self.joints) != list(range(self.dof)):
            raise ValueError("关节清单必须完整覆盖0至自由度减1，每个索引一次")
        names = [item.joint_name for item in self.joints if item.joint_name is not None]
        if len(names) != len(set(names)):
            raise ValueError("实际关节名称不能重复")
        addresses = [(item.bus, item.motor_id) for item in self.joints if item.bus is not None]
        if len(addresses) != len(set(addresses)):
            raise ValueError("同一总线上的电机编号不能重复")
        for key in self.temperature_limits_c:
            match = re.fullmatch(r"joint\.(0|[1-9][0-9]*)\.(motor|mos)_temperature", key)
            if not match or int(match[1]) >= self.dof:
                raise ValueError("温度限值只接受当前自由度内电机或MOS温度指标")
        return self

    def validate_source(self, source_kind):
        if (
            source_kind == "ros2"
            and self.physical_mapping_verified
            and any(not item.joint_name for item in self.joints)
        ):
            raise ValueError("确认ROS2物理映射前必须填写全部实际关节名称")
        return self

    def motor_map(self, robot_id, source_kind):
        self.validate_source(source_kind)
        # Unverified ROS2 drafts may hold unknown joint names. They remain unmapped.
        mapping = RobotMotorMap.model_construct(
            source_kind=source_kind,
            dof=self.dof,
            physical_mapping_verified=self.physical_mapping_verified,
            scale_min_c=self.scale_min_c,
            scale_max_c=self.scale_max_c,
            joints=self.joints,
        )
        # The configuration schema already validates a complete draft roster.
        # Do not reapply file-only ROS name requirements to an unverified draft.
        return MotorMapConfig.model_construct(robots={robot_id: mapping})


def empty_configuration(dof=24):
    return MonitoringConfiguration(
        dof=dof, joints=[JointLocation(index=index, label=f"关节 {index}") for index in range(dof)]
    )


def local_configuration(robot_id, source_kind, motor_map, policy):
    mapping = motor_map.robots.get(robot_id)
    if not mapping or mapping.source_kind != source_kind:
        return {
            "robot_id": robot_id,
            "source_kind": source_kind,
            "revision": None,
            "configuration": empty_configuration().model_dump(),
            "source": "unconfigured",
        }
    dof = mapping.dof or (28 if any(item.index >= 24 for item in mapping.joints) else 24)
    by_index = {item.index: item for item in mapping.joints}
    configuration = empty_configuration(dof).model_copy(
        update={
            "physical_mapping_verified": mapping.physical_mapping_verified,
            "scale_min_c": mapping.scale_min_c,
            "scale_max_c": mapping.scale_max_c,
            "joints": [
                by_index.get(index, JointLocation(index=index, label=f"关节 {index}"))
                for index in range(dof)
            ],
            "temperature_limits_c": {
                key: value
                for key, value in policy.temperature_limits_c.items()
                if (match := re.fullmatch(r"joint\.(0|[1-9][0-9]*)\.(motor|mos)_temperature", key))
                and int(match[1]) < dof
            },
        }
    )
    return {
        "robot_id": robot_id,
        "source_kind": source_kind,
        "revision": None,
        "configuration": configuration.model_dump(),
        "source": "local_file",
    }


def monitoring_runtime(envelope, robot_id, source_kind, fallback_map, fallback_policy):
    """Only a matching database version replaces local display and temperature settings."""
    if not envelope or envelope.get("source") != "database":
        return fallback_map, fallback_policy
    if (envelope.get("robot_id"), envelope.get("source_kind")) != (robot_id, source_kind):
        raise ValueError("监测配置与机器人身份或来源不匹配")
    configuration = MonitoringConfiguration.model_validate(envelope["configuration"])
    configuration.validate_source(source_kind)
    limits = {
        key: value
        for key, value in fallback_policy.temperature_limits_c.items()
        if not re.fullmatch(r"joint\.(0|[1-9][0-9]*)\.(motor|mos)_temperature", key)
    }
    limits.update(configuration.temperature_limits_c)
    policy = type(fallback_policy).model_validate(
        {**fallback_policy.model_dump(), "temperature_limits_c": limits}
    )
    return configuration.motor_map(robot_id, source_kind), policy
