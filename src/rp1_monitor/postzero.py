"""PC records for explicit post-zero raw motor-output pose rechecks."""

import copy
import json
import math
import re
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

SCHEMA = "rp1.postzero.v1"
MAX_LINE_BYTES = 64 * 1024
MAX_RECORD_BYTES = 512 * 1024
CONFIGURATION_SHA256 = {
    24: "81f9a92fa9cf0e91273fbb5817acea2566a298fe8ee06eb5ef6c0e1fd8c5a9a5",
    28: "a91ef44a2d83083765802f75cfa5af83708ada9f9c5dccc356e6c7122dff877f",
}
EVENT_KEYS = {
    "metadata": {"type", "schema", "session_id", "dof", "source_kind", "position_basis",
                 "timestamp_basis", "firmware109_verified", "protocol_evidence", "started_monotonic_ns",
                 "motors", "configuration_sha256", "affinity", "mode",
                 "position_scale_rad", "position_scale_verified"},
    "sample": {"type", "session_id", "index", "round", "status", "angle_deg", "received_at",
               "query_sent_monotonic_ns", "rx_monotonic_ns", "raw_hex", "fault", "kernel_rx_timestamp",
               "interface", "motor_id", "model", "enabled"},
    "complete": {"type", "session_id", "cancelled", "finished_monotonic_ns"},
    "error": {"type", "session_id", "message"},
}


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("复查事件整数或范围无效")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("复查角度或偏移必须为有限数值")
    return float(value)


def validate_session_id(value):
    if not isinstance(value, str) or len(value) > 36:
        raise ValueError("复查session_id无效")
    try:
        UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError("复查session_id必须为UUID") from error
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("复查JSON包含重复字段")
        result[key] = value
    return result


def read_event(line):
    """Decode one bounded JSON object without accepting NaN or duplicate keys."""
    if isinstance(line, dict):
        line = json.dumps(line, allow_nan=False, ensure_ascii=False)
    if not isinstance(line, (str, bytes)):
        raise ValueError("复查事件必须为JSON对象")
    if len(line.encode("utf-8") if isinstance(line, str) else line) > MAX_LINE_BYTES:
        raise ValueError("复查事件超过64KiB限制")
    try:
        event = json.loads(line, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限JSON数值")))
    except (UnicodeError, RecursionError, TypeError) as error:
        raise ValueError("复查JSON格式无效") from error
    if not isinstance(event, dict) or event.get("type") not in {"metadata", "sample", "complete", "error"}:
        raise ValueError("复查事件类型无效")
    if set(event) - EVENT_KEYS[event["type"]]:
        raise ValueError("复查事件包含未识别字段")
    validate_session_id(event.get("session_id"))
    return event


def validate_metadata(event, dof=None, session_id=None, capture_mode=None):
    event = read_event(event)
    mode = event.get("mode", "query")
    expected_source = {"query": "lro_position_query", "passive": "lro_type1_passive"}.get(mode)
    expected_evidence = "local_deploy_type1_firmware109_layout" if mode == "passive" else "local_v1.02_and_upstream_query"
    if (event["type"] != "metadata" or event.get("schema") != SCHEMA
            or not expected_source or event.get("source_kind") != expected_source
            or event.get("position_basis") != "motor_output_raw_deg"
            or event.get("timestamp_basis") != "kernel_so_timestampns"
            or event.get("protocol_evidence") != expected_evidence
            or event.get("firmware109_verified") is not False):
        raise ValueError("复查来源或原始电机角度基准无效")
    if capture_mode is not None and mode != capture_mode:
        raise ValueError("反馈读取方式与本次选择不匹配")
    if mode == "passive":
        if (type(event.get("position_scale_rad")) not in (int, float)
                or event["position_scale_rad"] != 12.5 or event.get("position_scale_verified") is not False):
            raise ValueError("被动反馈位置量程证据无效")
    elif "position_scale_rad" in event or "position_scale_verified" in event:
        raise ValueError("独立查询不能混用Type1量程字段")
    _integer(event.get("dof"), 24, 28)
    if event["dof"] not in (24, 28) or (dof is not None and event["dof"] != dof):
        raise ValueError("复查机型不匹配")
    if session_id is not None and event["session_id"] != session_id:
        raise ValueError("复查会话不匹配")
    if event.get("configuration_sha256") != CONFIGURATION_SHA256[event["dof"]]:
        raise ValueError("复查配置哈希无效")
    _integer(event.get("started_monotonic_ns"), 1, 2**63 - 1)
    affinity = event.get("affinity")
    if (not isinstance(affinity, list) or not affinity
            or any(type(cpu) is not int or cpu not in {1, 2, 3, 7} for cpu in affinity)
            or len(set(affinity)) != len(affinity)):
        raise ValueError("复查CPU范围无效")
    motors = event.get("motors")
    if not isinstance(motors, list) or len(motors) != event["dof"]:
        raise ValueError("复查物理电机清单不完整")
    identities = set()
    expected = []
    counts = [7, 7, 5, 5] if event["dof"] == 24 else [7] * 4
    for bus_index, (interface, count) in enumerate(zip(("can1", "can2", "can0", "can3"), counts)):
        ids = [2, 3, 4, 5, 6, 7, 1] if bus_index < 2 else list(range(1, count + 1))
        expected.extend((interface, identity) for identity in ids)
    for index, motor in enumerate(motors):
        if not isinstance(motor, dict) or type(motor.get("index")) is not int or motor["index"] != index:
            raise ValueError("复查物理电机索引重复或乱序")
        if set(motor) != {"index", "interface", "motor_id", "model", "software_offset_rad"}:
            raise ValueError("复查物理电机身份字段无效")
        interface = motor.get("interface")
        if not isinstance(interface, str) or not re.fullmatch(r"can[0-9]+", interface):
            raise ValueError("复查总线名称无效")
        _integer(motor.get("motor_id"), 1, 8)
        _integer(motor.get("model"), 0, 3)
        _number(motor.get("software_offset_rad"))
        identity = interface, motor["motor_id"]
        if identity in identities:
            raise ValueError("复查物理电机身份重复")
        identities.add(identity)
        if identity != expected[index]:
            raise ValueError("复查物理电机清单与已审查配置不匹配")
    return event


def _sample_diagnostics(event, *, passive=False):
    if event["status"] == "fresh":
        return []
    if event["status"] == "timeout":
        return [("no_response", "等待窗口内未收到可用应答")]
    raw = bytes.fromhex(event.get("raw_hex") or "")
    result = []
    position_frame = (len(raw) in (8, 12) and raw[0] >> 5 == 1) if passive else (
        len(raw) == 6 and raw[0] >> 5 == 5 and raw[1] == 1)
    status = event.get("fault")
    raw_status = raw[0] & (0x07 if passive else 0x1F) if position_frame else None
    for value in dict.fromkeys(value for value in (status, raw_status) if value):
        result.append(("abnormal_status", f"应答状态非零（0x{value:02X}；状态含义待核对）"))
    if not position_frame:
        result.append(("unexpected_frame", "响应无效：原始帧不符合" + ("8/12字节Type1反馈格式" if passive else "6字节Type5位置应答格式")) if raw else
                      ("invalid_feedback", "响应无效：未保存可复核的原始应答"))
        return result
    if not passive and not math.isfinite(struct.unpack(">f", raw[2:])[0]):
        result.append(("nonfinite_position", "原始位置字段为非有限数值"))
    sent, received = event["query_sent_monotonic_ns"], event.get("rx_monotonic_ns")
    if not event["kernel_rx_timestamp"]:
        result.append(("invalid_receive_timing", "应答未满足内核接收时间/格式校验，未取得可接受的内核时间证据"))
    elif received is None or not isinstance(event.get("received_at"), str):
        result.append(("invalid_receive_timing", "内核接收时间信息不完整"))
    elif not 0 <= received - sent <= (1_000_000_000 if passive else 250_000_000):
        result.append(("invalid_receive_timing", "内核接收时间不在本次读取窗口内"))
    if not result:
        result.append(("invalid_feedback", "响应标记为无效；当前记录不足以确定原因"))
    return result


class PostzeroSession:
    def __init__(self, session_id, dof, robot_id, mechanical_reference=False,
                 tolerance_deg=3, *, trusted_capture=False, capture_mode="query"):
        self.session_id = validate_session_id(session_id)
        if type(dof) is not int or dof not in (24, 28):
            raise ValueError("复查仅支持24或28台物理电机")
        if not isinstance(robot_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", robot_id):
            raise ValueError("复查机器人身份无效")
        if type(mechanical_reference) is not bool or type(trusted_capture) is not bool:
            raise ValueError("机械基准与可信采集确认必须为布尔值")
        self.tolerance_deg = _number(tolerance_deg)
        if not 0 < self.tolerance_deg <= 3:
            raise ValueError("复查容差必须大于0且不超过3度")
        self.dof, self.robot_id = dof, robot_id
        if capture_mode not in ("query", "passive"):
            raise ValueError("反馈读取方式无效")
        self.capture_mode = capture_mode
        if capture_mode == "passive" and mechanical_reference:
            raise ValueError("被动监听仅作角度观测，不接受标零通过确认")
        self.mechanical_reference, self.trusted_capture = mechanical_reference, trusted_capture
        self.metadata = None
        self.events, self.received_monotonic_ns, self.samples, self.issues = [], [], {}, []
        self.completed = self.cancelled = False
        self._next_sample, self._last_rx, self._last_query, self._total_bytes = 0, 0, 0, 0
        self._last_rx_by_index = {}

    def invalidate(self, reason):
        self.issues.append(str(reason)[:512] or "复查已失效")

    abort = invalidate

    def ingest(self, event):
        try:
            event = read_event(event)
            if event["session_id"] != self.session_id or self.completed:
                raise ValueError("复查会话不匹配或完成后仍收到事件")
            if len(self.events) >= 3 * self.dof + 3:
                raise ValueError("复查事件数量超过限制")
            self._total_bytes += len(json.dumps(event, allow_nan=False).encode("utf-8"))
            if self._total_bytes > MAX_RECORD_BYTES:
                raise ValueError("复查记录超过512KiB限制")
            if event["type"] == "metadata":
                if self.metadata is not None or self.events:
                    raise ValueError("复查metadata重复或乱序")
                self.metadata = validate_metadata(event, self.dof, self.session_id, self.capture_mode)
                self._last_rx = self.metadata["started_monotonic_ns"]
                self._last_query = self._last_rx
            elif event["type"] == "error":
                if not isinstance(event.get("message"), str) or len(event["message"]) > 2048:
                    raise ValueError("复查错误事件格式无效")
                self.invalidate(event["message"])
            elif self.metadata is None and event["type"] != "complete":
                raise ValueError("复查事件缺少metadata")
            elif event["type"] == "sample":
                self._sample(event)
            elif event["type"] == "complete":
                if type(event.get("cancelled")) is not bool:
                    raise ValueError("复查结束状态无效")
                finished = _integer(event.get("finished_monotonic_ns"), 1, 2**63 - 1)
                if finished < max(self._last_rx, self._last_query) or (self.metadata and
                    finished - self.metadata["started_monotonic_ns"] > 30_001_000_000):
                    raise ValueError("复查结束时间无效或会话超过30秒")
                self.completed, self.cancelled = True, event["cancelled"]
            self.events.append(copy.deepcopy(event))
            self.received_monotonic_ns.append(time.monotonic_ns())
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            self.invalidate("无效复查事件：" + str(error))
            raise ValueError(str(error)) from error

    def _sample(self, event):
        index = _integer(event.get("index"), 0, self.dof - 1)
        round_number = _integer(event.get("round"), 1, 3)
        if (round_number - 1) * self.dof + index != self._next_sample:
            raise ValueError("复查样本重复、缺失或乱序")
        status = event.get("status")
        if status not in {"fresh", "timeout", "invalid"} or type(event.get("kernel_rx_timestamp")) is not bool:
            raise ValueError("复查样本质量字段无效")
        if self.capture_mode == "passive":
            if event.get("enabled") is not None and type(event["enabled"]) is not bool:
                raise ValueError("被动反馈使能状态无效")
        elif "enabled" in event:
            raise ValueError("独立查询不能混用Type1使能状态")
        sent = _integer(event.get("query_sent_monotonic_ns"), self.metadata["started_monotonic_ns"], 2**63 - 1)
        if sent < self._last_query or sent - self.metadata["started_monotonic_ns"] > 30_000_000_000:
            raise ValueError("复查query时间倒序或超出会话范围")
        for key in ("interface", "motor_id", "model"):
            if key in event and event[key] != self.metadata["motors"][index][key]:
                raise ValueError("复查样本物理电机身份不匹配")
        for key in ("angle_deg", "fault"):
            if event.get(key) is not None:
                _number(event[key]) if key == "angle_deg" else _integer(event[key], 0, 255)
        raw = event.get("raw_hex")
        if status == "timeout" and (event["kernel_rx_timestamp"] or raw not in (None, "") or any(
            event.get(key) is not None for key in ("angle_deg", "fault", "received_at", "rx_monotonic_ns")
        )):
            raise ValueError("超时样本不能携带伪造接收反馈")
        if raw is not None and (not isinstance(raw, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,64}", raw)):
            raise ValueError("复查原始反馈十六进制无效")
        if status == "fresh":
            rx = _integer(event.get("rx_monotonic_ns"), 1, 2**63 - 1)
            previous_rx = self._last_rx_by_index.get(index, self.metadata["started_monotonic_ns"]) if self.capture_mode == "passive" else self._last_rx
            if (not event["kernel_rx_timestamp"] or rx <= previous_rx or rx < sent
                    or rx - sent > (1_000_000_000 if self.capture_mode == "passive" else 250_000_000)
                    or event.get("angle_deg") is None or event.get("fault") != 0 or not raw):
                raise ValueError("fresh样本缺少合法内核接收时间、角度或无故障反馈")
            payload = bytes.fromhex(raw)
            if self.capture_mode == "passive":
                if (len(payload) not in (8, 12) or payload[0] >> 5 != 1
                        or payload[0] & 0x0F or type(event.get("enabled")) is not bool
                        or event["enabled"] != bool(payload[0] & 0x10)):
                    raise ValueError("被动原始反馈或使能状态不一致")
                raw_angle = math.degrees(((int.from_bytes(payload[1:3], "big") / 65535.0) * 25.0) - 12.5)
                if raw_angle != event["angle_deg"]:
                    raise ValueError("被动原始反馈与显示角度不一致")
            elif ("enabled" in event or len(payload) != 6 or payload[:2] != b"\xa0\x01"
                  or struct.unpack(">f", payload[2:])[0] != event["angle_deg"]):
                raise ValueError("复查raw反馈与Type5位置角度不一致")
            if not isinstance(event.get("received_at"), str):
                raise ValueError("复查原始接收时间无效")
            stamp = datetime.fromisoformat(event["received_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError("复查原始接收时间必须为UTC")
            self._last_rx = max(self._last_rx, rx)
            self._last_rx_by_index[index] = rx
        elif event.get("rx_monotonic_ns") is not None:
            _integer(event["rx_monotonic_ns"], 1, 2**63 - 1)
        self.samples[index, round_number] = (copy.deepcopy(event), time.monotonic_ns())
        self._last_query = sent
        self._next_sample += 1

    def report(self, *, live=False):
        now, rows, missing = time.monotonic_ns(), [], []
        motors = self.metadata["motors"] if self.metadata else [{"index": i} for i in range(self.dof)]
        for motor in motors:
            index = motor["index"]
            samples = [self.samples[index, r] for r in (1, 2, 3) if (index, r) in self.samples]
            absent = [r for r in (1, 2, 3) if (index, r) not in self.samples]
            if absent:
                missing.append({"index": index, "rounds": absent})
            angles = [sample[0]["angle_deg"] for sample in samples if sample[0]["status"] == "fresh"]
            angle = (angles[-1] if self.capture_mode == "passive" else sum(angles) / len(angles)) if angles else None
            spread = max(angles) - min(angles) if angles else None
            age = max(0, (now - samples[-1][1]) / 1e6) if samples else None
            diagnostics = {}
            for sample, _ in samples:
                for code, detail in _sample_diagnostics(sample, passive=self.capture_mode == "passive"):
                    diagnostics[code, detail] = diagnostics.get((code, detail), 0) + 1
            diagnostics = [{"code": code, "count": count, "detail": detail}
                           for (code, detail), count in diagnostics.items()]
            feedback_reason = "；".join(f'{item["count"]}次{item["detail"]}' for item in diagnostics)
            reason = ("会话已取消或失效" if self.cancelled or self.issues else
                      "缺少三轮完整样本" if len(samples) != 3 else
                      feedback_reason if feedback_reason else
                      "本机接收已过期" if live and age > 1500 else
                      "三轮角度跨度超过0.5度" if spread > 0.5 else
                      "采样尚未完整结束" if not self.completed else
                      "被动反馈仅作观测，不判定标零通过" if self.capture_mode == "passive" else
                      "未确认机械零位基准" if not self.mechanical_reference else
                      "导入或测试数据未经专用采集验证" if not self.trusted_capture else
                      "角度超出正负3度容差" if abs(angle) > self.tolerance_deg or any(abs(a) > self.tolerance_deg for a in angles) else "")
            if feedback_reason and reason != feedback_reason:
                reason += "；" + feedback_reason
            passed = False if self.cancelled or self.issues else not reason if self.mechanical_reference and self.trusted_capture else None
            rows.append({**copy.deepcopy(motor), "angle_deg": angle,
                         "reference_deg": 0.0 if self.mechanical_reference else None,
                         "deviation_deg": angle if self.mechanical_reference else None,
                         "sample_count": len(samples), "valid_sample_count": len(angles),
                         "timeout_count": sum(sample[0]["status"] == "timeout" for sample in samples),
                         "invalid_count": sum(sample[0]["status"] == "invalid" for sample in samples),
                         "diagnostics": diagnostics,
                         "spread_deg": spread, "live_age_ms": age,
                         "enabled": samples[-1][0].get("enabled") if self.capture_mode == "passive" and samples else None,
                         "status": "passed" if passed else "unverified" if passed is None else "attention",
                         "reason": reason, "pose_recheck_passed": passed})
        passed = (self.completed and not self.cancelled and not self.issues
                  and all(row["pose_recheck_passed"] is True for row in rows))
        summary = {"expected": 3 * self.dof,
                   "attempted": sum(row["sample_count"] for row in rows),
                   "valid": sum(row["valid_sample_count"] for row in rows),
                   "timeout": sum(row["timeout_count"] for row in rows),
                   "invalid": sum(row["invalid_count"] for row in rows)}
        summary["completion_state"] = (
            "cancelled" if self.cancelled else "invalidated" if self.issues else
            "collecting" if not self.completed else
            "no_valid_feedback" if summary["valid"] == 0 else
            "partial_feedback" if summary["valid"] < summary["expected"] else
            "unconfirmed" if not self.mechanical_reference or not self.trusted_capture else
            "invalidated" if live and any(row["live_age_ms"] > 1500 for row in rows) else
            "unstable" if any(row["spread_deg"] > 0.5 for row in rows) else
            "out_of_tolerance" if not passed else "passed"
        )
        return {"schema": SCHEMA, "session_id": self.session_id, "robot_id": self.robot_id,
                "dof": self.dof, "capture_mode": self.capture_mode,
                "position_basis": "motor_output_raw_deg", "hardware_ready": False,
                "mechanical_reference": self.mechanical_reference, "trusted_capture": self.trusted_capture,
                "tolerance_deg": self.tolerance_deg, "scope": "live" if live else "as_of_sampling",
                "completed": self.completed, "cancelled": self.cancelled,
                "status": "passed" if passed else "cancelled" if self.cancelled else "invalid" if self.issues else "complete" if self.completed else "collecting",
                "pose_recheck_passed": False if self.cancelled or self.issues else passed if self.mechanical_reference and self.trusted_capture else None,
                "metadata": copy.deepcopy(self.metadata), "events": copy.deepcopy(self.events),
                "pc_received_monotonic_ns": list(self.received_monotonic_ns),
                "rows": rows, "missing": missing, "issues": list(self.issues), "summary": summary}


def save_record(path, session):
    report = session.report(live=False) if isinstance(session, PostzeroSession) else copy.deepcopy(session)
    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        raise ValueError("只能保存本次标零后复查记录")
    report["saved_at"] = datetime.now(timezone.utc).isoformat()
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return report
