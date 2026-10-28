"""Small shared wire contract. Times describe host reception, not sensor hardware time."""

import math
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    SecretStr,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from .profiles import RobotProfile

RobotId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
MetricKey = Annotated[
    str,
    StringConstraints(
        pattern=r"^(joint\.[0-9]{1,2}\.(position|velocity|torque|policy_target|tracking_rms|motor_temperature|mos_temperature|voltage|fault_code)|imu\.(quaternion\.[xyzw]|angular_velocity\.[xyz])|battery\.(voltage|current|soc|power_enabled|rail48_enabled|rail5_enabled|temperature|protection)|host\.(cpu\.(?:[0-9]{1,2}\.)?usage|memory\.usage|uptime\.seconds|thermal\.[0-9]{1,2}\.temperature)|process\.[a-z][a-z0-9_]{0,23}\.(alive|cpu_percent|rss_mib|threads|observed_restarts))$"
    ),
]
SourceKind = Literal["simulated", "ros2"]
TargetAlignment = Literal["unverified", "matched_names", "rp1_named_index"]
NonNegativeFloat = Annotated[FiniteFloat, Field(ge=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DeviceRegistration(Model):
    robot_id: RobotId
    name: str = Field(min_length=1, max_length=128)
    source_kind: SourceKind
    token: SecretStr = Field(min_length=32, max_length=256)
    profile: dict | None = None

    @field_validator("profile")
    @classmethod
    def valid_profile(cls, value):
        return RobotProfile.model_validate(value).model_dump() if value is not None else None


def metric_spec(key: str) -> dict[str, str]:
    parts = key.split(".")
    if parts[0] == "process":
        label, unit = {
            "alive": ("存活", "0/1"),
            "cpu_percent": ("CPU（单核100%）", "%"),
            "rss_mib": ("RSS常驻内存（近似）", "MiB"),
            "threads": ("线程数", "个"),
            "observed_restarts": ("观察到的重启", "次"),
        }[parts[2]]
        return {
            "key": key,
            "label": f"进程 {parts[1]} · {label}",
            "unit": unit,
            "aggregation": "range" if parts[2] in {"cpu_percent", "rss_mib", "threads"} else "last",
        }
    if parts[0] == "joint":
        quantity = parts[2]
        label, unit = {
            "position": ("反馈位置", "rad"),
            "velocity": ("反馈速度", "rad/s"),
            "torque": ("上报力矩", "N·m"),
            "policy_target": ("策略目标", "rad"),
            "tracking_rms": ("反馈相对策略目标误差 RMS（同采样配对）", "rad"),
            "motor_temperature": ("电机温度", "°C"),
            "voltage": ("关节反馈电压", "V"),
            "mos_temperature": ("MOS温度", "°C"),
            "fault_code": ("原始故障码", ""),
        }[quantity]
        return {
            "key": key,
            "label": f"关节 {parts[1]} · {label}",
            "unit": unit,
            "aggregation": "last" if quantity == "fault_code" else "range",
        }
    if parts[0] == "battery":
        label, unit = {
            "voltage": ("电池电压", "V"),
            "current": ("电池电流", "A"),
            "soc": ("电池电量", "%"),
            "power_enabled": ("电池供电开关", "0/1"),
            "rail48_enabled": ("48V电源轨开关", "0/1"),
            "rail5_enabled": ("5V电源轨开关", "0/1"),
            "temperature": ("电池温度", "°C"),
            "protection": ("电池原始保护位", ""),
        }[parts[1]]
        return {
            "key": key,
            "label": label,
            "unit": unit,
            "aggregation": "last"
            if parts[1] in {"power_enabled", "rail48_enabled", "rail5_enabled", "protection"}
            else "range",
        }
    if parts[0] == "host":
        if parts[1] == "cpu" and len(parts) == 4:
            return {"key": key, "label": f"CPU {parts[2]} 使用率（单核100%）", "unit": "%", "aggregation": "range"}
        if parts[1] == "uptime":
            return {"key": key, "label": "机器人内核开机时长", "unit": "s", "aggregation": "range"}
        label, unit = (
            (f"主机热区 {parts[2]} 温度", "°C")
            if parts[1] == "thermal"
            else ("CPU使用率" if parts[1] == "cpu" else "内存使用率", "%")
        )
        return {"key": key, "label": label, "unit": unit, "aggregation": "range"}
    quantity = parts[1]
    return {
        "key": key,
        "label": f"IMU · {'四元数' if quantity == 'quaternion' else '角速度'} {parts[2]}",
        "unit": "" if quantity == "quaternion" else "rad/s",
        "aggregation": "last" if quantity == "quaternion" else "range",
    }


def metric_max_age_ms(key: str) -> int:
    """Health polling/logs are slow; motion telemetry remains strict at 1 second."""
    if key.startswith("process."):
        return 6000
    return (
        5000
        if key.startswith(("battery.", "host."))
        or key.endswith((".motor_temperature", ".mos_temperature", ".fault_code"))
        else 1000
    )


class ReportedMotorFault(Model):
    motor_id: int = Field(ge=0, le=65535)
    code: int = Field(ge=0, le=2**32 - 1)
    name: str = Field(default="", max_length=128)


class HealthSourceStatus(Model):
    state: Literal["live", "waiting", "error", "unsupported"]
    detail: str = Field(max_length=512)
    reported_faults: list[ReportedMotorFault] = Field(default_factory=list, max_length=64)
    observed_at: AwareDatetime | None = None


class ProcessObservation(Model):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    label: str = Field(min_length=1, max_length=64)
    comm: str = Field(min_length=1, max_length=15)
    state: Literal["running", "stopped", "zombie", "ambiguous", "unknown"]
    detail: str = Field(max_length=256)
    pid: int | None = Field(default=None, ge=1)
    start_ticks: int | None = Field(default=None, ge=0)
    kernel_state: str | None = Field(default=None, max_length=1)
    observed_at: AwareDatetime | None = None
    age_ms: NonNegativeFloat | None = None


class MetricRollup(Model):
    last: FiniteFloat | None
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None
    count: int = Field(ge=0)
    age_ms: FiniteFloat | None = Field(default=None, ge=0)
    sum_squares: NonNegativeFloat | None = None
    pairing: Literal["same_sample"] | None = None

    @model_serializer(mode="wrap")
    def compatible_wire_shape(self, handler):
        result = handler(self)
        # Old cached windows must retain their exact canonical shape and hash.
        for name in ("sum_squares", "pairing"):
            if result.get(name) is None:
                result.pop(name, None)
        return result

    @model_validator(mode="after")
    def coherent(self):
        if (self.sum_squares is None) != (self.pairing is None):
            raise ValueError("配对证据与误差平方和必须成对提供")
        if self.sum_squares is not None and (
            self.count == 0
            or self.last is None
            or self.last < 0
            or not math.isclose(
                self.last, math.sqrt(self.sum_squares / self.count), rel_tol=1e-9, abs_tol=1e-12
            )
        ):
            raise ValueError("RMS必须由配对样本的平方和与数量计算")
        if self.count == 0 and any(
            value is not None for value in (self.last, self.minimum, self.maximum)
        ):
            raise ValueError("无新样本的窗口不能伪造数值")
        if self.count > 0 and self.last is None:
            raise ValueError("存在样本时必须提供最后值")
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("极值必须成对提供")
        if self.minimum is not None and not self.minimum <= self.last <= self.maximum:
            raise ValueError("最后值必须落在极值范围内")
        return self


class DataQuality(Model):
    device_profile: dict | None = None
    collector_status: dict = Field(default_factory=dict, max_length=16)

    @field_validator("device_profile")
    @classmethod
    def valid_device_profile(cls, value):
        return RobotProfile.model_validate(value).model_dump() if value is not None else None

    topic_counts: dict[str, NonNegativeInt] = Field(default_factory=dict, max_length=16)
    topic_ages_ms: dict[str, NonNegativeFloat | None] = Field(default_factory=dict, max_length=16)
    publisher_timestamps: dict[str, AwareDatetime | None] = Field(
        default_factory=dict, max_length=16
    )
    invalid_values: int = Field(default=0, ge=0)
    clock_status: Literal["HOST_ANCHORED", "WALL_CLOCK_CHANGED"] = "HOST_ANCHORED"
    time_basis: Literal["host_receive"] = "host_receive"
    cache_discarded_windows: int = Field(default=0, ge=0)
    joint_names: list[str] = Field(default_factory=list, max_length=64)
    target_alignment: TargetAlignment = "unverified"
    mapping_note: str = Field(default="", max_length=256)
    health_sources: dict[str, HealthSourceStatus] = Field(default_factory=dict, max_length=16)
    health_sensors: dict[str, str] = Field(default_factory=dict, max_length=320)
    health_values: dict[MetricKey, FiniteFloat] = Field(default_factory=dict, max_length=512)
    health_ages_ms: dict[MetricKey, NonNegativeFloat] = Field(default_factory=dict, max_length=512)
    processes: list[ProcessObservation] = Field(default_factory=list, max_length=16)


class TelemetryWindow(Model):
    protocol_version: Literal[1] = 1
    robot_id: RobotId
    stream_id: UUID
    sequence: int = Field(ge=1)
    source_kind: SourceKind
    window_start: AwareDatetime
    window_end: AwareDatetime
    metrics: dict[MetricKey, MetricRollup] = Field(max_length=512)
    quality: DataQuality

    @model_validator(mode="after")
    def coherent(self):
        duration = (self.window_end - self.window_start).total_seconds()
        if self.quality.device_profile and self.quality.device_profile["robot_id"] != self.robot_id:
            raise ValueError("设备档案与遥测身份不一致")
        if not 0 < duration <= 86400:
            raise ValueError("汇总窗口必须大于0秒且不超过24小时；实际时长保留，不假设调度永远准时")
        for key, value in self.metrics.items():
            if key.endswith(".tracking_rms") and value.count > 0:
                if (
                    value.pairing != "same_sample"
                    or value.sum_squares is None
                    or value.age_ms is None
                    or value.age_ms > 1000
                    or (
                        self.source_kind == "ros2"
                        and (
                            self.quality.target_alignment == "unverified"
                            or any(
                                self.quality.topic_ages_ms.get(topic) is None
                                or self.quality.topic_ages_ms[topic] > 1000
                                for topic in ("/joint_states", "/action")
                            )
                        )
                    )
                ):
                    raise ValueError("跟踪RMS缺少有效同采样配对证据或数据已过期")
            elif value.sum_squares is not None or value.pairing is not None:
                raise ValueError("仅跟踪RMS允许携带配对误差平方和")
            if (
                metric_spec(key)["aggregation"] == "range"
                and value.count > 0
                and value.minimum is None
            ):
                raise ValueError("范围型指标存在样本时必须提供极值，不能把未知极值当作最后值")
            if metric_spec(key)["aggregation"] == "last" and value.minimum is not None:
                raise ValueError("离散状态与四元数只保留最后值，不计算极值或均值")
        return self


class LiveObservation(Model):
    """Relay receipt evidence, separate from the unchanged robot window/hash."""

    stream_id: UUID
    sequence: int = Field(ge=1)
    collector_sampled_at: AwareDatetime
    relay_observed_at: AwareDatetime
    round_trip_ms: FiniteFloat = Field(ge=0, le=3000)


class TelemetryBatch(Model):
    delivery: Literal["live", "backfill"]
    windows: list[TelemetryWindow] = Field(min_length=1, max_length=120)
    live_observation: LiveObservation | None = None

    @model_validator(mode="after")
    def observation_identity(self):
        if self.live_observation is not None:
            if self.delivery != "live" or len(self.windows) != 1:
                raise ValueError("传输新鲜度证据仅用于单个实时窗口")
            window = self.windows[0]
            if (self.live_observation.stream_id, self.live_observation.sequence) != (
                window.stream_id,
                window.sequence,
            ):
                raise ValueError("传输新鲜度证据必须绑定同一窗口身份")
        return self


class Receipt(Model):
    stream_id: UUID
    sequence: int


class BatchAck(Model):
    receipts: list[Receipt]
    inserted: int
    duplicates: int
    live_ignored: int = 0


class TransferPage(Model):
    robot_id: RobotId
    source_kind: SourceKind
    stream_id: UUID
    sequence: int = Field(ge=0)
    sampled_at: AwareDatetime | None = None
    delivery: Literal["live", "backfill"]
    windows: list[TelemetryWindow] = Field(default_factory=list, max_length=30)


class TransferReceipt(Receipt):
    sequence: int = Field(ge=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TransferAck(Model):
    robot_id: RobotId
    receipts: list[TransferReceipt] = Field(min_length=1, max_length=30)


class RelayStatus(Model):
    robot_id: RobotId
    source_kind: SourceKind
    session_id: UUID
    sequence: int = Field(ge=1)
    observed_at: AwareDatetime
    collector_connected: bool
    pending_windows: int = Field(ge=0)
    pending_bytes: int = Field(ge=0)
    cache_budget_bytes: int = Field(gt=0)
    free_disk_bytes: int | None = Field(default=None, ge=0)
    reserve_bytes: int = Field(ge=0)
    robot_pending_windows: int | None = Field(default=None, ge=0)
    robot_free_disk_bytes: int | None = Field(default=None, ge=0)
    robot_reserve_bytes: int | None = Field(default=None, ge=0)
    robot_observed_at: AwareDatetime | None = None
    robot_disk_observed_at: AwareDatetime | None = None


class PreviewFrame(Model):
    robot_id: RobotId
    stream_id: UUID
    source_kind: SourceKind
    sampled_at: datetime
    values: dict[str, float | None]
    ages_ms: dict[str, float]
    topic_ages_ms: dict[str, float | None]
    catalog: list[dict[str, str]]
    upload: dict = Field(default_factory=dict)
    joint_names: list[str] = Field(default_factory=list)
    target_alignment: TargetAlignment = "unverified"
    mapping_note: str = ""
    health_sources: dict[str, HealthSourceStatus] = Field(default_factory=dict)
    health_sensors: dict[str, str] = Field(default_factory=dict)
    health: dict = Field(default_factory=dict)
    processes: list[ProcessObservation] = Field(default_factory=list, max_length=16)
    device_profile: dict | None = None
    # Host relay adds non-secret central display settings; never part of TelemetryWindow.
    monitoring_configuration: dict | None = None
