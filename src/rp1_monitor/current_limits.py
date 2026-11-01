"""Current-based candidate obstruction detection, with no hardware I/O.

Positions and commands must already use the same URDF joint coordinates. The
load measurement is explicitly amperes: no torque conversion is inferred. All
thresholds are supplied for the particular motor and test, without RP1 defaults.
"""

import math
import statistics
from collections import deque
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

Positive = Annotated[StrictFloat, Field(gt=0)]
Nonnegative = Annotated[StrictFloat, Field(ge=0)]
Direction = Literal["lower", "upper"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CurrentDetectionSettings(_Model):
    hold_s: Positive
    max_gap_s: Positive
    max_feedback_age_ms: Positive
    position_span_rad: Positive
    minimum_motion_rad: Positive
    command_progress_rad: Positive
    contact_current_a: Positive
    current_rise_a: Positive
    abort_current_a: Positive
    max_duration_s: Positive
    max_travel_rad: Positive

    @model_validator(mode="after")
    def coherent(self):
        if self.contact_current_a >= self.abort_current_a:
            raise ValueError("接触电流必须低于立即中止电流")
        if self.minimum_motion_rad <= self.position_span_rad:
            raise ValueError("先前运动量必须大于静止角度范围")
        if self.max_travel_rad <= self.minimum_motion_rad:
            raise ValueError("最大检查行程必须大于先前运动量")
        if self.max_duration_s <= self.hold_s or self.max_gap_s > self.hold_s:
            raise ValueError("持续窗口、最大反馈间隔和总时长不一致")
        return self


class CurrentSample(_Model):
    time_s: Nonnegative
    feedback_sequence: Annotated[StrictInt, Field(ge=0)]
    position_rad: StrictFloat
    current_a: StrictFloat
    command_position_rad: StrictFloat
    position_age_ms: Nonnegative
    current_age_ms: Nonnegative
    drive_fault: StrictBool


class CurrentObstructionDetector:
    """Detect one side; stop requests latch until a new detector is created.

    ``feed`` does not stop a motor. The local executor must act on request_stop
    and independently run ``check_timeout`` even when no feedback arrives. The
    executor must also bound the initial wait before the first sample.
    """

    def __init__(
        self,
        settings: CurrentDetectionSettings,
        direction: Direction,
        *,
        baseline_current_a: float | None = None,
    ):
        if direction not in {"lower", "upper"}:
            raise ValueError("方向必须为lower或upper")
        if baseline_current_a is not None and (
            isinstance(baseline_current_a, bool)
            or not isinstance(baseline_current_a, (int, float))
            or not math.isfinite(baseline_current_a)
            or baseline_current_a < 0
        ):
            raise ValueError("保持电流基线必须为有限非负A数值")
        self.settings = CurrentDetectionSettings.model_validate(settings)
        self.direction = direction
        self.sign = -1 if direction == "lower" else 1
        self.baseline_current_a = baseline_current_a
        self.baseline_basis = (
            "measured_window" if baseline_current_a is not None else "first_sample"
        )
        self.first: CurrentSample | None = None
        self.previous: CurrentSample | None = None
        self.result: dict | None = None
        # A bounded queue remains conservative if input outruns this capacity:
        # it cannot establish hold_s until the retained samples span hold_s.
        self.window: deque[CurrentSample] = deque(maxlen=4096)
        self.moved = False
        self.highest_progress = 0.0

    def _finish(self, status, reason_code, reason, time_s, **evidence):
        self.result = dict(
            status=status,
            reason_code=reason_code,
            reason=reason,
            time_s=time_s,
            direction=self.direction,
            request_stop=True,
            stop_acknowledged=False,
            current_unit="A",
            position_unit="rad",
            baseline_current_a=self.baseline_current_a,
            baseline_basis=self.baseline_basis,
            physical_stop_confirmed=False,
            calibration_passed=None,
            **evidence,
        )
        return self.result

    def check_timeout(self, time_s: float):
        """Check local monotonic time during silence, after the first sample."""
        if self.result is not None:
            return self.result
        if isinstance(time_s, bool) or not isinstance(time_s, (int, float)):
            raise ValueError("看门狗时间必须为有限非负数")
        if not math.isfinite(time_s) or time_s < 0:
            raise ValueError("看门狗时间必须为有限非负数")
        if self.previous is None:
            return None
        if time_s < self.previous.time_s:
            return self._finish("aborted", "clock_reversed", "本地单调时钟倒序", time_s)
        if time_s - self.previous.time_s > self.settings.max_gap_s:
            return self._finish("aborted", "feedback_timeout", "关节反馈中断", time_s)
        if time_s - self.first.time_s >= self.settings.max_duration_s:
            return self._finish("aborted", "duration_limit", "达到最大检查时长", time_s)
        return None

    def feed(self, sample: CurrentSample):
        if self.result is not None:
            return self.result
        sample = CurrentSample.model_validate(sample)
        p = self.settings
        if self.baseline_current_a is None:
            self.baseline_current_a = abs(sample.current_a)
        # Every failure is checked before the candidate test. A simultaneous
        # plateau and overcurrent must abort, never produce an endpoint.
        if sample.drive_fault:
            return self._finish("aborted", "drive_fault", "驱动上报故障", sample.time_s)
        if max(sample.position_age_ms, sample.current_age_ms) > p.max_feedback_age_ms:
            return self._finish("aborted", "stale_feedback", "位置或电流反馈过期", sample.time_s)
        if abs(sample.current_a) >= p.abort_current_a:
            return self._finish("aborted", "overcurrent", "达到立即中止电流", sample.time_s)
        if self.previous is not None:
            dt = sample.time_s - self.previous.time_s
            if (
                dt <= 0
                or dt > p.max_gap_s
                or sample.feedback_sequence <= self.previous.feedback_sequence
            ):
                return self._finish(
                    "aborted",
                    "invalid_feedback_order",
                    "反馈重复、倒序或中断",
                    sample.time_s,
                )
        if self.first is None:
            self.first = sample
        travel = self.sign * (sample.position_rad - self.first.position_rad)
        if self.highest_progress - travel > p.position_span_rad:
            return self._finish(
                "aborted",
                "reverse_motion",
                "反向运动或角度坐标跳变",
                sample.time_s,
            )
        self.highest_progress = max(self.highest_progress, travel)
        if abs(travel) >= p.max_travel_rad:
            return self._finish("aborted", "travel_limit", "达到最大检查行程", sample.time_s)
        if sample.time_s - self.first.time_s >= p.max_duration_s:
            return self._finish("aborted", "duration_limit", "达到最大检查时长", sample.time_s)
        self.moved |= travel >= p.minimum_motion_rad
        self.previous = sample
        self.window.append(sample)
        boundary = sample.time_s - p.hold_s
        while len(self.window) > 2 and self.window[1].time_s <= boundary:
            self.window.popleft()
        start = self.window[0]
        if not self.moved or sample.time_s - start.time_s < p.hold_s or len(self.window) < 3:
            return None
        positions = [item.position_rad for item in self.window]
        currents = [abs(item.current_a) for item in self.window]
        commands = [self.sign * item.command_position_rad for item in self.window]
        if (
            max(positions) - min(positions) <= p.position_span_rad
            and min(currents) >= p.contact_current_a
            and min(currents) - self.baseline_current_a >= p.current_rise_a
            and commands[-1] - commands[0] >= p.command_progress_rad
            and all(b >= a for a, b in zip(commands, commands[1:]))
        ):
            return self._finish(
                "candidate_obstruction",
                "sustained_current_plateau",
                "已运动后持续电流升高、位置停滞且目标继续推进；阻挡原因待确认",
                sample.time_s,
                position_rad=statistics.median(positions),
                position_span_rad=max(positions) - min(positions),
                peak_current_a=max(currents),
                window_start_s=start.time_s,
                feedback_sequence=sample.feedback_sequence,
            )
        return None


def compare_current_endpoint(
    result,
    expected_rad: float,
    tolerance_rad: float,
    *,
    mapping_verified: bool = False,
    mechanical_reference_verified: bool = False,
):
    """Compare a candidate with its selected URDF endpoint, without certification."""
    for value in (expected_rad, tolerance_rad):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("参考角度和误差容差必须为有限数值")
    if tolerance_rad <= 0:
        raise ValueError("误差容差必须大于零")
    if type(mapping_verified) is not bool or type(mechanical_reference_verified) is not bool:
        raise ValueError("映射和机械参考确认必须为布尔值")
    if result is None or result.get("status") != "candidate_obstruction":
        return None
    observed = result.get("position_rad")
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(observed)
        or result.get("direction") not in {"lower", "upper"}
    ):
        raise ValueError("候选端点缺少有效角度或方向")
    deviation = observed - expected_rad
    within = abs(deviation) <= tolerance_rad
    return dict(
        direction=result["direction"],
        observed_rad=observed,
        expected_rad=expected_rad,
        deviation_rad=deviation,
        tolerance_rad=tolerance_rad,
        within_numeric_tolerance=within,
        within_reference_tolerance=(
            within if mapping_verified and mechanical_reference_verified else None
        ),
        basis="candidate_obstruction",
        physical_stop_confirmed=False,
        calibration_passed=None,
    )
