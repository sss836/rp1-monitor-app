"""Offline obstruction detection and URDF comparison; never sends motor commands.

Inputs must already be in the URDF joint coordinate system. A detected load
plateau is a candidate obstruction, not proof of a mechanical stop or zeroing.
All detection thresholds are supplied by the recording, with no hardware defaults.
"""

import argparse
import hashlib
import json
import math
import statistics
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

Number = StrictFloat
Positive = Annotated[StrictFloat, Field(gt=0)]
Nonnegative = Annotated[StrictFloat, Field(ge=0)]
MAX_URDF_BYTES = 4 * 1024 * 1024
MAX_RECORDING_BYTES = 16 * 1024 * 1024


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class DetectionSettings(_Model):
    hold_s: Positive
    max_gap_s: Positive
    max_feedback_age_ms: Positive
    position_span_rad: Positive
    minimum_motion_rad: Positive
    command_progress_rad: Positive
    contact_torque_nm: Positive
    torque_rise_nm: Positive
    abort_torque_nm: Positive
    max_duration_s: Positive
    max_travel_rad: Positive

    @model_validator(mode="after")
    def coherent(self):
        if self.contact_torque_nm >= self.abort_torque_nm:
            raise ValueError("接触判据必须低于中止力矩")
        if self.minimum_motion_rad <= self.position_span_rad:
            raise ValueError("先前运动量必须大于静止判据")
        if self.max_travel_rad <= self.minimum_motion_rad:
            raise ValueError("最大行程必须大于先前运动量")
        if self.max_duration_s <= self.hold_s or self.max_gap_s > self.hold_s:
            raise ValueError("持续窗口、最大反馈间隔和总时长不一致")
        return self


class LimitSample(_Model):
    time_s: Nonnegative
    feedback_sequence: Annotated[StrictInt, Field(ge=0)]
    position_rad: Number
    torque_nm: Number
    command_position_rad: Number
    position_age_ms: Nonnegative
    torque_age_ms: Nonnegative
    drive_fault: StrictBool


class LimitTrace(_Model):
    direction: Literal["lower", "upper"]
    settings: DetectionSettings
    samples: Annotated[list[LimitSample], Field(min_length=1, max_length=100_000)]


class LimitRecording(_Model):
    schema_version: Literal[1]
    robot_id: Annotated[str, Field(min_length=1, max_length=128)]
    source_kind: Literal["ros2", "simulated"]
    joint_name: Annotated[str, Field(min_length=1, max_length=128)]
    calibration_id: Annotated[str, Field(min_length=1, max_length=128)]
    coordinate_frame: Literal["urdf_joint"]
    time_basis: Literal["robot_monotonic"]
    position_unit: Literal["rad"]
    torque_unit: Literal["Nm"]
    mapping_verified: StrictBool
    torque_feedback_verified: StrictBool
    mechanical_reference_verified: StrictBool
    urdf_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    tolerance_rad: Positive
    traces: Annotated[list[LimitTrace], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def distinct_directions(self):
        if len({trace.direction for trace in self.traces}) != len(self.traces):
            raise ValueError("同一记录中的方向不能重复")
        return self


def load_urdf_limits(text):
    """Read explicit revolute-joint limits, without executing xacro or loading meshes."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_URDF_BYTES:
        raise ValueError("URDF不是文本或超过4MiB")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("不接受URDF中的DTD或实体")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("URDF XML无效") from error
    if root.tag != "robot":
        raise ValueError("需要已展开的URDF robot文件，不执行xacro")
    joints, names = {}, set()
    for joint in root.findall("joint"):
        name = joint.get("name")
        if not name or name in names:
            raise ValueError("URDF关节名称缺失或重复")
        names.add(name)
        if joint.get("type") != "revolute" or joint.find("mimic") is not None:
            continue
        limit = joint.find("limit")
        if limit is None:
            raise ValueError("旋转关节缺少limit：" + name)
        try:
            lower, upper = float(limit.attrib["lower"]), float(limit.attrib["upper"])
        except (KeyError, ValueError) as error:
            raise ValueError("需要显式的有限lower/upper：" + name) from error
        if not all(math.isfinite(v) for v in (lower, upper)) or lower >= upper:
            raise ValueError("URDF关节范围无效：" + name)
        joints[name] = {"lower_rad": lower, "upper_rad": upper}
    return joints


class ObstructionDetector:
    """Consume one direction's fresh, distinct feedback; terminal results latch.

    request_stop is an instruction for a future local executor, not a claim that
    a motor stopped. This module deliberately has no SSH, driver or ROS imports.
    """

    def __init__(self, settings: DetectionSettings, direction):
        if direction not in {"lower", "upper"}:
            raise ValueError("方向必须为lower或upper")
        self.settings = settings
        self.direction = direction
        self.sign = -1 if direction == "lower" else 1
        self.first = self.previous = self.result = None
        self.window = deque()
        self.moved = False

    def _finish(self, status, reason, sample, **extra):
        self.result = dict(status=status, reason=reason, time_s=sample.time_s,
                           direction=self.direction, request_stop=True, **extra)
        return self.result

    def feed(self, sample: LimitSample):
        if self.result:
            return self.result
        p = self.settings
        if (sample.drive_fault or max(sample.position_age_ms, sample.torque_age_ms)
                > p.max_feedback_age_ms):
            return self._finish("aborted", "故障或反馈过期", sample)
        if abs(sample.torque_nm) >= p.abort_torque_nm:
            return self._finish("aborted", "达到中止力矩", sample)
        if self.previous:
            dt = sample.time_s - self.previous.time_s
            if dt <= 0 or dt > p.max_gap_s or sample.feedback_sequence <= self.previous.feedback_sequence:
                return self._finish("aborted", "反馈重复、倒序或中断", sample)
            # Do not mistake +/-pi wrapping, discontinuities or contrary motion for a stop.
            step = self.sign * (sample.position_rad - self.previous.position_rad)
            if step < -p.position_span_rad:
                return self._finish("aborted", "反向运动或角度坐标跳变", sample)
        if self.first is None:
            self.first = sample
        travel = self.sign * (sample.position_rad - self.first.position_rad)
        if abs(sample.position_rad - self.first.position_rad) >= p.max_travel_rad:
            return self._finish("aborted", "达到最大检查行程", sample)
        if sample.time_s - self.first.time_s >= p.max_duration_s:
            return self._finish("aborted", "达到最大检查时长", sample)
        self.moved |= travel >= p.minimum_motion_rad
        self.previous = sample
        self.window.append(sample)
        # Keep the sample immediately before the window boundary to prove its duration.
        while len(self.window) > 2 and self.window[1].time_s <= sample.time_s - p.hold_s:
            self.window.popleft()
        start = self.window[0]
        if (not self.moved or sample.time_s - start.time_s < p.hold_s
                or len(self.window) < 3):
            return None
        positions = [item.position_rad for item in self.window]
        torques = [abs(item.torque_nm) for item in self.window]
        commands = [self.sign * item.command_position_rad for item in self.window]
        if (max(positions) - min(positions) <= p.position_span_rad
                and min(torques) >= p.contact_torque_nm
                and min(torques) - abs(self.first.torque_nm) >= p.torque_rise_nm
                and commands[-1] - commands[0] >= p.command_progress_rad
                and all(b >= a for a, b in zip(commands, commands[1:]))):
            return self._finish(
                "candidate_obstruction", "已运动后出现持续高负载且位置停滞；原因待确认", sample,
                position_rad=statistics.median(positions),
                position_span_rad=max(positions) - min(positions),
                peak_torque_nm=max(torques), window_start_s=start.time_s,
            )
        return None


def analyze_recording(recording, urdf_text):
    """Replay evidence and compare endpoints; never certifies calibration accuracy."""
    if not isinstance(recording, LimitRecording):
        recording = LimitRecording.model_validate(recording)
    actual_hash = hashlib.sha256(urdf_text.encode("utf-8")).hexdigest()
    if recording.urdf_sha256 != actual_hash:
        raise ValueError("记录绑定的URDF哈希与当前文件不一致")
    limits = load_urdf_limits(urdf_text)
    if recording.joint_name not in limits:
        raise ValueError("所选关节没有可比对的独立旋转限位；不猜测连续、mimic或缺失关节")
    expected = limits[recording.joint_name]
    results, offsets = [], {}
    for trace in recording.traces:
        detector = ObstructionDetector(trace.settings, trace.direction)
        outcome = None
        consumed = 0
        for consumed, sample in enumerate(trace.samples, 1):
            outcome = detector.feed(sample)
            if outcome:
                break
        if outcome is None:
            outcome = dict(status="incomplete", reason="记录结束，未取得持续阻挡候选证据",
                           direction=trace.direction, request_stop=True)
        outcome = {**outcome, "consumed_samples": consumed,
                   "remaining_samples": len(trace.samples) - consumed,
                   "expected_rad": expected[trace.direction + "_rad"]}
        if outcome["status"] == "candidate_obstruction":
            offset = outcome["position_rad"] - outcome["expected_rad"]
            outcome.update(deviation_rad=offset,
                           within_tolerance=abs(offset) <= recording.tolerance_rad)
            offsets[trace.direction] = offset
        results.append(outcome)
    qualified = (recording.mapping_verified and recording.torque_feedback_verified
                 and recording.mechanical_reference_verified)
    status = "insufficient_evidence"
    single_endpoint_comparison = None
    shift = span_error = None
    if len(offsets) == 2:
        shift = (offsets["lower"] + offsets["upper"]) / 2
        span_error = offsets["upper"] - offsets["lower"]
        if qualified:
            status = (
                "endpoints_within_tolerance"
                if all(abs(value) <= recording.tolerance_rad for value in offsets.values()) else
                "possible_zero_offset" if abs(span_error) <= recording.tolerance_rad else
                "range_mismatch"
            )
    elif len(offsets) == 1:
        direction, offset = next(iter(offsets.items()))
        if qualified:
            status = "single_endpoint_only"
        single_endpoint_comparison = dict(
            direction=direction,
            observed_minus_urdf_rad=offset,
            within_reference_tolerance=(abs(offset) <= recording.tolerance_rad) if qualified else None,
            basis="candidate_obstruction",
            physical_stop_confirmed=False,
            interpretation=(
                "单侧候选阻挡位置与模型端点的差值；未证明阻挡是机械止挡，"
                "不能排除模型或传动误差，不能据此单独认证标零通过。"
            ),
        )
    return dict(
        schema_version=1, robot_id=recording.robot_id, source_kind=recording.source_kind,
        joint_name=recording.joint_name, calibration_id=recording.calibration_id,
        urdf_sha256=actual_hash, expected=expected, tolerance_rad=recording.tolerance_rad,
        mapping_verified=recording.mapping_verified,
        torque_feedback_verified=recording.torque_feedback_verified,
        mechanical_reference_verified=recording.mechanical_reference_verified,
        status=status, zero_shift_estimate_rad=shift, range_error_rad=span_error,
        single_endpoint_comparison=single_endpoint_comparison,
        directions=results, calibration_passed=None, execution_performed=False,
        limitations=[
            "仅回放分析，没有发送指令；停止请求不证明电机实际停止。",
            "高负载静止可能是碰撞、卡滞或控制器限幅，不自动认定为机械限位。",
            "URDF限位不自动等于实物止挡；Nm反馈、同一标零状态和坐标映射需确认。",
            "双端一致只能支持范围/偏移一致性，不能独立证明绝对标零精度。",
            *( ["simulated：仅用于软件验证，不是真机验收。"]
               if recording.source_kind == "simulated" else []),
        ],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="限位证据回放与URDF比对；不连接机器人")
    parser.add_argument("--urdf", required=True)
    parser.add_argument("--recording", required=True)
    args = parser.parse_args(argv)
    try:
        if Path(args.urdf).stat().st_size > MAX_URDF_BYTES:
            raise ValueError("URDF超过4MiB")
        if Path(args.recording).stat().st_size > MAX_RECORDING_BYTES:
            raise ValueError("回放文件超过16MiB")
        result = analyze_recording(
            json.loads(Path(args.recording).read_text(encoding="utf-8")),
            Path(args.urdf).read_text(encoding="utf-8"),
        )
    except (OSError, ValueError) as error:
        parser.exit(2, "无法分析：" + str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
