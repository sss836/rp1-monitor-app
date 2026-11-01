"""Read-only posture snapshots. Receive freshness never verifies motor hardware.

The live input is PreviewFrame (or its JSON dict), not a rollup window. All
calculations retain radians. A reference is a local observation, never a target
or a tracking-RMS sample, and no angle wrapping is applied to its deviation.
"""

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

MAX_REFERENCE_BYTES = 128 * 1024
STATUSES = {
    "fresh": "新鲜接收值；硬件反馈真实性未验证",
    "stale": "位置接收值已过期",
    "missing": "未提供位置",
    "invalid": "位置不是有限数值",
    "unknown_age": "位置接收年龄未知或无效",
    "disconnected": "采集连接未确认",
}
_REFERENCE_FIELDS = {
    "schema_version", "robot_id", "source_kind", "stream_id", "sampled_at", "saved_at",
    "joint_names", "joint_count", "dof", "positions_rad", "ages_ms", "elapsed_ms",
    "topic_ages_ms", "max_age_ms", "unit", "time_basis", "freshness_basis",
    "hardware_feedback_verified",
}


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _age(value):
    return float(value) if _number(value) and value >= 0 else None


def _timestamp(value):
    if isinstance(value, datetime):
        parsed = value
        text = value.isoformat()
    elif isinstance(value, str):
        text = value
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("时间必须是含时区的ISO 8601时间") from error
    else:
        raise ValueError("时间必须是含时区的ISO 8601时间")
    if parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区，不能推测机器人或上位机时区")
    return text


def angle_value(radians, unit="deg"):
    """Convert a finite radian value for display, without modifying the source."""
    if unit not in {"rad", "deg"}:
        raise ValueError("角度单位只支持deg或rad")
    if not _number(radians):
        return None
    converted = float(radians) if unit == "rad" else math.degrees(radians)
    return converted if math.isfinite(converted) else None


def format_angle(radians, unit="deg", *, signed=False, precision=2):
    value = angle_value(radians, unit)
    return "—" if value is None else format(value, f"{'+' if signed else ''}.{precision}f")


def extract_joint_pose(frame, *, dof=None, elapsed_ms=0, max_age_ms=1000, transport_connected=False):
    """Build rows including missing joints; elapsed_ms is local monotonic time.

    Explicit dof is a display selection, never a robot configuration write.
    Confirmed relay or live direct-reader connections require complete fresh positions.
    Names are retained in wire order; unnamed simulation uses stable indices.
    """
    if dof is not None and (type(dof) is not int or dof not in (24, 28)):
        raise ValueError("自由度只支持24或28")
    if _age(elapsed_ms) is None or _age(max_age_ms) is None or not 0 < max_age_ms <= 1000:
        raise ValueError("本机经过时间必须非负，新鲜度上限必须在0至1000ms内")
    if hasattr(frame, "model_dump"):
        frame = frame.model_dump(mode="json")
    frame = frame if isinstance(frame, Mapping) else {}
    values, ages = frame.get("values") or {}, frame.get("ages_ms") or {}
    topics = frame.get("topic_ages_ms") or {}
    issues = []
    robot_id, source_kind = frame.get("robot_id"), frame.get("source_kind")
    if not isinstance(robot_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", robot_id):
        issues.append("机器人ID未知或无效")
    if source_kind not in ("simulated", "ros2"):
        issues.append("数据来源未知")
    try:
        sampled_at = _timestamp(frame.get("sampled_at"))
    except ValueError:
        sampled_at = None
        issues.append("原始采样时间缺失或未带时区")
    try:
        stream_id = str(UUID(str(frame.get("stream_id"))))
    except ValueError:
        stream_id = None
        issues.append("原始采集流ID缺失或无效")
    names = frame.get("joint_names") or []
    if not isinstance(names, list) or len(names) > 64 or any(
        not isinstance(name, str) or not name.strip() or len(name) > 120 for name in names
    ) or len(set(names)) != len(names):
        names = []
        issues.append("关节名称必须非空、唯一且按原始索引排列")
    names = list(names)
    if source_kind == "ros2" and not names:
        issues.append("ROS2关节名称未知，无法核对参考姿态映射")

    configured_dof = None
    envelope = frame.get("monitoring_configuration") or {}
    if envelope.get("source") in {"database", "local_file"}:
        if (envelope.get("robot_id"), envelope.get("source_kind")) != (robot_id, source_kind):
            issues.append("监测配置与机器人ID或来源不匹配")
        else:
            configured_dof = (envelope.get("configuration") or {}).get("dof")
    health_dof = ((frame.get("health") or {}).get("motor_map") or {}).get("dof")
    if configured_dof is None:
        configured_dof = health_dof
    elif health_dof is not None and health_dof != configured_dof:
        issues.append("监测配置与健康视图自由度不一致")
    if configured_dof is not None and (type(configured_dof) is not int or configured_dof not in (24, 28)):
        issues.append("已配置自由度无效")
        configured_dof = None
    if dof is not None and configured_dof is not None and dof != configured_dof:
        issues.append("所选自由度与监测配置不一致")
    selected_dof = dof if dof is not None else configured_dof
    indices = [
        int(match[1]) for key in set(values) | set(ages)
        if (match := re.fullmatch(r"joint\.(0|[1-9][0-9]?)\.position", key))
    ]
    observed_count = max(indices, default=-1) + 1
    count = max(selected_dof or 0, len(names), observed_count)
    if count not in (24, 28):
        issues.append("关节清单尚未完整确定为24或28自由度")
    if selected_dof and (observed_count > selected_dof or names and len(names) != selected_dof):
        issues.append("观测关节清单与自由度配置不一致")
    if names and observed_count > len(names):
        issues.append("位置索引超出原始关节名称清单")
    mapping_valid = not issues
    count = min(count, 64)
    upload = frame.get("upload") or {}
    connected = upload.get("collector_connected") is True or (
        upload.get("stage") == "robot_buffer" and "collector_connected" not in upload
        and transport_connected is True
    )
    if not connected:
        issues.append("采集连接未确认，禁止保存参考姿态")
    raw_topic_age = _age(topics.get("/joint_states"))
    topic_age = raw_topic_age + elapsed_ms if raw_topic_age is not None else None
    if topic_age is None or topic_age > max_age_ms:
        issues.append("/joint_states接收新鲜度未知或已过期")
    rows = []
    for index in range(count):
        key = f"joint.{index}.position"
        raw, raw_age = values.get(key), _age(ages.get(key))
        age = raw_age + elapsed_ms if raw_age is not None else None
        status = (
            "disconnected" if not connected else
            "missing" if raw is None else
            "invalid" if not _number(raw) else
            "unknown_age" if age is None else
            "stale" if age > max_age_ms else "fresh"
        )
        rows.append(dict(
            index=index, name=names[index] if index < len(names) else f"关节 {index}",
            position_rad=float(raw) if _number(raw) else None,
            raw_age_ms=raw_age, age_ms=age, status=status, reason=STATUSES[status],
        ))
    if any(row["status"] != "fresh" for row in rows):
        issues.append("参考姿态要求全部关节位置完整、有限且接收新鲜")
    return dict(
        robot_id=robot_id, source_kind=source_kind, stream_id=stream_id,
        sampled_at=sampled_at, joint_names=names, dof=selected_dof, joint_count=count,
        connected=connected, mapping_valid=mapping_valid, rows=rows, elapsed_ms=float(elapsed_ms),
        max_age_ms=float(max_age_ms), topic_ages_ms={"/joint_states": raw_topic_age},
        topic_age_ms=topic_age, time_basis="host_receive", hardware_feedback_verified=False,
        can_save=not issues, issues=issues,
    )


def _validate_reference(value):
    if not isinstance(value, dict) or set(value) != _REFERENCE_FIELDS:
        raise ValueError("参考姿态字段不完整或包含未知字段")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("仅支持版本1参考姿态")
    robot_id = value["robot_id"]
    if not isinstance(robot_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", robot_id):
        raise ValueError("参考姿态机器人ID无效")
    if value["source_kind"] not in ("simulated", "ros2"):
        raise ValueError("参考姿态来源无效")
    try:
        UUID(value["stream_id"])
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("参考姿态采集流ID无效") from error
    _timestamp(value["sampled_at"])
    _timestamp(value["saved_at"])
    count, dof, names = value["joint_count"], value["dof"], value["joint_names"]
    if type(count) is not int or count not in (24, 28) or dof is not None and (type(dof) is not int or dof != count):
        raise ValueError("参考姿态自由度或关节数量无效")
    if not isinstance(names, list) or len(names) not in (0, count) or any(
        not isinstance(name, str) or not name.strip() or len(name) > 120 for name in names
    ) or len(set(names)) != len(names) or value["source_kind"] == "ros2" and not names:
        raise ValueError("参考姿态有序关节名称清单无效")
    positions, ages = value["positions_rad"], value["ages_ms"]
    if not isinstance(positions, list) or len(positions) != count or not all(_number(v) for v in positions):
        raise ValueError("参考姿态必须包含每个索引的有限rad位置")
    limit, elapsed = value["max_age_ms"], value["elapsed_ms"]
    if _age(limit) is None or not 0 < limit <= 1000 or _age(elapsed) is None:
        raise ValueError("参考姿态新鲜度口径无效")
    if not isinstance(ages, list) or len(ages) != count or any(
        _age(age) is None or age + elapsed > limit for age in ages
    ):
        raise ValueError("参考姿态包含缺失或过期的位置接收年龄")
    topics = value["topic_ages_ms"]
    if not isinstance(topics, dict) or set(topics) != {"/joint_states"} or (
        _age(topics["/joint_states"]) is None or topics["/joint_states"] + elapsed > limit
    ):
        raise ValueError("参考姿态缺少新鲜的/joint_states接收证据")
    if (value["unit"], value["time_basis"], value["freshness_basis"]) != (
        "rad", "host_receive", "reported_age_plus_pc_monotonic_elapsed"
    ) or value["hardware_feedback_verified"] is not False:
        raise ValueError("参考姿态单位、时间口径或硬件验证声明无效")
    return value


def make_reference(pose, *, saved_at=None):
    """Capture only a complete, valid, freshly received live pose."""
    if not pose.get("can_save") or not pose.get("connected") or any(
        row["status"] != "fresh" for row in pose["rows"]
    ):
        raise ValueError("；".join(pose.get("issues", [])) or "当前姿态不能保存为参考")
    if [row["index"] for row in pose["rows"]] != list(range(pose["joint_count"])):
        raise ValueError("当前姿态行必须按完整关节索引排列")
    return _validate_reference(dict(
        schema_version=1, robot_id=pose["robot_id"], source_kind=pose["source_kind"],
        stream_id=pose["stream_id"], sampled_at=pose["sampled_at"],
        saved_at=_timestamp(saved_at if saved_at is not None else datetime.now(timezone.utc)),
        joint_names=list(pose["joint_names"]), joint_count=pose["joint_count"], dof=pose["dof"],
        positions_rad=[row["position_rad"] for row in pose["rows"]],
        ages_ms=[row["raw_age_ms"] for row in pose["rows"]], elapsed_ms=pose["elapsed_ms"],
        topic_ages_ms=dict(pose["topic_ages_ms"]), max_age_ms=pose["max_age_ms"],
        unit="rad", time_basis="host_receive",
        freshness_basis="reported_age_plus_pc_monotonic_elapsed", hardware_feedback_verified=False,
    ))


def reference_json(reference):
    return json.dumps(_validate_reference(reference), ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("参考姿态JSON字段不能重复")
        result[key] = value
    return result


def load_reference(text):
    """Parse local JSON only. Schema validation does not verify hardware truth."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_REFERENCE_BYTES:
        raise ValueError("参考姿态JSON超过大小限制或不是文本")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("参考姿态不是有效JSON") from error
    return _validate_reference(value)


def save_reference(path, pose, *, saved_at=None):
    reference = make_reference(pose, saved_at=saved_at)
    Path(path).write_text(reference_json(reference), encoding="utf-8")
    return reference


def compare_joint_pose(pose, reference=None):
    """Return direct current-minus-reference radians for fresh compatible rows."""
    reason = "尚未加载参考姿态"
    compatible = False
    if reference is not None:
        try:
            _validate_reference(reference)
        except ValueError as error:
            reason = str(error)
        else:
            mismatches = [
                label for key, label in (("robot_id", "机器人ID"), ("source_kind", "数据来源"),
                                         ("joint_count", "关节数量"), ("joint_names", "有序关节名称映射"))
                if pose.get(key) != reference[key]
            ]
            if pose.get("dof") is not None and reference["dof"] is not None and pose["dof"] != reference["dof"]:
                mismatches.append("自由度配置")
            compatible = not mismatches
            reason = "参考身份与关节映射一致" if compatible else "参考不匹配：" + "、".join(mismatches)
            if not pose.get("mapping_valid"):
                compatible = False
                reason = "当前姿态身份、时间或关节映射无效"
    rows = []
    for row in pose["rows"]:
        baseline = reference["positions_rad"][row["index"]] if compatible else None
        usable = (
            compatible and pose.get("connected") and row["status"] == "fresh"
            and pose.get("topic_age_ms") is not None
            and pose["topic_age_ms"] <= pose["max_age_ms"]
        )
        rows.append({**row, "reference_rad": baseline,
                     "deviation_rad": row["position_rad"] - baseline if usable else None})
    return dict(compatible=compatible, reason=reason, rows=rows)
