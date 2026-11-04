"""Factory checks outside the unchanged fourteen-plugin TUI protocol."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import RobotId
from .robopi_tui import PASSIVE_PLUGINS, PLUGIN_GROUPS, PLUGIN_NOTES

EXTRA_CHECKS = {"IMU_CONTINUOUS": "IMU连续数据", "IMU_FIRMWARE": "IMU固件版本", "BMS_RECOVERY": "BMS重启恢复"}
BASIC_CHECKS = {**PASSIVE_PLUGINS, **EXTRA_CHECKS}
BASIC_GROUPS = {**PLUGIN_GROUPS, "IMU_CONTINUOUS": "通信与 IMU", "IMU_FIRMWARE": "通信与 IMU", "BMS_RECOVERY": "动力电池"}
BASIC_NOTES = {
    **PLUGIN_NOTES,
    "IMU_CONTINUOUS": "被动接收 can_hipnuc 上 HIPNUC ID 8 的四元数和角速度5秒；每路至少50Hz，最大接收间隔不超过0.2秒。不发送CAN报文，不检查安装方向或绝对精度。",
    "IMU_FIRMWARE": "向 can_hipnuc 上的 HIPNUC ID 8 发送一次固件版本读取请求；未配置允许版本时显示待确认。",
    "BMS_RECOVERY": "执行时重启 bms.service，然后在30秒内检查新启动实例运行正常且恢复数据日志；不会重启机器人或操作电机。需要本次服务重启权限。",
}


class FactoryCheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    report_kind: Literal["factory_check"] = "factory_check"
    schema_version: Literal[1] = 1
    report_id: UUID
    robot_id: RobotId
    source_kind: Literal["ros2", "simulated"]
    validation_scope: Literal["simulated", "software_observation_not_hardware_acceptance"]
    created_at: datetime
    check_id: Literal["IMU_CONTINUOUS", "IMU_FIRMWARE", "BMS_RECOVERY"]
    executor_version: Literal["factory-basic-1"] = "factory-basic-1"
    result: Literal["pass", "fail", "error"]
    message: str = Field(min_length=1, max_length=1000)
    details: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def traceable(self):
        expected = "simulated" if self.source_kind == "simulated" else "software_observation_not_hardware_acceptance"
        if self.created_at.tzinfo is None or self.validation_scope != expected:
            raise ValueError("工厂检查时间或来源无效")
        return self

    def head(self):
        return self.model_dump(mode="json", exclude={"check_id", "schema_version"})
