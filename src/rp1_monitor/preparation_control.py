"""Pure preparation-command calculation in physical motor order.

Positions are radians, time is seconds and feedforward is output torque in Nm.
This module performs no robot I/O and does not decide whether preparation passed.
The runtime applies the selected profile's feedback, fault and timeout checks.
"""

import math


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}必须是有限数值")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name}必须是有限数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name}必须是有限数值")
    return result


def _vector(values, size, name, *, nonnegative=False):
    if not isinstance(values, (list, tuple)) or len(values) != size:
        raise ValueError(f"{name}须按物理电机顺序提供恰好{size}项")
    result = [_number(value, name) for value in values]
    if nonnegative and any(value < 0 for value in result):
        raise ValueError(f"{name}不能为负")
    return result


def _gains(value, size, name):
    if not isinstance(value, dict) or "kp" not in value or "kd" not in value:
        raise ValueError(f"{name}须包含kp和kd")
    return {key: _vector(value[key], size, f"{name}.{key}", nonnegative=True)
            for key in ("kp", "kd")}


def interpolate_gains(start, end, elapsed_s, duration_s=2.0):
    """Return a cubic S-curve gain transition without mutating either endpoint.

    Both endpoints are dictionaries containing equal-length kp/kd vectors.
    Feedforward, if present in a snapshot, is deliberately not interpolated here.
    """
    if not isinstance(start, dict) or not isinstance(start.get("kp"), (list, tuple)):
        raise ValueError("初始增益须包含kp和kd数组")
    size = len(start["kp"])
    if size == 0:
        raise ValueError("增益数组不能为空")
    start, end = _gains(start, size, "初始增益"), _gains(end, size, "目标增益")
    elapsed = _number(elapsed_s, "增益切换时间")
    duration = _number(duration_s, "增益切换时长")
    if duration <= 0:
        raise ValueError("增益切换时长必须为正")
    u = max(0.0, min(1.0, elapsed / duration))
    fraction = u * u * (3.0 - 2.0 * u)
    return {key: [a if a == b else a * (1.0 - fraction) + b * fraction
                  for a, b in zip(start[key], end[key])]
            for key in ("kp", "kd")}


class PreparationController:
    """Ramp deploy gains and learn a small, slew-limited static-load torque.

    The integral is enabled only at a fixed settling endpoint with error <= 3°
    and measured speed <= 0.5°/s across at least 0.1 seconds of feedback. Its
    stored state is the bounded output itself,
    so saturation cannot accumulate an unseen windup. The controller never
    raises the profile gains or relaxes the runtime's acceptance tolerances.
    """

    def __init__(self, profile, abort_torque_nm, start_positions, now_s,
                 initial_gains=None, initial_feedforward=None, *, deploy_reset=False):
        if not isinstance(profile, dict) or not isinstance(profile.get("kp"), (list, tuple)):
            raise ValueError("准备增益配置须包含kp和kd数组")
        self._size = len(profile["kp"])
        if not self._size:
            raise ValueError("准备增益数组不能为空")
        self._target_gains = _gains(profile, self._size, "准备增益")
        if type(deploy_reset) is not bool:
            raise ValueError("deploy复位标志必须为布尔值")
        self._deploy_reset = deploy_reset
        self._ramp_s = _number(profile.get("ramp_s"), "准备增益切换时长")
        if self._ramp_s != 2.0:
            raise ValueError("准备增益必须用2秒平滑切换")
        self._abort_torque_nm = None if abort_torque_nm is None else _number(abort_torque_nm, "中止扭矩")
        if self._abort_torque_nm is not None and self._abort_torque_nm <= 0:
            raise ValueError("中止扭矩必须为正")
        self._ff_limit = 2.5 if self._abort_torque_nm is None else min(2.5, 0.25 * self._abort_torque_nm)
        self._initial_gains = _gains(
            initial_gains if initial_gains is not None
            else {"kp": [0.0] * self._size, "kd": [0.5] * self._size},
            self._size, "初始增益")
        self._gains = {key: list(value) for key, value in self._initial_gains.items()}
        self._feedforward = _vector(
            initial_feedforward if initial_feedforward is not None else [0.0] * self._size,
            self._size, "初始补偿")
        if any(abs(value) > self._ff_limit for value in self._feedforward):
            raise ValueError("初始补偿超出有界补偿上限")
        if self._deploy_reset:
            self._gains = {"kp": [value / 2.5 for value in self._target_gains["kp"]],
                           "kd": self._target_gains["kd"][:]}
            self._feedforward = [0.0] * self._size
        self._start_s = self._last_s = _number(now_s, "初始时间")
        _vector(start_positions, self._size, "初始角度")
        # Constructor positions have no known receive timestamps and cannot
        # serve as velocity or integral evidence for the first real sample.
        self._speed_samples = [[] for _ in range(self._size)]
        self._last_settling_targets = None
        self._seen_command = False

    def command(self, now_s, targets, positions, torques, *, settling=False, feedback_times=None):
        """Return fresh kp/kd/feedforward lists, rejecting invalid samples.

        The first call may share the construction timestamp; subsequent samples
        must advance by no more than 0.25 seconds. A first sample, a changed
        endpoint, or a non-settling sample cannot increase or erase compensation.
        Physical receive times establish independent per-motor speed windows;
        cached samples freeze compensation. Pure simulations may omit them to
        treat each command's positions as a new sample received at ``now_s``.
        """
        now = _number(now_s, "反馈时间")
        dt = now - self._last_s
        if dt < 0 or dt > 0.25 or (self._seen_command and dt == 0):
            raise ValueError("反馈时间须递增且间隔不超过0.25秒")
        if type(settling) is not bool:
            raise ValueError("settling必须是布尔值")
        targets = _vector(targets, self._size, "目标角度")
        positions = _vector(positions, self._size, "实际角度")
        torques = _vector(torques, self._size, "反馈扭矩")
        times = _vector([now] * self._size if feedback_times is None else feedback_times,
                        self._size, "物理电机反馈接收时间")
        if any(received > now or now - received > 0.25 for received in times):
            raise ValueError("物理电机反馈接收时间超前或超过0.25秒")
        if self._abort_torque_nm is not None and any(abs(value) >= self._abort_torque_nm for value in torques):
            raise ValueError("反馈扭矩达到中止上限")
        if self._deploy_reset:
            scale = 1.0 if now - self._start_s >= 4.0 else 1.0 / 2.5
            self._gains = {"kp": [value * scale for value in self._target_gains["kp"]],
                           "kd": self._target_gains["kd"][:]}
            self._feedforward = [0.0] * self._size
            self._last_s, self._seen_command = now, True
            return self.snapshot()
        gains = interpolate_gains(
            self._initial_gains, self._target_gains, now - self._start_s, self._ramp_s)
        feedforward = list(self._feedforward)
        windows = [window[:] for window in self._speed_samples]
        can_integrate = self._seen_command and settling and targets == self._last_settling_targets
        for index, (received, actual, window) in enumerate(zip(times, positions, windows)):
            previous = window[-1] if window else None
            if previous is not None:
                if received < previous[0]:
                    raise ValueError("物理电机反馈接收时间倒退")
                if received == previous[0]:
                    if actual != previous[1]:
                        raise ValueError("相同物理电机接收时间的角度发生变化")
                    continue
            window.append((received, actual))
            # Keep the point at/before the left boundary, so a jittered stream
            # can cover the complete window rather than staying just below it.
            while len(window) >= 3 and window[1][0] <= received - 0.1 + 1e-9:
                window.pop(0)
            span = received - window[0][0]
            if not can_integrate or previous is None or span < 0.1 - 1e-9:
                continue
            error = targets[index] - actual
            # A native encoder count is about 0.022 degrees: its 50 Hz frame
            # derivative is 1.1 deg/s even for a stationary motor. Use the full
            # observed spread over >=100 ms of DISTINCT receive times. Cached
            # control ticks never turn a moving, low-rate stream into a plateau.
            speed = (max(position for _, position in window)
                     - min(position for _, position in window)) / span
            if not math.isfinite(error) or not math.isfinite(speed):
                raise ValueError("角度差或反馈速度必须有限")
            if abs(error) <= math.radians(3.0) and speed <= math.radians(0.5):
                # Integrate actual sample time, but bound every SENT increment
                # by the control period. There is no hidden accumulated state.
                sample_dt = received - previous[0]
                delta = gains["kp"][index] * 0.5 * error * sample_dt
                delta = max(-0.5 * dt, min(0.5 * dt, delta))
                feedforward[index] = max(
                    -self._ff_limit, min(self._ff_limit, feedforward[index] + delta))
        self._gains, self._feedforward = gains, feedforward
        self._last_s = now
        self._speed_samples = windows
        self._last_settling_targets = targets if settling else None
        self._seen_command = True
        return self.snapshot()

    def snapshot(self):
        """Copy the last emitted gains and compensation for a smooth handoff."""
        return {"kp": list(self._gains["kp"]), "kd": list(self._gains["kd"]),
                "feedforward": list(self._feedforward)}
