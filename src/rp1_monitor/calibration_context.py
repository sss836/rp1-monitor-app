"""Inspect calibration preflight eligibility without reading joint telemetry.

This context permits opening a reviewed preflight, never motor control. A missing
explicit DOF leaves the separate human 24/28 selection to the caller.
"""

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from uuid import UUID


def _local_age(value):
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _aware_timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    return isinstance(value, datetime) and value.utcoffset() is not None


def _valid_dof(value):
    return type(value) is int and value in (24, 28)


def extract_calibration_context(frame, *, elapsed_ms=0, transport_connected=False) -> dict:
    """Return identity, connection and explicit configuration checks only.

    ``elapsed_ms`` is elapsed local monotonic time since receiving this preview;
    the collector timestamp must include a timezone but is not a PC clock check.
    ``configuration_binding`` snapshots (source, revision, raw DOF, health DOF)
    so a caller can reject changes after human confirmation. ``can_preflight``
    does not establish motor readiness, physical DOF or permission to run motors.
    """
    if hasattr(frame, "model_dump"):
        frame = frame.model_dump(mode="json")
    frame = frame if isinstance(frame, Mapping) else {}
    issues = []
    robot_id, source_kind = frame.get("robot_id"), frame.get("source_kind")
    if not isinstance(robot_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", robot_id
    ):
        issues.append("机器人ID未知或无效")
    if source_kind != "ros2":
        issues.append("需要ROS2来源机器人；模拟或未知来源不可标定")
    try:
        stream_id = str(UUID(str(frame.get("stream_id"))))
    except (ValueError, TypeError, AttributeError):
        stream_id = None
        issues.append("原始采集流ID缺失或无效")
    if not _aware_timestamp(frame.get("sampled_at")):
        issues.append("原始采样时间缺失或未带时区")
    elapsed = _local_age(elapsed_ms)
    if elapsed is None or elapsed > 1000:
        issues.append("机器人预览帧本机接收年龄无效或已超过1000ms")

    upload = frame.get("upload")
    upload = upload if isinstance(upload, Mapping) else {}
    connected = upload.get("collector_connected") is True or (
        upload.get("stage") == "robot_buffer" and "collector_connected" not in upload
        and transport_connected is True
    )
    if not connected:
        issues.append("采集连接未确认，禁止标定预检")

    health = frame.get("health")
    health = health if isinstance(health, Mapping) else {}
    motor_map = health.get("motor_map")
    motor_map = motor_map if isinstance(motor_map, Mapping) else {}
    health_dof = motor_map.get("dof")
    if health_dof is not None and not _valid_dof(health_dof):
        issues.append("健康视图自由度无效")

    configured_dof = None
    source = revision = raw_dof = None
    envelope = frame.get("monitoring_configuration")
    if envelope is not None and not isinstance(envelope, Mapping):
        issues.append("监测配置格式无效")
    elif envelope:
        source, revision = envelope.get("source"), envelope.get("revision")
        identity_matches = all(
            envelope.get(key) == expected
            for key, expected in (("robot_id", robot_id), ("source_kind", source_kind))
            if source != "unconfigured" or key in envelope
        )
        if not identity_matches:
            issues.append("监测配置与机器人ID或来源不匹配")
        configuration = envelope.get("configuration")
        raw_dof = configuration.get("dof") if isinstance(configuration, Mapping) else None
        if source == "unconfigured":
            pass  # Its default display DOF is not an explicit robot configuration.
        elif source not in ("database", "local_file"):
            issues.append("监测配置来源未确认，不能据此确定自由度")
        elif not _valid_dof(raw_dof):
            issues.append("已配置自由度无效")
        elif identity_matches:
            configured_dof = raw_dof
        if (source in ("database", "local_file") and _valid_dof(raw_dof)
                and _valid_dof(health_dof) and raw_dof != health_dof):
            issues.append("监测配置与健康视图自由度不一致")

    return dict(
        robot_id=robot_id, source_kind=source_kind, stream_id=stream_id,
        connected=connected, elapsed_ms=elapsed, configured_dof=configured_dof,
        health_dof=deepcopy(health_dof),
        configuration_binding=deepcopy((source, revision, raw_dof, health_dof)),
        issues=issues, can_preflight=not issues,
    )
