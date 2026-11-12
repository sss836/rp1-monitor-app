"""Judge a confirmed mechanical zero from independent, read-only encoder samples."""

import math
from copy import deepcopy

from . import calibration_mapping as mapping
from .joint_angle_monitor import ANKLE_SKEW_NS, RAW_FLOAT32_MAX, valid_raw_utc
from .postzero import CONFIGURATION_SHA256

LIMITS = {"raw_tolerance_deg": 3.0, "max_span_deg": 0.5,
          "required_samples": 3, "max_pair_skew_ms": ANKLE_SKEW_NS // 1_000_000}
READING_FIELDS = ("logical_measured_deg", "logical_delta_deg", "raw_angles_deg", "raw_span_deg")


class ZeroReferenceCheck:
    """Require three post-confirmation sweeps in the selected mechanical pose."""

    def __init__(self, dof):
        self.dof = dof
        # The bundled coordinate math is validated here. The SSH monitor checks
        # the running board's source and configuration before delivering frames.
        self._binding = mapping._encoder_observation_binding(mapping.version_bound_mapping(dof), policy_gains=False)
        self._binding["source_verified"] = True
        self.results = []
        for index, row in enumerate(self._binding["rows"]):
            joint, indices = row["name"], row["motor_indices"]
            reference = self._logical_deg(joint, indices, [0.0] * len(indices))
            motors = [{key: self._binding["motors"][motor][key] for key in ("index", "interface", "motor_id")}
                      for motor in indices]
            self.results.append(dict(index=index, joint=joint, motor_indices=list(indices), motors=motors,
                                     logical_reference_deg=reference, attempts=[]))
            self._reset(self.results[-1], "pending", "等待检查")
        self.current_index = 0
        self._confirmed_failures = set()
        self._last_sequence, self._last_frame_ns = 0, 0
        self._discard_next = False
        self._reset(self.results[0], "awaiting_confirmation", "请将当前关节放在已核对的机械零位并确认")

    def _current(self):
        return self.results[self.current_index] if self.current_index is not None else None

    @staticmethod
    def _reset(row, status, reason):
        row.update(dict.fromkeys(READING_FIELDS))
        row.update(status=status, reason=reason, samples=[], sample_count=0)

    @staticmethod
    def _record_attempt(row, status, reason):
        evidence = {key: row[key] for key in (*READING_FIELDS, "sample_count", "samples")}
        row["attempts"].append(deepcopy(dict(evidence, status=status, reason=reason)))

    def _logical_deg(self, joint, indices, raw):
        return math.degrees(mapping.encoder_joint_angle(self._binding, joint, dict(zip(indices, raw))))

    def report(self):
        results = deepcopy(self.results)
        current = results[self.current_index] if self.current_index is not None else None
        return dict(schema_version=1, mode="encoder_zero_reference_recheck", dof=self.dof,
                    phase=current["status"] if current else "complete", current_index=self.current_index,
                    current=current, results=results, limits=dict(LIMITS), source={
                        "kind": "version_bound_lro_encoder",
                        "reference_basis": "official_deploy_encoder_raw_zero",
                        "configuration_sha256": CONFIGURATION_SHA256[self.dof],
                        "timestamp_basis": "kernel_so_timestampns", "hardware_acceptance": False,
                    })

    def arm(self):
        row = self._current()
        if row and row["status"] == "awaiting_confirmation":
            self._reset(row, "collecting", "已确认机械零位，等待下一轮采样结束")
            self._discard_next = True
        return self.report()

    def invalidate(self, reason="反馈缺失，等待连续的新样本"):
        row = self._current()
        if row and row["status"] == "collecting":
            self._reset(row, "collecting", reason)
        return self.report()

    def pause(self):
        row = self._current()
        if row and row["status"] == "collecting":
            reason = "采样已暂停" if row["samples"] else "采样已暂停，未取得有效编码器样本"
            self._record_attempt(row, "interrupted", reason)
            self._reset(row, "awaiting_confirmation", "采样已暂停，继续前请重新确认机械零位")
        return self.report()

    def _advance(self):
        index = self.current_index
        order = list(range(index + 1, self.dof)) + list(range(index))
        self.current_index = next((i for i in order if i not in self._confirmed_failures
                                   and self.results[i]["status"] not in {"passed", "skipped"}), None)
        row = self._current()
        if row and row["status"] != "failed":
            self._reset(row, "awaiting_confirmation", "请将当前关节放在已核对的机械零位并确认")

    def skip(self):
        row = self._current()
        if row:
            # A previous failed verdict is already in the history.
            if row["status"] == "failed":
                self._reset(row, "skipped", "操作员跳过，当前项未完成")
            row.update(status="skipped", reason="操作员跳过，当前项未完成")
            self._record_attempt(row, row["status"], row["reason"])
            self._advance()
        return self.report()

    def continue_failed(self):
        row = self._current()
        if row and row["status"] == "failed":
            self._confirmed_failures.add(row["index"])
            self._advance()
        return self.report()

    def retest(self, index=None):
        if index is None:
            index = self.current_index
        if type(index) is not int or not 0 <= index < self.dof:
            raise ValueError("请选择要重新检查的关节")
        row = self._current()
        if row and row["status"] in {"awaiting_confirmation", "collecting"}:
            if row["samples"]:
                self._record_attempt(row, "interrupted", "操作员重新选择检查项")
            self._reset(row, "pending", "等待检查")
        self.current_index = index
        self._confirmed_failures.discard(index)
        self._reset(self.results[index], "awaiting_confirmation", "复测前请重新确认机械零位")
        return self.report()

    def feed(self, frame):
        if not isinstance(frame, dict):
            return self.invalidate()
        sequence, frame_ns = frame.get("sequence"), frame.get("frame_monotonic_ns")
        if (type(sequence) is not int or sequence <= 0 or type(frame_ns) is not int or not 0 < frame_ns < 2**63):
            return self.invalidate()
        if sequence == self._last_sequence and frame_ns == self._last_frame_ns:
            return self.report()
        if sequence <= self._last_sequence or frame_ns <= self._last_frame_ns:
            return self.invalidate("收到过期反馈，等待连续的新样本")
        previous_sequence, previous_frame_ns = self._last_sequence, self._last_frame_ns
        self._last_sequence, self._last_frame_ns = sequence, frame_ns
        row = self._current()
        if not row or row["status"] != "collecting":
            return self.report()
        if self._discard_next:
            self._discard_next = False
            row["reason"] = "等待三轮新鲜且稳定的编码器样本"
            return self.report()
        if sequence != previous_sequence + 1:
            self._reset(row, "collecting", "采样轮次中断，重新累计")
        return self._add_sample(row, frame, previous_frame_ns)

    def _add_sample(self, row, frame, previous_frame_ns):
        indices = row["motor_indices"]
        arrays = [frame.get(key) for key in ("raw_angles_deg", "raw_rx_monotonic_ns", "raw_received_at")]
        if any(not isinstance(values, list) or len(values) != self.dof for values in arrays):
            return self.invalidate()
        raw, stamps, utc = ([values[index] for index in indices] for values in arrays)
        frame_ns = frame["frame_monotonic_ns"]
        if (any(type(value) not in (int, float) or not -RAW_FLOAT32_MAX <= value <= RAW_FLOAT32_MAX for value in raw)
                or any(type(stamp) is not int or not previous_frame_ns < stamp <= frame_ns for stamp in stamps)
                or not all(valid_raw_utc(value) for value in utc)):
            return self.invalidate()
        if len(stamps) > 1 and not 0 < max(stamps) - min(stamps) <= ANKLE_SKEW_NS:
            return self.invalidate("踝部两台电机未同时取得有效反馈，等待新的成对样本")
        try:
            measured = self._logical_deg(row["joint"], indices, raw)
        except ValueError:
            measured = None
        delta = math.remainder(measured - row["logical_reference_deg"], 360.0) if measured is not None else None
        row["samples"].append(dict(sequence=frame["sequence"], frame_monotonic_ns=frame_ns,
                                   raw_angles_deg=raw, raw_rx_monotonic_ns=stamps, raw_received_at=utc,
                                   logical_measured_deg=measured, logical_delta_deg=delta))
        columns = zip(*(sample["raw_angles_deg"] for sample in row["samples"]))
        span = [max(values) - min(values) for values in columns]
        row.update(raw_angles_deg=raw, raw_span_deg=span, logical_measured_deg=measured,
                   logical_delta_deg=delta, sample_count=len(row["samples"]), reason="正在累计新鲜编码器样本")
        if row["sample_count"] < LIMITS["required_samples"]:
            return self.report()
        if any(abs(angle) > LIMITS["raw_tolerance_deg"]
               for sample in row["samples"] for angle in sample["raw_angles_deg"]):
            status, reason = "failed", "原始电机轴角超出 ±3°"
        elif any(value > LIMITS["max_span_deg"] for value in span):
            status, reason = "failed", "原始电机轴角跨度超过 0.5°"
        elif any(sample["logical_measured_deg"] is None for sample in row["samples"]):
            return self.invalidate("逻辑角换算未得到有效结果，等待新的有效反馈")
        else:
            status, reason = "passed", "三轮原始轴角及其稳定性均满足零位复查要求"
        row.update(status=status, reason=reason)
        self._record_attempt(row, status, reason)
        if status == "passed":
            self._advance()
        return self.report()
