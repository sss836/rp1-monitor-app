"""Manual endpoint observations only: no SDK, I/O, targets, or movement commands."""

import copy
import json
import math
import statistics

try:
    from .calibration_check import validate_video_plan
except ImportError:  # Standalone, in-memory SSH helper modules.
    from calibration_check import validate_video_plan

DEFAULT_OPTIONS = {"tolerance_deg": 3.0, "hold_s": 1.0, "position_span_deg": 0.2,
                   "torque_rise_nm": 3.0, "contact_torque_nm": 0.5,
                   "torque_required": False}
PHASES = {"ready", "collecting", "paused", "failed", "complete"}
STATUSES = {"pending", "active", "passed", "failed", "skipped"}
METRICS = ("current_angle_deg", "peak_angle_deg", "span_deg", "torque_nm",
           "baseline_nm", "rise_nm", "sample_time_s")
CONDITIONS = {"in_range", "stable", "torque", "no_overshoot"}
REFERENCE_FIELDS = ("zero_direction", "zero_source", "check_side_basis", "zero_pose_deg", "zero_reference_error_deg",
                    "check_mode", "tolerance_deg", "pin_hold_s", "pin_confirm_required")
MAX_FRAME_GAP_S = 0.25
MAX_ENCODER_FRAME_GAP_S = 0.55


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _outcome_complete(row):
    return (row['status'] in {'passed', 'skipped'}
            or (row['status'] == 'failed' and 'failure_confirmation' in row['attempts'][-1]))


def _target_not_reached(direction, difference):
    limit = DEFAULT_OPTIONS['tolerance_deg'] + 1e-9
    return direction == 'upper' and difference < -limit or direction == 'lower' and difference > limit


def validate_options(options=None):
    if options is None:
        options = {}
    if not isinstance(options, dict) or set(options) - DEFAULT_OPTIONS.keys():
        raise ValueError("人工检查参数包含未知字段")
    normalized = DEFAULT_OPTIONS | options
    if normalized["torque_required"] is not False:
        raise ValueError("人工检查的力矩固定仅作辅助记录")
    for name in DEFAULT_OPTIONS.keys() - {"torque_required"}:
        if not _finite(normalized[name]):
            raise ValueError(name + "须为有限数值")
        normalized[name] = float(normalized[name])
    if normalized["tolerance_deg"] != 3.0:
        raise ValueError("人工检查角度容差固定为±3°")
    if not 0.1 <= normalized["hold_s"] <= 30 or not 0 < normalized["position_span_deg"] <= 3:
        raise ValueError("人工检查稳定时间或位置幅度无效")
    if any(not 0 <= normalized[key] <= 100 for key in ("torque_rise_nm", "contact_torque_nm")):
        raise ValueError("辅助力矩阈值无效")
    return normalized


class ManualCalibrationCheck:
    """Outcome order is distinct from joint-vector order; every retest appends.

    The newest attempt is effective, including a retest of an earlier pass.
    An interrupted attempt is pending. A failed attempt can only be superseded
    by an explicit retest/skip. Confirming the failure keeps the attempt effective.
    """

    def __init__(self, plan, options=None, *, max_frame_gap_s=MAX_FRAME_GAP_S):
        if isinstance(plan, dict) and plan.get("mode") == "manual_opposite_zero_limit_check":
            try:
                from .manual_check_plan import validate_manual_plan
            except ImportError:  # Standalone helper loads the reference module first.
                from manual_check_plan import validate_manual_plan
            self.plan = validate_manual_plan(plan, plan.get("dof"))
            self.reference_source_kind = "urdf_model"
        else:
            # Legacy video inputs remain test compatibility only; runtime gates the
            # production command to the new, independently validated manual plan.
            self.plan = validate_video_plan(plan, plan.get("dof") if isinstance(plan, dict) else None)
            self.reference_source_kind = "simulated"
        self.options = validate_options(options)
        if not _finite(max_frame_gap_s) or not MAX_FRAME_GAP_S <= max_frame_gap_s <= MAX_ENCODER_FRAME_GAP_S:
            raise ValueError("检查反馈最大间隔无效")
        self.max_frame_gap_s = max_frame_gap_s
        self.dof = self.plan["dof"]
        self.phase, self.paused, self.current_index = "ready", False, 0
        self.interruption_reason = None
        self._interrupted = False
        self._last_now = None
        self.data_quality = "awaiting_first_sample"
        names = self.plan["joint_names"]
        self.results = [dict(index=index, joint=row["joint"], joint_index=names.index(row["joint"]),
                             reference_deg=math.degrees(row["requested_rad"]), direction=row["direction"],
                             status="pending", attempts=[], effective_attempt_id=None)
                        for index, row in enumerate(self.plan["outcomes"])]
        for row, original in zip(self.results, self.plan["outcomes"]):
            for key in REFERENCE_FIELDS:
                if key in original:
                    row[key] = copy.deepcopy(original[key])
        self._activate(0)

    def _activate(self, index):
        self.current_index = index
        row = self.results[index]
        attempt = {key: copy.deepcopy(row[key]) for key in ("index", "joint", "joint_index", "reference_deg", "direction")}
        for key in REFERENCE_FIELDS:
            if key in row:
                attempt[key] = copy.deepcopy(row[key])
        attempt.update({key: None for key in METRICS})
        attempt.update(attempt_id=len(row["attempts"]) + 1, status="active", started_at_s=None,
                       ended_at_s=None, reason=None, stable_elapsed_s=0.0,
                       conditions=dict.fromkeys(CONDITIONS, False))
        if row.get("check_mode") in {"pin_zero", "center_zero"}:
            attempt["pin_confirmed_at_s"] = None
            attempt["pin_window_min_deg"] = attempt["pin_window_max_deg"] = None
            if row.get("check_mode") == "center_zero":
                attempt["zero_window_start_s"] = None
        row["attempts"].append(attempt)
        row.update(status="active", effective_attempt_id=attempt["attempt_id"])
        self._window, self._baseline = [], []
        self._baseline_started = self._last_sample = self._last_unique_now = None
        self._peak_sample_time = None
        self._baseline_closed = False

    def _attempt(self):
        if self.current_index is None:
            return None
        return self.results[self.current_index]["attempts"][-1]

    def _reset_window(self):
        self._window = []
        attempt = self._attempt()
        if attempt:
            attempt["stable_elapsed_s"], attempt["span_deg"] = 0.0, None
            attempt["conditions"]["stable"] = False
            if attempt.get("check_mode") in {"pin_zero", "center_zero"}:
                attempt["pin_window_min_deg"] = attempt["pin_window_max_deg"] = None
                if attempt.get("check_mode") == "center_zero":
                    attempt["zero_window_start_s"] = None

    def _finish(self, status, reason):
        attempt = self._attempt()
        attempt.update(status=status, ended_at_s=self._last_now, reason=reason)
        self.results[self.current_index]["status"] = status

    def _advance(self):
        old = self.current_index
        indices = list(range(old + 1, len(self.results))) + list(range(old + 1))
        following = next((index for index in indices if not _outcome_complete(self.results[index])), None)
        if following is None:
            self.current_index, self.phase, self.paused = None, "complete", False
        elif self.results[following]["status"] == "failed":
            # Returning to an unresolved failure is not authorization to retest it.
            self.current_index, self.phase, self.paused = following, "failed", True
            self._window, self._baseline = [], []
            self._baseline_closed = True
            self._baseline_started = self._attempt()["started_at_s"]
            self._last_sample = self._attempt()["sample_time_s"]
            self._last_unique_now = self._last_now
        else:
            self._activate(following)
            self.phase, self.paused = "collecting", False

    def _invalid(self, reason):
        self.data_quality = reason
        self._reset_window()
        attempt = self._attempt()
        if attempt:
            attempt["current_angle_deg"] = attempt["torque_nm"] = attempt["rise_nm"] = None
            attempt["conditions"]["in_range"] = attempt["conditions"]["torque"] = False
        return self.report()

    def invalidate_measurement(self):
        return self._invalid("missing_or_invalid_measurement") if self.current_index is not None else self.report()

    def update(self, now_s, positions_rad, torques_nm, *, sample_times_s=None):
        if self.current_index is None:
            return self.report()
        if not _finite(now_s) or now_s < 0 or (self._last_now is not None and now_s <= self._last_now):
            return self._invalid("invalid_or_non_increasing_update_time")
        self._last_now = float(now_s)
        if not isinstance(positions_rad, (list, tuple)) or len(positions_rad) != self.dof:
            return self._invalid("incomplete_position_vector")
        attempt = self._attempt()
        joint = attempt["joint_index"]
        angle = positions_rad[joint]
        if not _finite(angle):
            return self._invalid("invalid_joint_angle")
        stamp = now_s
        if sample_times_s is not None:
            if not isinstance(sample_times_s, (list, tuple)) or len(sample_times_s) != self.dof:
                return self._invalid("incomplete_sample_time_vector")
            stamp = sample_times_s[joint]
        if not _finite(stamp) or stamp < 0 or stamp > now_s + 1e-9 or now_s - stamp > self.max_frame_gap_s + 1e-9:
            return self._invalid("missing_stale_or_future_sample_time")
        if self._last_sample is not None and stamp <= self._last_sample:
            if stamp < self._last_sample:
                return self._invalid("out_of_order_sample")
            self.data_quality = "duplicate_sample"
            if self._last_unique_now is not None and now_s - self._last_unique_now > self.max_frame_gap_s + 1e-9:
                self._reset_window()
            return self.report()
        gap = self._last_sample is not None and (stamp - self._last_sample > self.max_frame_gap_s + 1e-9
              or now_s - self._last_unique_now > self.max_frame_gap_s + 1e-9)
        if gap:
            self._reset_window()
        self._last_sample, self._last_unique_now = float(stamp), float(now_s)
        self.data_quality = "frame_gap_reset" if gap else "fresh_unique_sample"
        if attempt["started_at_s"] is None:
            attempt["started_at_s"] = float(now_s)
        angle = math.degrees(angle)
        torque = torques_nm[joint] if isinstance(torques_nm, (list, tuple)) and len(torques_nm) == self.dof else None
        torque = float(torque) if _finite(torque) else None
        attempt.update(current_angle_deg=angle, torque_nm=torque, sample_time_s=float(stamp))
        if attempt.get("check_mode") in {"pin_zero", "center_zero"}:
            attempt["peak_angle_deg"] = angle
            attempt["conditions"]["in_range"] = abs(angle - attempt["reference_deg"]) <= attempt["tolerance_deg"] + 1e-9
            if self.paused or self._interrupted or attempt["status"] == "failed":
                return self.report()
            self.phase = "collecting"
            confirmed = attempt.get("pin_confirmed_at_s")
            if attempt.get("pin_confirm_required") and (confirmed is None or stamp <= confirmed):
                self._reset_window()
                self.data_quality = "waiting_pin_confirmation" if confirmed is None else "waiting_post_confirmation_sample"
                return self.report()
            if not attempt["conditions"]["in_range"]:
                self._reset_window()
                self.data_quality = "pin_zero_outside_tolerance"
                return self.report()
            self._window.append((float(stamp), angle))
            if attempt.get("check_mode") == "center_zero":
                attempt["zero_window_start_s"] = self._window[0][0]
            low = min(value for _, value in self._window)
            high = max(value for _, value in self._window)
            elapsed = self._window[-1][0] - self._window[0][0]
            attempt.update(pin_window_min_deg=low, pin_window_max_deg=high,
                           span_deg=high - low, stable_elapsed_s=elapsed)
            attempt["conditions"]["stable"] = elapsed + 1e-9 >= attempt["pin_hold_s"]
            if attempt["conditions"]["stable"]:
                self._finish("passed", "pin_confirmed_zero_held_five_seconds" if attempt.get("pin_confirm_required") else "zero_held_five_seconds")
                self._advance()
            return self.report()
        if self._baseline_started is None:
            self._baseline_started = float(stamp)
        if not self._baseline_closed:
            if stamp - self._baseline_started <= 0.5 + 1e-9 and torque is not None:
                self._baseline.append(abs(torque))
            if stamp - self._baseline_started >= 0.5 - 1e-9:
                self._baseline_closed = True
                attempt["baseline_nm"] = statistics.median(self._baseline) if self._baseline else None
        baseline = attempt["baseline_nm"]
        attempt["rise_nm"] = abs(torque) - baseline if torque is not None and baseline is not None else None
        attempt["conditions"]["torque"] = bool(torque is not None and baseline is not None
            and abs(torque) + 1e-9 >= self.options["contact_torque_nm"]
            and attempt["rise_nm"] + 1e-9 >= self.options["torque_rise_nm"])
        previous_peak = attempt["peak_angle_deg"]
        peak = angle if previous_peak is None else (max if attempt["direction"] == "upper" else min)(previous_peak, angle)
        attempt["peak_angle_deg"] = peak
        if peak != previous_peak:
            self._peak_sample_time = float(stamp)
        reference = attempt["reference_deg"]
        outside = peak > reference + 3 + 1e-9 if attempt["direction"] == "upper" else peak < reference - 3 - 1e-9
        attempt["conditions"]["no_overshoot"] = not outside
        attempt["conditions"]["in_range"] = abs(angle - reference) <= 3 + 1e-9
        if outside:
            self._reset_window()
            if attempt["status"] != "failed":
                self._finish("failed", "outer_reference_tolerance_exceeded")
            self.phase, self.paused = "failed", True
            return self.report()
        if self.paused or self._interrupted or attempt["status"] == "failed":
            self._reset_window()
            return self.report()
        self.phase = "collecting"
        if not attempt["conditions"]["in_range"]:
            self._reset_window()
            return self.report()
        self._window.append((float(stamp), angle))
        while len(self._window) > 1 and max(value for _, value in self._window) - min(value for _, value in self._window) > self.options["position_span_deg"] + 1e-9:
            self._window.pop(0)
        span = max(value for _, value in self._window) - min(value for _, value in self._window)
        elapsed = self._window[-1][0] - self._window[0][0]
        attempt.update(span_deg=span, stable_elapsed_s=elapsed)
        attempt["conditions"]["stable"] = elapsed + 1e-9 >= self.options["hold_s"]
        if attempt["conditions"]["stable"] and attempt["conditions"]["no_overshoot"]:
            self._finish("passed", "angle_and_stability_met_torque_auxiliary_only")
            self._advance()
        return self.report()

    def pause(self, paused):
        if type(paused) is not bool:
            raise ValueError("暂停标记须为布尔值")
        if self.current_index is None or self.phase == "failed" or self._interrupted:
            return self.report()
        self.paused, self.phase = paused, "paused" if paused else "collecting"
        if paused and self._attempt().get("check_mode") == "pin_zero":
            self._attempt()["pin_confirmed_at_s"] = None
        self._reset_window()
        return self.report()

    def confirm_pin(self, now_s):
        attempt = self._attempt()
        if (not attempt or attempt.get("check_mode") != "pin_zero" or attempt["status"] != "active"
                or self.paused or self._interrupted or attempt.get("pin_confirmed_at_s") is not None):
            raise ValueError("仅能在腰部插销零位检查中确认已插好销钉")
        if not _finite(now_s) or now_s < 0 or (self._last_now is not None and now_s < self._last_now):
            raise ValueError("插销确认时间无效")
        attempt["pin_confirmed_at_s"] = float(now_s)
        self.data_quality = "waiting_post_confirmation_sample"
        return self.report()

    def retest(self, index):
        if type(index) is not int or not 0 <= index < len(self.results):
            raise ValueError("重测序号须为计划端点序号")
        if self._interrupted:
            raise ValueError("本轮已中断，请重新开始一轮")
        attempt = self._attempt()
        if attempt and attempt["status"] == "active":
            self._finish("interrupted", "operator_retest_started")
            self.results[self.current_index]["status"] = "pending"
        self._activate(index)
        self.phase, self.paused = "ready", False
        return self.report()

    def skip(self):
        if self.current_index is None or self._interrupted:
            return self.report()
        if self._attempt()["status"] == "failed":
            self._activate(self.current_index)
        self._finish("skipped", "operator_skipped_not_passed")
        self._advance()
        return self.report()

    def continue_failed(self):
        if self.phase != 'failed' or self._interrupted:
            return self.report()
        attempt = self._attempt()
        attempt['failure_confirmation'] = {'difference_deg': attempt['peak_angle_deg'] - attempt['reference_deg']}
        self._advance()
        return self.report()

    def target_not_reached_angle(self):
        attempt = self._attempt()
        if attempt and attempt['status'] == 'active' and self._peak_sample_time is not None:
            peak = attempt['peak_angle_deg']
            if _target_not_reached(attempt['direction'], peak - attempt['reference_deg']):
                return peak
        return None

    def can_mark_target_not_reached(self):
        attempt = self._attempt()
        return bool(self.phase == 'collecting' and not self.paused and not self._interrupted
                    and attempt and attempt['current_angle_deg'] is not None
                    and self.target_not_reached_angle() is not None)

    def mark_target_not_reached(self):
        if not self.can_mark_target_not_reached():
            return self.report()
        attempt = self._attempt()
        self._reset_window()
        attempt.update(current_angle_deg=attempt['peak_angle_deg'], sample_time_s=self._peak_sample_time,
                       torque_nm=None, rise_nm=None)
        attempt['conditions'].update(in_range=False, torque=False)
        attempt['failure_confirmation'] = {'difference_deg': attempt['peak_angle_deg'] - attempt['reference_deg']}
        self._finish('failed', 'operator_target_not_reached')
        self._advance()
        return self.report()

    def interrupt(self, reason):
        if self.phase == "complete":
            return self.report()
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("中断原因不能为空")
        if self.interruption_reason is None:
            self.interruption_reason = reason
        self._interrupted, self.paused = True, True
        self._reset_window()
        if self.phase != "failed":
            self.phase = "paused"
        return self.report()

    def report(self):
        current = self._attempt()
        return copy.deepcopy(dict(schema_version=1, mode="manual_one_sided_endpoint_check",
            dof=self.dof, phase=self.phase, paused=self.paused, current_index=self.current_index,
            current=current, results=self.results, options=self.options,
            data_quality=self.data_quality, interruption_reason=self.interruption_reason,
            effective_rule="latest_attempt_supersedes_previous; interrupted_attempt_is_pending",
            source=dict(reference_source_kind=self.reference_source_kind, measurement_source_kind="caller_supplied",
                        torque_basis="caller_supplied_auxiliary", time_basis="monotonic_sample_times",
                        hardware_acceptance=False, plan_sha256=self.plan["plan_sha256"],
                        **({"excluded_joints": self.plan["excluded_joints"]} if "excluded_joints" in self.plan else {}),
                        plan_mode=self.plan["mode"])))


def validate_report(report, dof):
    """Validate a bounded pure-data report before transport/UI use."""
    if type(dof) is not int or dof not in (24, 28) or not isinstance(report, dict):
        raise ValueError("人工检查报告型号无效")
    try:
        encoded = json.dumps(report, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("人工检查报告不是有限JSON") from exc
    if len(encoded.encode()) > 8 * 1024**2:
        raise ValueError("人工检查报告超出大小限制")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1 or report.get("mode") != "manual_one_sided_endpoint_check" or report.get("dof") != dof:
        raise ValueError("人工检查报告协议无效")
    if not isinstance(report.get("phase"), str) or report["phase"] not in PHASES or type(report.get("paused")) is not bool:
        raise ValueError("人工检查报告阶段无效")
    if validate_options(report.get("options")) != report.get("options"):
        raise ValueError("人工检查报告参数未规范化")
    rows = report.get("results")
    if not isinstance(rows, list) or len(rows) not in (dof - 1, dof):
        raise ValueError("人工检查逐项结果未完整覆盖")
    names, joints = set(), set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != index or not isinstance(row.get("status"), str) or row["status"] not in STATUSES:
            raise ValueError("人工检查结果顺序或状态无效")
        if not isinstance(row.get("joint"), str) or not row["joint"] or row["joint"] in names:
            raise ValueError("人工检查关节名称无效")
        joint = row.get("joint_index")
        if type(joint) is not int or not 0 <= joint < dof or joint in joints or not isinstance(row.get("direction"), str) or row["direction"] not in {"upper", "lower", "center"} or not _finite(row.get("reference_deg")):
            raise ValueError("人工检查关节或参考值无效")
        names.add(row["joint"])
        joints.add(joint)
        attempts = row.get("attempts")
        if not isinstance(attempts, list):
            raise ValueError("人工检查尝试历史无效")
        for number, attempt in enumerate(attempts, 1):
            if not isinstance(attempt, dict) or type(attempt.get("attempt_id")) is not int or attempt["attempt_id"] != number or not isinstance(attempt.get("status"), str) or attempt["status"] not in {"active", "passed", "failed", "skipped", "interrupted"}:
                raise ValueError("人工检查尝试序号或状态无效")
            for key in ("index", "joint", "joint_index", "reference_deg", "direction"):
                if attempt.get(key) != row[key]:
                    raise ValueError("人工检查尝试身份无效")
            for key in REFERENCE_FIELDS:
                if key in row and attempt.get(key) != row[key]:
                    raise ValueError("人工检查尝试的标零侧来源不一致")
            for key in (*METRICS, "started_at_s", "ended_at_s"):
                if key not in attempt or (attempt[key] is not None and not _finite(attempt[key])):
                    raise ValueError("人工检查尝试数值无效")
            if not _finite(attempt.get("stable_elapsed_s")) or attempt["stable_elapsed_s"] < 0:
                raise ValueError("人工检查稳定时间无效")
            conditions = attempt.get("conditions")
            if not isinstance(conditions, dict) or set(conditions) != CONDITIONS or any(type(value) is not bool for value in conditions.values()):
                raise ValueError("人工检查条件格式无效")
            for key in ("sample_time_s", "started_at_s", "ended_at_s", "span_deg", "baseline_nm"):
                if attempt[key] is not None and attempt[key] < 0:
                    raise ValueError("人工检查时间、幅度或基线不能为负值")
            reason = attempt.get('reason')
            if 'failure_confirmation' in attempt or reason == 'operator_target_not_reached':
                confirmation = attempt.get('failure_confirmation')
                difference = confirmation.get('difference_deg') if isinstance(confirmation, dict) else None
                peak = attempt['peak_angle_deg']
                if (attempt['status'] != 'failed' or peak is None or attempt['sample_time_s'] is None
                        or not _finite(difference) or abs(difference - (peak - row['reference_deg'])) > 1e-9):
                    raise ValueError('已确认的不合格结果缺少超限实测证据' if reason == 'outer_reference_tolerance_exceeded'
                                     else '未达到目标的结果缺少有效实测证据')
                if reason == 'operator_target_not_reached':
                    if (not _target_not_reached(row['direction'], difference) or attempt['current_angle_deg'] != peak
                            or attempt['started_at_s'] is None or attempt['ended_at_s'] is None
                            or attempt['sample_time_s'] > attempt['ended_at_s'] + 1e-9
                            or conditions['in_range'] or conditions['stable'] or not conditions['no_overshoot']):
                        raise ValueError('未达到目标的结果缺少有效实测证据')
                elif (reason != 'outer_reference_tolerance_exceeded'
                      or not (row['direction'] == 'upper' and difference > 3 + 1e-9
                              or row['direction'] == 'lower' and difference < -3 - 1e-9)):
                    raise ValueError('已确认的不合格结果缺少超限实测证据')
            pin = row.get("check_mode") == "pin_zero"
            center = row.get("check_mode") == "center_zero"
            if pin or center:
                confirmed = attempt.get("pin_confirmed_at_s")
                if confirmed is not None and (not _finite(confirmed) or confirmed < 0):
                    raise ValueError("插销确认时间无效")
                allowed = (pin and row.get("joint") == "waist_roll_joint" and row.get("tolerance_deg") in (0.5, 2.0) and row.get("pin_confirm_required") is True) or (center and row.get("joint") in {"left_hip_pitch_joint", "right_hip_pitch_joint"} and row.get("tolerance_deg") == 2.0 and row.get("pin_confirm_required") is False)
                if not allowed or row.get("direction") != "center" or row.get("reference_deg") != 0 or row.get("pin_hold_s") != 5.0:
                    raise ValueError("零位检查参考无效")
                low, high = attempt.get("pin_window_min_deg"), attempt.get("pin_window_max_deg")
                if (low is None) != (high is None) or (low is not None and (not _finite(low) or not _finite(high) or low > high)):
                    raise ValueError("插销零位连续窗口无效")
                if attempt["status"] == "failed":
                    raise ValueError("插销零位超出范围应重新计时，不自动记为失败")
                if attempt["status"] == "passed":
                    angle, sample = attempt["current_angle_deg"], attempt["sample_time_s"]
                    window_start = confirmed if pin else attempt.get("zero_window_start_s")
                    if (not _finite(window_start) or window_start < 0 or sample is None or sample <= window_start or angle is None
                            or attempt["ended_at_s"] is None or attempt["started_at_s"] is None
                            or not attempt["conditions"]["in_range"] or not attempt["conditions"]["stable"]
                            or attempt["stable_elapsed_s"] + 1e-9 < 5.0 or low is None
                            or sample - window_start + 1e-9 < attempt["stable_elapsed_s"]
                            or attempt["ended_at_s"] + 1e-9 < sample
                            or low < -row["tolerance_deg"] - 1e-9 or high > row["tolerance_deg"] + 1e-9 or not low - 1e-9 <= angle <= high + 1e-9
                            or attempt["span_deg"] is None or abs(attempt["span_deg"] - (high - low)) > 1e-9):
                        raise ValueError("插销零位结果缺少确认后的有效角度证据")
            if attempt["status"] == "passed":
                if pin or center:
                    continue
                if (not all(conditions[key] for key in ("in_range", "stable", "no_overshoot"))
                        or any(attempt[key] is None for key in ("current_angle_deg", "peak_angle_deg", "span_deg"))
                        or attempt["started_at_s"] is None or attempt["ended_at_s"] is None
                        or abs(attempt["current_angle_deg"] - row["reference_deg"]) > 3 + 1e-9
                        or attempt["span_deg"] > report["options"]["position_span_deg"] + 1e-9
                        or attempt["stable_elapsed_s"] + 1e-9 < report["options"]["hold_s"]):
                    raise ValueError("人工检查通过状态缺少角度和稳定证据")
                peak = attempt["peak_angle_deg"]
                if (row["direction"] == "upper" and peak > row["reference_deg"] + 3 + 1e-9
                        or row["direction"] == "lower" and peak < row["reference_deg"] - 3 - 1e-9):
                    raise ValueError("人工检查越界历史不能判通过")
        effective = row.get("effective_attempt_id")
        expected = len(attempts) or None
        if effective != expected or (effective is not None and type(effective) is not int):
            raise ValueError("人工检查有效尝试无效")
        expected_status = attempts[-1]["status"] if attempts else "pending"
        if expected_status == "interrupted":
            expected_status = "pending"
        if row["status"] != expected_status:
            raise ValueError("人工检查有效状态与历史不一致")
    current = report.get("current_index")
    if current is None:
        if report["phase"] != "complete" or report["paused"] or report.get("current") is not None or not all(map(_outcome_complete, rows)):
            raise ValueError("人工检查完成状态不一致")
    elif type(current) is not int or not 0 <= current < len(rows) or not rows[current]["attempts"] or report.get("current") != rows[current]["attempts"][-1] or report["phase"] == "complete":
        raise ValueError("人工检查当前项与尝试历史不一致")
    if report["phase"] in {"ready", "collecting"} and (report["paused"] or rows[current]["status"] != "active"):
        raise ValueError("人工检查运行阶段与当前项不一致")
    if report["phase"] in {"paused", "failed"} and not report["paused"]:
        raise ValueError("人工检查暂停阶段与标记不一致")
    if report["phase"] == "failed" and (rows[current]["status"] != "failed" or _outcome_complete(rows[current])):
        raise ValueError("人工检查失败阶段与当前项不一致")
    source = report.get("source")
    if not isinstance(source, dict) or source.get("hardware_acceptance") is not False or not isinstance(source.get("reference_source_kind"), str) or source["reference_source_kind"] not in {"simulated", "urdf_model"}:
        raise ValueError("人工检查来源不能声称实机验收")
    expected_reference = "urdf_model" if source.get("plan_mode") == "manual_opposite_zero_limit_check" else "simulated"
    if source["reference_source_kind"] != expected_reference:
        raise ValueError("人工检查参考来源与计划类型不一致")
    digest = source.get("plan_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("人工检查参考计划指纹无效")
    if not isinstance(report.get("data_quality"), str) or (report.get("interruption_reason") is not None and not isinstance(report["interruption_reason"], str)):
        raise ValueError("人工检查数据质量或中断说明无效")
    if source.get("plan_mode") == "manual_opposite_zero_limit_check":
        try:
            from .manual_check_plan import (
                builtin_manual_plan,
                excluded_waist_manual_plan,
                legacy_pin_manual_plan,
                previous_manual_plan,
            )
        except ImportError:
            from manual_check_plan import (
                builtin_manual_plan,
                excluded_waist_manual_plan,
                legacy_pin_manual_plan,
                previous_manual_plan,
            )
        frozen = next((plan for plan in (builtin_manual_plan(dof), legacy_pin_manual_plan(dof), excluded_waist_manual_plan(dof), previous_manual_plan(dof))
                       if digest == plan["plan_sha256"]), None)
        if frozen is None:
            raise ValueError("人工检查参考指纹与本版本冻表不一致")
        if len(rows) != len(frozen["outcomes"]) or source.get("excluded_joints") != frozen.get("excluded_joints"):
            raise ValueError("人工检查覆盖范围与参考计划不一致")
        for row, reference in zip(rows, frozen["outcomes"]):
            if (row["joint"] != reference["joint"] or row["joint_index"] != reference["joint_index"]
                    or row["direction"] != reference["direction"]
                    or abs(row["reference_deg"] - math.degrees(reference["reference_rad"])) > 1e-9
                    or any(row.get(key) != reference.get(key) for key in REFERENCE_FIELDS)):
                raise ValueError("人工检查逐项参考、方向或标零侧来源与冻表不一致")
    elif len(rows) != dof:
        raise ValueError("人工检查逐项结果未完整覆盖")
    return copy.deepcopy(report)
