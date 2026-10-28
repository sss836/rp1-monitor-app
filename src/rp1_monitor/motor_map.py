"""Shared, display-only motor heat map. No inferred physical joint assignments."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Coordinates refer to the existing 960 × 1707 brand illustration, not robot pose.
REGIONS = {
    "left_shoulder": ("左肩", 0.71, 0.28),
    "left_elbow": ("左肘", 0.77, 0.41),
    "left_wrist": ("左腕", 0.78, 0.55),
    "right_shoulder": ("右肩", 0.33, 0.27),
    "right_elbow": ("右肘", 0.34, 0.40),
    "right_wrist": ("右腕", 0.16, 0.34),
    "left_hip": ("左髋", 0.65, 0.55),
    "left_knee": ("左膝", 0.65, 0.72),
    "left_ankle": ("左踝", 0.65, 0.90),
    "right_hip": ("右髋", 0.40, 0.53),
    "right_knee": ("右膝", 0.40, 0.70),
    "right_ankle": ("右踝", 0.40, 0.87),
}
Region = Literal[tuple(REGIONS)]


class JointLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=63)
    joint_name: str | None = Field(default=None, min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=80)
    region: Region | None = None
    bus: str | None = Field(default=None, min_length=1, max_length=64)
    motor_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("joint_name", "label", "bus", "motor_id")
    @classmethod
    def nonblank_text(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("关节名称、标签、总线或编号不能只包含空白")
        return value

    @model_validator(mode="after")
    def paired_address(self):
        if (self.bus is None) != (self.motor_id is None):
            raise ValueError("总线与电机编号必须成对填写")
        return self


class RobotMotorMap(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source_kind: Literal["simulated", "ros2"]
    dof: Literal[24, 28] | None = None
    physical_mapping_verified: bool = False
    scale_min_c: float
    scale_max_c: float
    joints: list[JointLocation] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_mapping(self):
        if self.scale_max_c <= self.scale_min_c:
            raise ValueError("色阶最高温度必须大于最低温度；色阶不是安全限值")
        if len({item.index for item in self.joints}) != len(self.joints):
            raise ValueError("一个关节只能配置一个部位")
        if self.dof is not None and any(item.index >= self.dof for item in self.joints):
            raise ValueError("关节索引超出已配置的24/28自由度清单")
        if self.source_kind == "ros2" and any(not item.joint_name for item in self.joints):
            raise ValueError("ROS2部位映射必须绑定实际关节名称，不能只猜索引")
        return self


class MotorMapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    robots: dict[str, RobotMotorMap] = Field(default_factory=dict, max_length=256)

    @classmethod
    def load(cls, path: Path | None):
        return cls.model_validate_json(path.read_text()) if path else cls()


def temperature_color(value, low, high):
    """Continuous shared palette, clamped at the explicitly configured display range."""
    stops = ((255, 245, 240), (255, 207, 184), (239, 107, 110), (142, 18, 53))
    position = max(0.0, min(1.0, (value - low) / (high - low))) * 3
    index = min(2, int(position))
    mix = position - index
    rgb = [round(a + (b - a) * mix) for a, b in zip(stops[index], stops[index + 1])]
    return "#" + "".join(f"{channel:02x}" for channel in rgb)


def text_color(color):
    def luminance(hex_color):
        channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    background, dark = luminance(color), luminance("#142441")
    return (
        "#142441"
        if (background + 0.05) / (dark + 0.05) >= 1.05 / (background + 0.05)
        else "#ffffff"
    )


def build_motor_map(robot_id, source_kind, health, config, limits):
    mapping = config.robots.get(robot_id)
    if mapping and mapping.source_kind != source_kind:
        mapping = None
    locations = {item.index: item for item in mapping.joints} if mapping else {}
    observed = {item["joint"]: item for item in health.get("motors", {}).get("items", [])}
    dof_mismatch = bool(mapping and mapping.dof and any(index >= mapping.dof for index in observed))
    mapping_ready = (
        bool(mapping)
        and (source_kind == "simulated" or mapping.physical_mapping_verified)
        and not dof_mismatch
    )
    rows = []
    expected = set(range(mapping.dof)) if mapping and mapping.dof else set()
    for index in sorted(set(observed) | set(locations) | expected):
        item, location = observed.get(index, {}), locations.get(index)
        name_matches = bool(location) and (
            not location.joint_name or location.joint_name == item.get("name")
        )
        region = location.region if mapping_ready and name_matches else None
        readings = [
            {
                "metric": f"joint.{index}.{metric}",
                "label": title,
                "value": item.get(field),
                "limit_c": limits.get(f"joint.{index}.{metric}"),
            }
            for metric, field, title in (
                ("motor_temperature", "temperature", "电机"),
                ("mos_temperature", "mos_temperature", "MOS"),
            )
        ]
        configured = [r for r in readings if r["limit_c"] is not None]
        for reading in readings:
            color = (
                temperature_color(reading["value"], mapping.scale_min_c, mapping.scale_max_c)
                if mapping and reading["value"] is not None and reading["limit_c"] is not None
                else "#c5ceda"
            )
            reading.update(color=color, text_color=text_color(color))
        hot = [r for r in configured if r["value"] is not None and r["value"] > r["limit_c"]]
        known = [r for r in configured if r["value"] is not None]
        state = (
            "hot"
            if hot
            else "unconfigured"
            if not configured
            else "unknown"
            if len(known) < len(configured)
            else "below"
        )
        # The picture represents the hottest configured sensor of this motor.
        value = max((r["value"] for r in known), default=None)
        color = (
            temperature_color(value, mapping.scale_min_c, mapping.scale_max_c)
            if mapping and value is not None and state in {"hot", "below"}
            else "#c5ceda"
        )
        status = {
            "hot": "超温",
            "below": "未超过配置限值",
            "unknown": "数据缺失 / 已过期",
            "unconfigured": "温度限值未配置",
        }[state]
        evidence = [
            f"{r['label']} {'—' if r['value'] is None else format(r['value'], '.1f')} °C / 限值 {'未配置' if r['limit_c'] is None else format(r['limit_c'], '.1f') + ' °C'}"
            for r in readings
        ]
        if region:
            location_note = ""
        elif not location or location.region is None:
            location_note = "部位映射未配置"
        elif not name_matches:
            location_note = "关节名称与映射不一致"
        elif dof_mismatch:
            location_note = "自由度配置与观测清单不一致"
        else:
            location_note = "实机部位映射待确认"
        rows.append(
            {
                "joint": index,
                "name": item.get("name", str(index)),
                "label": location.label if location else item.get("name", f"关节 {index}"),
                "region": region,
                "state": state,
                "status": status,
                "color": color,
                "text_color": text_color(color),
                "value": value,
                "readings": readings,
                "detail": " · ".join(
                    [*evidence, status, *([location_note] if location_note else [])]
                ),
            }
        )
    regions = []
    for key, (label, x, y) in REGIONS.items():
        members = [r for r in rows if r["region"] == key]
        if not members:
            continue
        valid = [r for r in members if r["state"] in {"hot", "below"}]
        hottest = max(valid, key=lambda r: r["value"], default=None)
        state = (
            "hot"
            if any(r["state"] == "hot" for r in members)
            else "unknown"
            if len(valid) < len(members)
            else "below"
        )
        color = hottest["color"] if hottest and state != "unknown" else "#c5ceda"
        regions.append(
            {
                "key": key,
                "label": label,
                "x": x,
                "y": y,
                "state": state,
                "color": color,
                "text_color": text_color(color),
                "value": hottest["value"] if hottest else None,
                "detail": "\n".join(f"{r['label']} · {r['detail']}" for r in members),
            }
        )
    hot_count = sum(row["state"] == "hot" for row in rows)
    unknown_count = sum(row["state"] in {"unknown", "unconfigured"} for row in rows)
    mapped_count = sum(row["region"] is not None for row in rows)
    dof = mapping.dof if mapping else None
    return {
        "rows": rows,
        "regions": regions,
        "hot_count": hot_count,
        "unknown_count": unknown_count,
        "scale_min_c": mapping.scale_min_c if mapping else None,
        "scale_max_c": mapping.scale_max_c if mapping else None,
        "dof": dof,
        "observed_joints": len(observed),
        "mapped_joints": mapped_count,
        "summary": (f"{dof} 自由度 · " if dof else "")
        + f"{hot_count} 台电机超温 · {unknown_count} 台温度判据未知",
        "mapping_coverage": f"已映射 {mapped_count} / {len(rows)} 个关节",
        "mapping_note": "观测关节清单超出自由度配置，部位映射已停用"
        if dof_mismatch
        else "模拟部位映射 · 非实机"
        if mapping and source_kind == "simulated"
        else "实机部位映射已人工确认"
        if mapping_ready
        else "未配置或未确认部位映射，图上不推断电机位置",
        "meaning": "颜色越深温度越高；描边和“超温”文字表示超过配置限值。灰色为未知。色阶仅用于显示，不是安全限值。同一部位显示已知最高温度，逐电机数据见列表。",
    }


def attach_motor_map(health, view):
    """Enrich the transient view only; raw telemetry and fault codes stay intact."""
    health["motor_map"] = view
    if view["dof"] and len(view["rows"]) > health["motors"]["total"]:
        health["motors"]["total"] = len(view["rows"])
        if health["motors"]["state"] != "fault":
            health["motors"].update(state="incomplete", label="电机反馈未覆盖完整自由度配置")
        if health["overall"]["state"] != "fault":
            health["overall"].update(state="incomplete", label="健康监测不完整")
    configured = any(r["limit_c"] is not None for row in view["rows"] for r in row["readings"])
    if configured:
        health["motors"]["detail"] = health["motors"]["detail"].replace(
            "温度限值尚未配置。", "电机温度按本次配置限值判断。"
        )
        health["overall"]["detail"] = health["overall"]["detail"].replace(
            "未配置温度、电压等判据，不给出健康结论。",
            "电机温度按本次配置限值显示；其他判据待核对，不给出整机健康结论。",
        )
    if view["hot_count"]:
        had_fault = health["overall"]["state"] == "fault"
        health["motors"].update(
            state="fault",
            label="存在电机超温及故障上报" if health["motors"]["faults"] else "存在电机超温",
        )
        health["overall"].update(
            state="fault", label="存在电机超温及故障/保护上报" if had_fault else "存在电机超温"
        )
        health["overall"]["detail"] = view["summary"] + "。" + health["overall"]["detail"]
