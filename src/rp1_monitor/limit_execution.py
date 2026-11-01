"""Local, injectable limit-control state machine. CLI ships a simulated driver only.

No ROS, SSH, CAN or deploy SDK is imported here. A real adapter must prove hardware
feedback provenance and independent stop behaviour before it can be connected.
"""

import argparse
import hashlib
import json
import math
import signal
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .current_limits import (
    CurrentDetectionSettings,
    CurrentObstructionDetector,
    CurrentSample,
    compare_current_endpoint,
)
from .gentle_motion import SmoothMove
from .joint_limits import MAX_URDF_BYTES, load_urdf_limits

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]
Finite = Annotated[float, Field(allow_inf_nan=False, strict=True)]
HARDWARE_BLOCKERS = [
    "只提供模拟驱动；尚未接入真实CAN接收时间与逐电机电流证据",
    "尚未核实驱动器断联保护及实际停止确认",
    "尚未验证运控互斥、闭链原始电流与关节位置的同步关系",
    "合成样例参数不是RP1实机参数；动作需要另行现场确认",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class JointTask(_Model):
    joint_name: Annotated[str, Field(min_length=1, max_length=128)]
    direction: Literal["lower", "upper"]
    group: Annotated[int, Field(ge=0, strict=True)]
    start_rad: Finite
    near_rad: Finite
    search_end_rad: Finite
    simulated_stop_rad: Finite
    retreat_rad: Positive
    approach_speed_rad_s: Positive
    search_speed_rad_s: Positive
    acceleration_rad_s2: Positive
    jerk_rad_s3: Positive
    max_segment_rad: Positive
    max_tracking_error_rad: Positive
    max_command_step_rad: Positive
    max_search_overtravel_rad: Positive
    detection: CurrentDetectionSettings
    torque_estimation_model: Annotated[int, Field(ge=0, le=3, strict=True)] | None = None
    torque_input_kind: Literal["motor_iq"] | None = None

    @model_validator(mode="after")
    def torque_binding(self):
        if (self.torque_estimation_model is None) != (self.torque_input_kind is None):
            raise ValueError("力矩估算须同时指定电机型号和原始motor_iq来源；不猜测闭链effort单位")
        return self


class ExecutionPlan(_Model):
    schema_version: Literal[1]
    source_kind: Literal["simulated"]
    robot_id: Annotated[str, Field(min_length=1, max_length=128)]
    robot_dof: Literal[24, 28]
    urdf_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    tolerance_deg: Positive = 3.0
    cycle_s: Annotated[float, Field(ge=0.001, le=0.2, strict=True)]
    baseline_hold_s: Positive
    feedback_timeout_s: Positive
    stop_timeout_s: Positive
    heartbeat_timeout_s: Positive
    rest_velocity_rad_s: Positive
    tasks: Annotated[list[JointTask], Field(min_length=1, max_length=28)]

    @model_validator(mode="after")
    def consistent(self):
        names = [t.joint_name for t in self.tasks]
        if len(set(names)) != len(names) or len(names) > self.robot_dof:
            raise ValueError("每个关节只能检查一个方向，任务数量不能超过机器人自由度")
        if self.baseline_hold_s < 3 * self.cycle_s:
            raise ValueError("保持电流基线至少需要3个采样周期")
        if min(self.feedback_timeout_s, self.stop_timeout_s) < self.cycle_s:
            raise ValueError("反馈与停止超时不能小于控制周期")
        if self.heartbeat_timeout_s < 0.5:
            raise ValueError("上位机心跳超时须不少于0.5秒")
        for t in self.tasks:
            d = t.detection
            if self.cycle_s > d.max_gap_s or d.hold_s < 3 * self.cycle_s:
                raise ValueError("控制周期与候选阻挡判定窗口不匹配")
            if d.max_duration_s > 1800:
                raise ValueError("单轴搜索最长1800秒")
        return self


def _read(path, maximum):
    with Path(path).open("rb") as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("文件超过允许大小")
    return data.decode("utf-8")


def load_execution_plan(config_path, urdf_path):
    config_text = _read(config_path, 1024 * 1024)
    plan = ExecutionPlan.model_validate_json(config_text)
    urdf = _read(urdf_path, MAX_URDF_BYTES)
    if hashlib.sha256(urdf.encode()).hexdigest() != plan.urdf_sha256:
        raise ValueError("执行配置与URDF版本哈希不一致")
    ranges = load_urdf_limits(urdf)
    expected = {}
    for t in plan.tasks:
        if t.joint_name not in ranges:
            raise ValueError("任务关节不在独立旋转URDF关节中：" + t.joint_name)
        r = ranges[t.joint_name]
        bound = r[t.direction + "_rad"]
        sign = 1 if t.direction == "upper" else -1
        if (
            not r["lower_rad"] <= t.start_rad <= r["upper_rad"]
            or not r["lower_rad"] < t.near_rad < r["upper_rad"]
        ):
            raise ValueError("初始和近端预定位须位于所选URDF范围内")
        if sign * (bound - t.near_rad) <= t.detection.minimum_motion_rad:
            raise ValueError("近端位置到参考端点距离不足以证明运动")
        if not 0 < sign * (t.search_end_rad - bound) <= t.max_search_overtravel_rad:
            raise ValueError("搜索目标须有显式且受限的模型端点外指令余量（仅模拟）")
        if (
            not 0
            < sign * (t.simulated_stop_rad - t.near_rad)
            < sign * (t.search_end_rad - t.near_rad)
        ):
            raise ValueError("模拟止挡必须位于近端和搜索终点之间")
        retreat = t.simulated_stop_rad - sign * t.retreat_rad
        if not r["lower_rad"] <= retreat <= r["upper_rad"]:
            raise ValueError("模拟回退终点必须处于URDF范围内")
        if abs(t.search_end_rad - t.near_rad) >= t.detection.max_travel_rad:
            raise ValueError("搜索指令余量超出允许检查行程")
        for a, b, speed in [
            (t.start_rad, t.near_rad, t.approach_speed_rad_s),
            (t.near_rad, t.search_end_rad, t.search_speed_rad_s),
            (t.simulated_stop_rad, retreat, t.search_speed_rad_s),
        ]:
            _move(t, a, b, speed)
        expected[t.joint_name] = bound
    return {
        **plan.model_dump(mode="json"),
        "expected_rad": expected,
        "config_sha256": hashlib.sha256(config_text.encode()).hexdigest(),
        "hardware_ready": False,
        "blockers": list(HARDWARE_BLOCKERS),
    }


def _move(task, start, end, speed):
    return SmoothMove.bounded(
        start,
        end,
        speed_rad_s=speed,
        acceleration_rad_s2=task.acceleration_rad_s2,
        jerk_rad_s3=task.jerk_rad_s3,
        maximum_stroke_rad=task.max_segment_rad,
    )


@dataclass(frozen=True)
class Feedback:
    position_rad: float
    velocity_rad_s: float
    current_a: float
    received_s: float
    sequence: int
    enabled: bool
    fault: bool = False


class SimulatedDriver:
    """Synthetic immediate position servo with a rigid, configured stop.

    Not a dynamics simulator. Current levels below are synthetic test signals,
    independent of RP1 hardware and never exported as real parameters.
    """

    source_kind = "simulated"

    def __init__(self, plan):
        self.tasks = {t.joint_name: t for t in plan.tasks}
        self.positions = {n: t.start_rad for n, t in self.tasks.items()}
        self.targets = dict(self.positions)
        self.enabled = set()
        self.sequence = 0
        self.previous_time = None
        self.previous_positions = dict(self.positions)
        self.commands = []
        self.stop_calls = []

    def arm(self, names):
        self.enabled.update(names)

    def command(self, targets):
        for name, target in targets.items():
            if name not in self.enabled:
                raise RuntimeError("不能向未使能模拟轴发送位置目标")
            self.targets[name] = target
        self.commands.append(dict(targets))

    def read(self, now_s):
        self.sequence += 1
        dt = now_s - self.previous_time if self.previous_time is not None else None
        result = {}
        for n, task in self.tasks.items():
            sign = 1 if task.direction == "upper" else -1
            previous = self.positions[n]
            target = self.targets[n] if n in self.enabled else previous
            position = (
                min(target, task.simulated_stop_rad)
                if sign == 1
                else max(target, task.simulated_stop_rad)
            )
            self.positions[n] = position
            blocked = n in self.enabled and sign * (target - task.simulated_stop_rad) > 1e-10
            current = (
                (task.detection.contact_current_a + task.detection.abort_current_a) / 2
                if blocked
                else task.detection.current_rise_a / 8
            )
            result[n] = Feedback(
                position,
                (position - previous) / dt if dt and dt > 0 else 0.0,
                current,
                now_s,
                self.sequence,
                n in self.enabled,
            )
        self.previous_time = now_s
        return result

    def request_stop(self, names):
        self.stop_calls.append(list(names))
        for name in names:
            self.enabled.discard(name)
            self.targets[name] = self.positions[name]


class SessionControl:
    def __init__(self, *, require_heartbeat=False, clock=time.monotonic):
        self.cancelled = threading.Event()
        self.require_heartbeat = require_heartbeat
        self.clock = clock
        self.last_heartbeat = clock()
        self.reason = "operator_abort"

    def abort(self, reason="operator_abort"):
        self.reason = reason
        self.cancelled.set()

    def heartbeat(self):
        self.last_heartbeat = self.clock()

    def check(self, timeout):
        if self.require_heartbeat and self.clock() - self.last_heartbeat > timeout:
            self.abort("heartbeat_timeout")
        if self.cancelled.is_set():
            raise ExecutionAbort(self.reason)


class ExecutionAbort(RuntimeError):
    pass


class SimulatorSession:
    """Tick-driven software executor; stop feedback is checked separately.

    Deliberately accepts only a simulated driver in this build. Hooks describe
    the required driver contract without silently enabling an unverified SDK.
    """

    def __init__(self, loaded_plan, driver=None, control=None, on_event=None):
        self.loaded = loaded_plan
        self.plan = ExecutionPlan.model_validate(
            {k: loaded_plan[k] for k in ExecutionPlan.model_fields}
        )
        self.driver = driver or SimulatedDriver(self.plan)
        if self.driver.source_kind != "simulated":
            raise ValueError("本版本未接入真机驱动，拒绝实际硬件执行")
        self.control = control or SessionControl()
        self.on_event = on_event or (lambda event: None)
        self.now = 0.0
        self.events, self.samples, self.outcomes = [], [], {}
        self.last_feedback = {}
        self.torque_estimates = {}
        self.torque_profiles = {}
        self.last_targets = {t.joint_name: t.start_rad for t in self.plan.tasks}
        self.stop_verified = False
        self.phase = "preflight"
        self.real_time = False
        self.last_progress_emit = -1.0
        self.safety_faults = []

    def emit(self, event, **values):
        item = dict(
            event=event,
            source_kind="simulated",
            time_s=round(self.now, 6),
            phase=self.phase,
            **values,
        )
        self.events.append(item)
        try:
            self.on_event(item)
        except Exception:
            self.control.abort("event_sink_failed")

    def tick(self, tasks, *, check_control=True):
        if check_control:
            self.control.check(self.plan.heartbeat_timeout_s)
        self.now += self.plan.cycle_s
        if self.real_time:
            time.sleep(self.plan.cycle_s)
        feedback = self.driver.read(self.now)
        selected = {}
        for t in tasks:
            f = feedback.get(t.joint_name)
            if f is None:
                raise ExecutionAbort("missing_feedback:" + t.joint_name)
            values = (f.position_rad, f.velocity_rad_s, f.current_a, f.received_s)
            if not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in values
            ):
                raise ExecutionAbort("invalid_feedback:" + t.joint_name)
            if len(self.samples) >= 250_000:
                raise ExecutionAbort("recording_sample_limit")
            self.samples.append(
                dict(
                    joint_name=t.joint_name,
                    phase=self.phase,
                    time_s=self.now,
                    feedback_sequence=f.sequence,
                    position_rad=f.position_rad,
                    velocity_rad_s=f.velocity_rad_s,
                    current_a=f.current_a,
                    received_s=f.received_s,
                    command_position_rad=self.last_targets[t.joint_name],
                    enabled=f.enabled,
                    drive_fault=f.fault,
                )
            )
            age = self.now - f.received_s
            previous = self.last_feedback.get(t.joint_name)
            if age < -1e-9 or age > min(
                self.plan.feedback_timeout_s, t.detection.max_feedback_age_ms / 1000
            ):
                raise ExecutionAbort("stale_feedback:" + t.joint_name)
            if previous and (
                f.sequence <= previous.sequence or f.received_s <= previous.received_s
            ):
                raise ExecutionAbort("repeated_feedback:" + t.joint_name)
            if f.fault or abs(f.current_a) >= t.detection.abort_current_a:
                raise ExecutionAbort("fault_or_overcurrent:" + t.joint_name)
            self.last_feedback[t.joint_name] = f
            selected[t.joint_name] = f
            self.torque_estimates[t.joint_name] = self.estimate_torque(t, f.current_a)
            self.samples[-1]["torque_estimation"] = self.torque_estimates[t.joint_name]
        if self.now - self.last_progress_emit >= 0.1:
            self.last_progress_emit = self.now
            self.emit(
                "feedback",
                joints={
                    n: dict(
                        position_rad=f.position_rad,
                        current_a=f.current_a,
                        enabled=f.enabled,
                        torque_estimation=self.torque_estimates[n],
                    )
                    for n, f in selected.items()
                },
                current_unit="A",
            )
        return selected

    def estimate_torque(self, task, current_a):
        if task.torque_estimation_model is None:
            return dict(
                provisional_estimated_nm=None,
                estimated_torque_nm=None,
                accuracy_status="not_configured",
                usable_for_contact_detection=False,
                reason="未关联原始电机Iq与型号；不将逻辑effort或母线电流作为Iq",
            )
        from .torque_estimation import estimate_output_torque

        try:
            estimate = estimate_output_torque(
                current_a,
                task.torque_estimation_model,
                current_kind=task.torque_input_kind,
                source_kind="simulated",
            )
            profile_key = (
                f"lro-model-{task.torque_estimation_model}@{estimate['reference']['sha256']}"
            )
            self.torque_profiles.setdefault(
                profile_key,
                {
                    key: value
                    for key, value in estimate.items()
                    if key
                    not in {
                        "raw_iq_a",
                        "provisional_estimated_nm",
                        "estimated_torque_nm",
                        "measured_torque_nm",
                    }
                },
            )
            # Store coefficient/source metadata once; each frame retains its own raw Iq.
            fields = (
                "raw_iq_a",
                "model_id",
                "model_name",
                "model_alias",
                "coefficient_nm_per_a",
                "provisional_estimated_nm",
                "estimated_torque_nm",
                "measured_torque_nm",
                "basis_frame",
                "source_kind",
                "accuracy_status",
                "usable_for_contact_detection",
            )
            return {**{key: estimate[key] for key in fields}, "profile_key": profile_key}
        except ValueError as error:
            return dict(
                provisional_estimated_nm=None,
                estimated_torque_nm=None,
                accuracy_status="invalid_input",
                usable_for_contact_detection=False,
                reason=str(error),
            )

    def command(self, targets, tasks):
        by_name = {t.joint_name: t for t in tasks}
        for n, target in targets.items():
            if abs(target - self.last_targets[n]) > by_name[n].max_command_step_rad:
                raise ExecutionAbort("command_step_exceeded:" + n)
        self.driver.command(targets)
        self.last_targets.update(targets)

    def transition(self, phase, group):
        self.phase = phase
        self.emit("phase", group=group)

    def move(self, tasks, targets, speed_attr, *, initial=False):
        moves = {
            t.joint_name: _move(
                t,
                self.last_feedback[t.joint_name].position_rad,
                targets[t.joint_name],
                getattr(t, speed_attr),
            )
            for t in tasks
        }
        duration = max(m.duration_s for m in moves.values())
        started = self.now
        while self.now - started <= duration + 2 * self.plan.cycle_s:
            feedback = self.tick(tasks)
            for t in tasks:
                f = feedback[t.joint_name]
                if not f.enabled:
                    raise ExecutionAbort("unexpected_disabled_axis:" + t.joint_name)
                if abs(f.position_rad - self.last_targets[t.joint_name]) > t.max_tracking_error_rad:
                    raise ExecutionAbort("tracking_error:" + t.joint_name)
            elapsed = min(duration, self.now - started)
            self.command({n: m.sample(elapsed)[0] for n, m in moves.items()}, tasks)
            if elapsed >= duration:
                final = self.tick(tasks)
                if any(
                    abs(final[t.joint_name].position_rad - targets[t.joint_name])
                    > t.detection.position_span_rad
                    for t in tasks
                ):
                    raise ExecutionAbort("position_not_reached")
                return

    def verify_stop(self, tasks, *, cleanup=False):
        names = [t.joint_name for t in tasks]
        self.driver.request_stop(names)
        self.emit("stop_requested", joints=names, simulated=True)
        start = self.now
        while self.now - start <= self.plan.stop_timeout_s:
            try:
                feedback = self.tick(tasks, check_control=False)
            except ExecutionAbort as error:
                self.safety_faults.append(str(error))
                self.emit("stop_feedback_error", reason=str(error))
                if not cleanup:
                    raise
                continue
            if all(
                not f.enabled and abs(f.velocity_rad_s) <= self.plan.rest_velocity_rad_s
                for f in feedback.values()
            ):
                self.emit(
                    "stop_verified", joints=names, simulated=True, physical_stop_verified=False
                )
                return True
        self.emit("stop_unverified", joints=names, physical_stop_verified=False)
        return False

    def search(self, tasks):
        baseline = {t.joint_name: [] for t in tasks}
        start = self.now
        while self.now - start < self.plan.baseline_hold_s:
            feedback = self.tick(tasks)
            for t in tasks:
                f = feedback[t.joint_name]
                if (
                    not f.enabled
                    or abs(f.position_rad - t.near_rad) > t.detection.position_span_rad
                    or abs(f.velocity_rad_s) > self.plan.rest_velocity_rad_s
                ):
                    raise ExecutionAbort("baseline_not_stationary:" + t.joint_name)
                baseline[t.joint_name].append(abs(f.current_a))
        detectors, moves = {}, {}
        for t in tasks:
            base = statistics.median(baseline[t.joint_name])
            self.emit("baseline", joint=t.joint_name, current_a=base, current_unit="A")
            detectors[t.joint_name] = CurrentObstructionDetector(
                t.detection, t.direction, baseline_current_a=base
            )
            moves[t.joint_name] = _move(t, t.near_rad, t.search_end_rad, t.search_speed_rad_s)
        start = self.now
        states = {t.joint_name: "searching" for t in tasks}
        stop_started = {}
        while any(state != "stopped" for state in states.values()):
            # One regular tick monitors the ENTIRE group, including stopped axes.
            feedback = self.tick(tasks)
            targets = {}
            for t in tasks:
                n, f = t.joint_name, feedback[t.joint_name]
                if states[n] == "stopped":
                    if f.enabled or abs(f.velocity_rad_s) > self.plan.rest_velocity_rad_s:
                        raise ExecutionAbort("stopped_axis_moved_or_enabled:" + n)
                    continue
                if states[n] == "stopping":
                    if not f.enabled and abs(f.velocity_rad_s) <= self.plan.rest_velocity_rad_s:
                        states[n] = "stopped"
                        self.emit(
                            "stop_verified",
                            joints=[n],
                            simulated=True,
                            physical_stop_verified=False,
                        )
                    elif self.now - stop_started[n] >= self.plan.stop_timeout_s:
                        raise ExecutionAbort("candidate_stop_unverified:" + n)
                    continue
                if not f.enabled:
                    raise ExecutionAbort("unexpected_disabled_axis:" + n)
                result = detectors[n].feed(
                    CurrentSample(
                        time_s=self.now,
                        feedback_sequence=f.sequence,
                        position_rad=f.position_rad,
                        current_a=f.current_a,
                        command_position_rad=self.last_targets[n],
                        position_age_ms=(self.now - f.received_s) * 1000,
                        current_age_ms=(self.now - f.received_s) * 1000,
                        drive_fault=f.fault,
                    )
                )
                if result:
                    if result["status"] != "candidate_obstruction":
                        raise ExecutionAbort(result["reason_code"] + ":" + n)
                    # Act on the stop before emitting an event or checking another axis.
                    self.driver.request_stop([n])
                    states[n], stop_started[n] = "stopping", self.now
                    comparison = compare_current_endpoint(
                        result,
                        self.loaded["expected_rad"][n],
                        math.radians(self.plan.tolerance_deg),
                    )
                    self.outcomes[n] = dict(
                        detection=result,
                        comparison=comparison,
                        torque_estimation_at_detection=self.torque_estimates[n],
                        torque_sample_sequence=f.sequence,
                        torque_sample_received_s=f.received_s,
                    )
                    self.emit("stop_requested", joints=[n], simulated=True)
                    self.emit("candidate_obstruction", joint=n, comparison=comparison)
                else:
                    elapsed = self.now - start
                    if elapsed >= moves[n].duration_s + self.plan.cycle_s:
                        raise ExecutionAbort("search_exhausted_without_candidate:" + n)
                    targets[n] = moves[n].sample(elapsed)[0]
            if targets:
                self.command(targets, tasks)

    def run(self, *, real_time=False):
        self.real_time = real_time
        status, reason = "aborted", "not_started"
        all_tasks = list(self.plan.tasks)
        try:
            self.emit(
                "started",
                robot_id=self.plan.robot_id,
                robot_dof=self.plan.robot_dof,
                hardware_ready=False,
                task_count=len(all_tasks),
            )
            self.control.check(self.plan.heartbeat_timeout_s)
            initial = self.tick(all_tasks)
            for t in all_tasks:
                if (
                    abs(initial[t.joint_name].position_rad - t.start_rad)
                    > t.detection.position_span_rad
                ):
                    raise ExecutionAbort("initial_pose_mismatch:" + t.joint_name)
            groups = sorted({t.group for t in all_tasks})
            for group in groups:
                tasks = [t for t in all_tasks if t.group == group]
                self.driver.arm([t.joint_name for t in tasks])
                self.transition("approach", group)
                self.move(tasks, {t.joint_name: t.near_rad for t in tasks}, "approach_speed_rad_s")
                self.transition("baseline_and_search", group)
                self.search(tasks)
                self.transition("retreat", group)
                targets = {
                    t.joint_name: self.outcomes[t.joint_name]["detection"]["position_rad"]
                    - (1 if t.direction == "upper" else -1) * t.retreat_rad
                    for t in tasks
                }
                for t in tasks:
                    self.last_targets[t.joint_name] = self.last_feedback[t.joint_name].position_rad
                self.driver.arm([t.joint_name for t in tasks])
                self.move(tasks, targets, "search_speed_rad_s")
                if not self.verify_stop(tasks):
                    raise ExecutionAbort("retreat_stop_unverified")
            status, reason = "completed_simulation", "单侧候选采集及回退完成；未认证标零"
        except Exception as error:
            reason = str(error)
        finally:
            self.phase = "final_stop"
            try:
                self.stop_verified = self.verify_stop(all_tasks, cleanup=True)
            except Exception as error:
                self.stop_verified = False
                reason += ";stop_error:" + str(error)
            if self.safety_faults:
                if status == "completed_simulation":
                    status = "aborted"
                reason += ";stop_feedback_errors:" + ",".join(self.safety_faults)
            if not self.stop_verified:
                status = "stop_unverified"
            self.phase = "finished"
        return dict(
            schema_version=1,
            source_kind="simulated",
            robot_id=self.plan.robot_id,
            robot_dof=self.plan.robot_dof,
            status=status,
            reason=reason,
            urdf_sha256=self.plan.urdf_sha256,
            config_sha256=self.loaded["config_sha256"],
            current_unit="A",
            position_unit="rad",
            tolerance_deg=self.plan.tolerance_deg,
            hardware_ready=False,
            physical_stop_verified=False,
            calibration_passed=None,
            simulated_stop_verified=self.stop_verified,
            outcomes=self.outcomes,
            torque_estimates=self.torque_estimates,
            torque_profiles=self.torque_profiles,
            torque_estimation_usage="provisional_display_and_recording_only;contact_detection_uses_current_A",
            simulated_time_s=self.now,
            events=self.events,
            samples=self.samples,
            safety_faults=self.safety_faults,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--urdf", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--require-heartbeat", action="store_true")
    parser.add_argument("--fast", action="store_true", help="仅模拟：虚拟时钟快速运行")
    args = parser.parse_args(argv)
    if not args.simulate:
        parser.error("本版本只允许--simulate；未接入真机驱动")
    try:
        loaded = load_execution_plan(args.config, args.urdf)
        # Create a unique directory before any driver is constructed; never overwrite evidence.
        output = args.output / ("simulated-" + uuid4().hex)
        output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError) as error:
        parser.exit(2, str(error) + "\n")
    control = SessionControl(require_heartbeat=args.require_heartbeat)

    def listen():
        for line in sys.stdin:
            if line.strip() == "abort":
                control.abort()
            elif line.strip() == "heartbeat":
                control.heartbeat()
        if args.require_heartbeat:
            control.abort("control_channel_closed")

    threading.Thread(target=listen, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: control.abort("process_signal"))

    def emit(event):
        try:
            print(json.dumps(event, ensure_ascii=False), flush=True)
        except OSError:
            control.abort("output_channel_closed")

    session = SimulatorSession(loaded, control=control, on_event=emit)
    result = session.run(real_time=not args.fast)
    try:
        report = output / "result.json"
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        emit(
            dict(
                event="finished",
                source_kind="simulated",
                status=result["status"],
                reason=result["reason"],
                output_files=[str(report.resolve())],
            )
        )
    except OSError as error:
        parser.exit(3, "模拟已结束，但保存结果失败：" + str(error) + "\n")
    return 0 if result["status"] == "completed_simulation" else 1


if __name__ == "__main__":
    raise SystemExit(main())
