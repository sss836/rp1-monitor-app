"""Current, editable factory summary; original evidence and test history stay intact."""

import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
from collections import Counter
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from .board_check_view import board_check_view
from .factory_board_profile import BoardTestProfile
from .factory_codec import decode_payload
from .models import metric_spec

EDITABLE_FIELDS = frozenset({"test_name", "started_at", "ended_at", "operator", "actual_result",
                             "criterion", "criterion_source", "verdict", "evidence", "notes"})
VERDICTS = frozenset({"未测试", "通过", "不通过", "待复核", "中断", "不适用"})
_STATUS = {"pass": "通过", "fail": "不通过", "incomplete": "待复核"}
_MOTOR_NAME = re.compile(r"^[0-9a-f-]{36}-[0-9a-f-]{36}\.json$")
_PROFILE_KEYS = {"RP003": ("ethernet_interfaces",), "RP011": ("usb_devices",),
                 "RP022": ("bms_firmware_versions",),
                 "RP029": ("serial_ports", "follow_bms_serial"),
                 "IMU_FIRMWARE": ("imu_firmware_versions",)}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@lru_cache(maxsize=1)
def _rules():
    return json.loads((Path(__file__).parent / "data/factory-report-rules.json").read_text(encoding="utf-8"))


def initialize(store):
    store.db.executescript("""
        CREATE TABLE IF NOT EXISTS report_edits (
            sn TEXT NOT NULL, row_key TEXT NOT NULL, source_token TEXT NOT NULL,
            fields TEXT NOT NULL, PRIMARY KEY(sn,row_key));
        CREATE TABLE IF NOT EXISTS report_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)


def report_settings(store):
    with store.lock:
        row = store.db.execute("SELECT value FROM report_settings WHERE key='station_id'").fetchone()
        return {"station_id": row[0] if row else ""}


def _human_source(binding):
    # A manual answer attached to a simulated sample must keep that visible label.
    return "simulated" if binding and binding.get("source_kind") == "simulated" else "human"


def _reference(store, table, identifier):
    return f"{store.directory / 'factory-index.sqlite'}#{table}:{identifier}"


def _stamp(row, evidence):
    """Use only explicitly supplied wall-clock boundaries, never confirmation times."""
    row["started_at"] = evidence.get("started_at") or ""
    row["ended_at"] = evidence.get("ended_at") or evidence.get("finished_at") or ""
    row["operator"] = evidence.get("operator") or ""


def _display(value):
    if value is None or value == "" or isinstance(value, (dict, list)):
        return "未知"
    if type(value) in (int, float):
        if not math.isfinite(value):
            return "未知"
        text = str(value)
        return text[:-2] if text.endswith(".0") else text
    return str(value)


def _manual_summary(head, *, label="人工结论", time_key="observed_at"):
    head = head or {}
    lines = [f"{label}：{_STATUS.get(head.get('status'), '未填写')}；确认时间：{_display(head.get(time_key))}。"]
    if head.get("notes"):
        lines.append("原备注：" + head["notes"])
    return "\n".join(lines)


def _range(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return "未知"
    return f"{_display(value[0])} ～ {_display(value[1])}"


def _motor_summary(result, dof, *, parameters):
    motors = result.get("rows") or []
    motors = [motor for motor in motors if isinstance(motor, dict)] if isinstance(motors, list) else []
    state = {"checked": "检查已返回", "requested": "已请求，等待结果", "updated": "已更新并读回，待复查",
             "partial": "部分更新", "unknown": "结果未知", "rejected": "操作未完成"}.get(result.get("state"), "未知")
    lines = [f"回执记录覆盖 {len(motors)}/{dof} 台；{state}；观察时间：{_display(result.get('observed_at'))}。"]
    for motor in motors:
        line = f"{_display(motor.get('address'))}：型号 {_display(motor.get('model'))}；FW {_display(motor.get('firmware'))}；HW {_display(motor.get('hardware'))}；协议 {_display(motor.get('protocol'))}。"
        if parameters:
            values = motor.get("parameters") or {}
            values = values if isinstance(values, dict) else {}
            readings = []
            differences = []
            for key in ("KP", "KD", "POS", "SPD", "TOR"):
                value = values.get(key) or {}
                if not isinstance(value, dict) or not value:
                    readings.append(key + "未读取")
                    differences.append(key)
                else:
                    readings.append(f"{key} 实际 {_range(value.get('actual'))}，期望 {_range(value.get('expected'))} {value.get('unit') or ''}".rstrip())
                    if value.get("matches") is not True:
                        differences.append(key)
            line += " 回执量程：" + "；".join(readings) + "。"
            if not differences:
                line += " KP/KD/POS/SPD/TOR一致。"
            line += f" CAN_TIMEOUT {_display(motor.get('can_timeout_ms'))} ms（只读记录，未判定合规）。"
        if motor.get("errors"):
            line += " 读取异常：" + "；".join(str(error) for error in motor["errors"])
        lines.append(line)
    if result.get("detail"):
        lines.append("回执说明：" + str(result["detail"]))
    return "\n".join(lines)


def _motor_rule(motors):
    groups = {}
    for motor in motors:
        values = motor.get("parameters", {})
        text = "；".join(f"{key} {_range(values.get(key, {}).get('expected'))} {values.get(key, {}).get('unit') or ''}".rstrip()
                         for key in ("KP", "KD", "POS", "SPD", "TOR"))
        groups.setdefault(text, []).append(_display(motor.get("address")))
    return "\n本次驱动期望量程：\n" + "\n".join("、".join(addresses) + "：" + text for text, addresses in groups.items())


def _calibration_summary(report):
    rows = report["results"]
    counts = Counter(item["status"] for item in rows)
    unfinished = len(rows) - counts["passed"] - counts["failed"]
    lines = [f"逐关节结果：通过{counts['passed']}项；失败{counts['failed']}项；未完成{unfinished}项（跳过{counts['skipped']}项）。"]
    for item in rows:
        attempt = next((entry for entry in item.get("attempts", []) if entry.get("attempt_id") == item.get("effective_attempt_id")), {})
        reference = attempt.get("reference_deg", item.get("reference_deg"))
        zero = attempt.get("check_mode", item.get("check_mode")) in {"pin_zero", "center_zero"}
        angle = attempt.get("current_angle_deg" if zero else "peak_angle_deg")
        deviation = (angle - reference if type(angle) in (int, float) and type(reference) in (int, float)
                     and math.isfinite(angle) and math.isfinite(reference) else None)
        status = {"passed": "通过", "failed": "失败", "skipped": "跳过", "active": "检查中", "pending": "未测"}.get(item["status"], "未知")
        direction = {"upper": "上限", "lower": "下限", "center": "零位"}.get(item.get("direction"), "方向未知")
        line = f"{item['joint']}（{direction}）：{status}；有效参考 {_display(reference)}°；"
        if zero:
            line += f"当前角度 {_display(angle)}°；相对零位偏差 {_display(deviation)}°"
            low, high, elapsed = (attempt.get(key) for key in ("pin_window_min_deg", "pin_window_max_deg", "stable_elapsed_s"))
            if low is not None or high is not None:
                line += f"；零位窗口 {_display(low)}° ～ {_display(high)}°"
            if elapsed is not None:
                line += f"；原记录稳定时长 {_display(elapsed)}秒"
        else:
            line += f"峰值 {_display(angle)}°；峰值偏差 {_display(deviation)}°"
        lines.append(line + "。")
    if report.get("interruption_reason"):
        lines.append("中断原因：" + str(report["interruption_reason"]))
    return "\n".join(lines)


def _calibration_rule(report):
    options = report.get("options") or {}
    text = (f"\n本次检查参数：限位容差 ±{_display(options.get('tolerance_deg'))}°；稳定时间 ≥{_display(options.get('hold_s'))}秒；"
            f"稳定窗口跨度 ≤{_display(options.get('position_span_deg'))}°；力矩仅辅助记录。")
    zero_rules = {(item.get("tolerance_deg"), item.get("pin_hold_s")) for item in report["results"]
                  if item.get("check_mode") in {"pin_zero", "center_zero"}}
    for tolerance, hold in sorted(zero_rules, key=str):
        text += f"\n本次零位判据：容差 ±{_display(tolerance)}°；保持 ≥{_display(hold)}秒；插销确认按原报告要求。"
    return text + "\n逐关节有效参考见实际结果及原始证据。"


def _motion_description(segments, summaries):
    lines = []
    for index, segment in enumerate(segments, 1):
        summary = summaries[segment["segment_id"]]
        state = {"sealed": "已结束", "interrupted": "中断", "fault": "采集异常", "recording": "记录中"}.get(segment.get("state"), "未知")
        lines.append(f"第{index}段：{state}；采集起始 {_display(segment.get('started_at'))}；结束 {_display(segment.get('ended_at'))}；单调采集时长 {_display(summary['duration_seconds'])}秒。")
        valid, total = segment.get("valid_counts", {}), segment.get("sample_counts", {})
        lines.append(f"有效采样：运动 {_display(valid.get('motion'))}/{_display(total.get('motion'))}；健康 {_display(valid.get('health'))}/{_display(total.get('health'))}。")
        data = summary["key_data_summary"]
        if summary["error"]:
            lines.append("关键统计读取异常：" + summary["error"])
        elif not data:
            lines.append("本段未提供关键统计摘要。")
        else:
            lines.append(f"已记录异常 {_display(data.get('exception_count'))}次；统计为采集端观察值。")
            for key, values in data.get("metrics", {}).items():
                try:
                    label = metric_spec(key)["label"]
                except (KeyError, IndexError):
                    label = key
                unit = values.get("unit") or ""
                lines.append(f"{label}：最小 {_display(values.get('minimum'))}，最大 {_display(values.get('maximum'))}，均值 {_display(values.get('mean'))} {unit}；有效 {_display(values.get('valid'))}次，缺失 {_display(values.get('missing'))}次；最大有效间隔 {_display(values.get('maximum_valid_gap_seconds'))}秒。")
    lines.append("时长满足不证明动作执行或性能合格，须人员填写结论。")
    return "\n".join(lines)


def _board_rule(row, head, frozen, profile):
    details = head.get("details") or {}
    original = {key: details[key] for key in ("limit_c", "required", "expected", "mismatches",
                "min_speed_mbps", "minimum_speed_mbps") if key in details}
    if original:
        row["criterion"] += "\n本次原报告判据/差异：" + _json(original)
    keys = _PROFILE_KEYS.get(row["source_id"], ())
    configured = {key: profile[key] for key in keys if key in profile}
    if frozen and configured:
        row["criterion"] += "\n本次冻结标准：" + _json(configured)
    if frozen:
        row["criterion_source"] = ("本次冻结主控评估；标准：" + profile.get("name", "未命名")
                                   + "；" + row["criterion_source"])
    version = head.get("tui_version") or head.get("executor_version")
    if version:
        row["criterion_source"] += "；原报告执行版本：" + str(version)


def _board_rows(store, rows, board):
    if not board:
        return
    assessment = board.get("assessment") or {}
    checks = {entry["id"]: entry["latest"] for entry in board["checks"]}
    children = [row for row in rows if row["case_code"] == "SYS-001" and row["level"] == "子项"]
    for row in children:
        head = checks.get(row["source_id"])
        if not head:
            continue
        digest = hashlib.sha256(_json(head).encode()).hexdigest()
        frozen = ((assessment.get("checks") or {}).get(row["source_id"])
                  if assessment.get("inputs", {}).get(row["source_id"]) == digest else None)
        profile = (assessment.get("profile") or {}) if frozen else {}
        view = frozen or vars(board_check_view(row["source_id"], head))
        row["source_kind"] = head.get("source_kind") if head.get("source_kind") in {"ros2", "simulated"} else ""
        _stamp(row, head)
        row["actual_result"] = "\n".join(filter(None, [view.get("summary", ""), *view.get("readings", [])]))
        original = {"pass": "通过", "fail": "不通过", "error": "读取错误", "needs_review": "待复核", "skip": "未执行"}.get(head.get("result"), "未知")
        row["actual_result"] += f"\n原报告结论：{original}；报告时间：{_display(head.get('created_at'))}。"
        row["verdict"] = {"通过": "通过", "不通过": "不通过"}.get(view.get("label"), "待复核")
        if row["source_id"] in {"RP022", "IMU_FIRMWARE"}:
            allowed = profile.get("bms_firmware_versions" if row["source_id"] == "RP022" else "imu_firmware_versions")
            if not allowed and row["verdict"] == "通过":
                row["verdict"] = "待复核"
                row["actual_result"] += "\n允许版本清单未提供，读取成功不代表版本合规。"
        _board_rule(row, head, frozen, profile)
        row["evidence"] = _reference(store, "board_evidence", head["report_id"] + "/" + row["source_id"])
        row["_inputs"] = [head, frozen, {key: profile.get(key) for key in _PROFILE_KEYS.get(row["source_id"], ())}]
    parent = next(row for row in rows if row["row_key"] == "SYS-001")
    if not any(row["_inputs"] for row in children):
        return
    counts = Counter(row["verdict"] for row in children)
    parent.update(source_kind=board["source_kind"],
                  actual_result="；".join(f"{key}{counts[key]}项" for key in ("通过", "不通过", "待复核", "未测试"))
                  + "。父项目结论由人员复核确认。",
                  verdict="不通过" if counts["不通过"] else "待复核",
                  evidence="\n".join(row["evidence"] for row in children if row["evidence"]))
    # Legacy item 1 includes lights and motor checks; it cannot certify this parent.
    parent["_inputs"] = [row["_inputs"] for row in children]


def _motor_attachments(store, sn):
    """Read only bounded, native parameter-report attachments, never arbitrary logs."""
    metadata = store.db.execute("SELECT payload FROM attachments WHERE sn=? AND item=1 ORDER BY rowid DESC", (sn,))
    for entry in metadata:
        attachment = json.loads(entry[0])
        if not _MOTOR_NAME.fullmatch(attachment.get("name", "")):
            continue
        path = store.attachments / attachment["attachment_id"]
        try:
            # Native receipts are bounded to 512 KiB; requested before-images may double that.
            if attachment["bytes"] > 2 * 1024**2:
                raise ValueError("参数附件超出摘要读取上限")
            # Hash only this small JSON, never a recording or a user-supplied large file.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_size != attachment["bytes"]
                        or info.st_size > 2 * 1024**2):
                    raise ValueError("参数附件字节数不符")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(2 * 1024**2 + 1)
                if hashlib.sha256(raw).hexdigest() != attachment["sha256"]:
                    raise ValueError("参数附件校验失败")
                body = json.loads(raw)
            finally:
                os.close(descriptor)
            if (not isinstance(body, dict) or body.get("sn") != sn
                    or not isinstance(body.get("request"), dict) or not isinstance(body.get("result"), dict)
                    or body["request"].get("operation") not in {"check", "update"}):
                return [(attachment, body if isinstance(body, dict) else {}, "参数附件结构或SN归属不匹配")]
            return [(attachment, body, "")]
        except (OSError, ValueError) as error:
            return [(attachment, {}, str(error))]
    return []


def _motor_rows(store, by_key, reports, binding, dof):
    from .motor_parameters_remote import validate_request

    if not reports:
        return
    attachment, body, error = reports[-1]
    result, request = body.get("result", {}), body.get("request", {})
    try:
        validate_request(dict(request))
        request_valid = True
    except (ValueError, TypeError, AttributeError, KeyError) as validation_error:
        request_valid = False
        error = error or "参数请求校验失败：" + str(validation_error)
    identity_matches = request_valid and _motor_identity_matches(request, result, binding, dof)
    for code in ("SYS-003", "SYS-004"):
        row = by_key[code]
        row.update(source_kind=binding["source_kind"] if identity_matches else "", verdict="待复核",
                   evidence=str(store.attachments / attachment["attachment_id"]))
        row["_inputs"] = [attachment["sha256"], body, error]
        if error:
            row["actual_result"] = "参数附件无法确认：" + error + "\n" + _motor_summary(result, dof, parameters=code == "SYS-004")
            continue
        _stamp(row, result)
        row["actual_result"] = _motor_summary(result, dof, parameters=code == "SYS-004")
        row["notes"] += " 原回执观察时间：" + str(result.get("observed_at") or "未知") + "。"
        row["criterion_source"] += "；本次配置SHA256：" + str(result.get("configuration_sha256") or "未知")
        row["criterion_source"] += "；本次驱动二进制SHA256：" + str(result.get("binary_sha256") or "未知")
        if not identity_matches:
            row["actual_result"] += "\n归属不匹配或尚未确认，待复核；请求编号、机器人、机型及本SN绑定须完整一致。"
            continue
        motors = result.get("rows") or []
        if result.get("state") != "checked":
            row["actual_result"] += "\n本次尚无完整复查结果；旧before快照不作为当前核查。"
            continue
        if (not isinstance(motors, list) or any(not isinstance(motor, dict)
                or not isinstance(motor.get("parameters"), dict)
                or any(not isinstance(value, dict) for value in motor["parameters"].values()) for motor in motors)):
            row["actual_result"] += "\n逐电机回执结构无效，待复核。"
            continue
        addresses = [motor.get("address") for motor in motors]
        complete = (len(motors) == dof and all(isinstance(address, str) and address for address in addresses)
                    and len(set(addresses)) == dof
                    and request.get("dof") == dof and result.get("dof", dof) == dof
                    and request.get("robot_id") == result.get("robot_id"))
        if code == "SYS-003":
            # Only the profile frozen into this receipt belongs to this attempt.
            try:
                frozen = (BoardTestProfile.model_validate(body["board_test_profile"]).model_dump(mode="json")
                          if body.get("board_test_profile") is not None else {})
            except ValueError:
                row["actual_result"] += "\n本附件冻结标准格式无效，版本合规待复核。"
                continue
            allowed = frozen.get("motor_firmware_versions", [])
            row["_inputs"].append(allowed)
            if allowed:
                row["criterion"] += "\n本次冻结电机允许固件版本：" + _json(allowed)
                row["criterion_source"] += "；本附件冻结主控标准：" + frozen["name"]
                actual = [motor.get("firmware") for motor in motors]
                if any(isinstance(value, str) and value and value not in allowed for value in actual):
                    row["verdict"] = "不通过"
                elif complete and all(isinstance(value, str) and value in allowed for value in actual):
                    row["verdict"] = "通过"
            else:
                row["actual_result"] += "\n允许版本清单未提供，版本读取不自动判合规。"
        else:
            row["criterion"] += _motor_rule(motors)
            if any(value.get("matches") is False for motor in motors for value in motor.get("parameters", {}).values()):
                row["verdict"] = "不通过"
            row["actual_result"] += "\nMIT量程一致仅表示编码范围核对结果，整项结论待人员复核。"
        if not complete:
            row["actual_result"] += f"\n覆盖 {len(motors)}/{dof} 台，完整性待复核。"
            if row["verdict"] == "通过":
                row["verdict"] = "待复核"


def _motor_identity_matches(request, result, binding, dof):
    return bool(binding and request.get("robot_id") == result.get("robot_id") == binding.get("robot_id")
                and request.get("request_id") == result.get("request_id")
                and request.get("dof") == result.get("dof") == dof
                and type(result.get("dof")) is int)


def motor_parameter_report(store, sn):
    """The latest checked receipt, only after native hash and SN/robot validation."""
    from .motor_parameters_remote import validate_request

    sn = store.validate_sn(sn)
    with store.lock:
        reports = _motor_attachments(store, sn)
        if not reports:
            return None
        attachment, body, error = reports[-1]
        if error:
            return None
        request, result = body["request"], body["result"]
        try:
            validate_request(dict(request))
        except (ValueError, TypeError, AttributeError, KeyError):
            return None
        binding, dof = store.sn_identity(sn), store.model_dof(sn)
        if not _motor_identity_matches(request, result, binding, dof) or result.get("state") != "checked":
            return None
        motors = result.get("rows")
        if (not isinstance(motors, list) or not 0 < len(motors) <= dof
                or any(not isinstance(motor, dict) or not isinstance(motor.get("address"), str)
                       or not motor["address"] or not isinstance(motor.get("parameters"), dict)
                       or any(not isinstance(value, dict) for value in motor["parameters"].values())
                       for motor in motors)
                or len({motor["address"] for motor in motors}) != len(motors)):
            return None
        return {"attachment_id": attachment["attachment_id"], "sha256": attachment["sha256"],
                "source_kind": binding["source_kind"], "robot_id": binding["robot_id"],
                "request_id": request["request_id"], "dof": dof, "result": result}


def _motion_summary(store, segment):
    from .factory_store import _open_evidence

    path = store.packages / segment["segment_id"] / "segment.sqlite"
    try:
        with _open_evidence(path) as database:
            metadata = dict(database.execute("SELECT key,value FROM metadata WHERE key IN ('recording_duration_seconds','key_data_summary')"))
        summary = decode_payload(metadata["key_data_summary"]) if "key_data_summary" in metadata else {}
        duration = json.loads(metadata["recording_duration_seconds"]) if "recording_duration_seconds" in metadata else summary.get("duration_seconds")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            duration = None
        return dict(duration_seconds=duration, key_data_summary=summary, error="")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as error:
        return dict(duration_seconds=None, key_data_summary={}, error=str(error))


def _motion_row(store, row, segments, summaries, conclusion, *, stunt=False):
    if not segments:
        return
    row["source_kind"] = "simulated" if any(s["source_kind"] == "simulated" for s in segments) else segments[-1]["source_kind"]
    newest = segments[-1]
    _stamp(row, newest)
    measured = [{**segment, "summary": summaries[segment["segment_id"]]} for segment in segments]
    row["actual_result"] = _motion_description(segments, summaries)
    row["evidence"] = "\n".join(str(store.packages / segment["segment_id"] / "manifest.json") for segment in segments)
    row["verdict"] = "中断" if newest["state"] in {"fault", "interrupted"} else "待复核"
    ids = {segment["segment_id"] for segment in segments}
    relevant = conclusion if conclusion and set(conclusion.get("segment_ids", [])) == ids else None
    if relevant:
        row["actual_result"] += "\n" + _manual_summary(relevant, label="关联人工结论", time_key="created_at")
        row["operator"] = relevant.get("operator", "")
        if not stunt:
            row["actual_result"] += "\n旧第4阶段结论仅供参考，不作为本运动项目的独立通过或不通过结论。"
            row["_inputs"] = [[{key: value for key, value in record.items() if key != "imported_at"}
                               for record in measured], relevant]
            return
        enough = all(summaries[segment["segment_id"]]["duration_seconds"] is not None
                     and summaries[segment["segment_id"]]["duration_seconds"] > 7200 for segment in segments)
        if relevant["status"] == "fail":
            row["verdict"] = "不通过"
        elif relevant["status"] == "pass" and enough and all(segment["state"] == "sealed" for segment in segments):
            row["verdict"] = "通过"
        elif relevant["status"] == "pass":
            row["actual_result"] += "\n已有人工通过结论，但记录时长或完整性尚不满足，汇总待复核。"
    row["_inputs"] = [[{key: value for key, value in record.items() if key != "imported_at"}
                       for record in measured], relevant]


def report_rows(store, sn):
    sn = store.validate_sn(sn)
    with store.lock:
        board = store.board_snapshot(sn)
        functions, pairing = store.functional_checks(sn), store.estop_pairing(sn)
        lights, confirmations = store.light_observations(sn), store.calibration_confirmations(sn)
        calibration, soc = store.calibration_check(sn), store.soc_observations(sn)
        segments = store.list_segments(sn)
        model = store.db.execute("SELECT dof FROM sn_models WHERE sn=?", (sn,)).fetchone()
        dof = model["dof"] if model else calibration["report"]["dof"] if calibration else 24
        identity = board or (segments[0] if segments else soc[0] if soc else None)
        binding = {key: identity[key] for key in ("robot_id", "source_kind")} if identity else None
        summaries = {segment["segment_id"]: _motion_summary(store, segment) for segment in segments}
        conclusions = {entry["item"]: json.loads(entry["payload"]) for entry in store.db.execute(
            "SELECT item,payload FROM results WHERE sn=? ORDER BY rowid", (sn,))}
        settings = report_settings(store)
        rows = []
        for rule in _rules()["rows"]:
            row = dict(rule)
            row_key = row["case_code"] + ("/" + row["source_id"] if row["level"] == "子项" else "")
            row.update(row_key=row_key, sample_id=sn, station_id=settings["station_id"], model=f"{dof}轴",
                       actual_result="未取得本项证据（未知）。", edited=False, _inputs=[])
            rows.append(row)
        by_key = {row["row_key"]: row for row in rows}
        _board_rows(store, rows, board)
        _motor_rows(store, by_key, _motor_attachments(store, sn), binding, dof)
        if lights:
            head, row = lights[-1], by_key["SYS-002"]
            row.update(source_kind=_human_source(binding), verdict="通过" if head["lit"] else "不通过",
                       actual_result="现场人员确认灯板" + ("亮灯。" if head["lit"] else "未亮灯。") + "确认时间：" + head["observed_at"] + "。",
                       evidence=_reference(store, "light_observations", head["observed_at"]), _inputs=[head])
        if confirmations or conclusions.get(2):
            row = by_key["SYS-005"]
            head = conclusions.get(2)
            confirmation = confirmations[-1] if confirmations else None
            process = ("已完成（人员确认）" if confirmation["completed"] else "未完成（人员确认）") if confirmation else "未记录流程确认"
            row.update(source_kind=_human_source(binding), verdict=_STATUS.get((head or {}).get("status"), "待复核"),
                       actual_result="标定流程：" + process + "；确认时间：" + _display((confirmation or {}).get("observed_at")) + "。\n"
                       + _manual_summary(head, label="验收结论", time_key="created_at")
                       + "\n流程完成不等于通过，未提供逐轴验收条件保持待复核。",
                       evidence=_reference(store, "results", (head or {}).get("result_id", "")),
                       _inputs=[confirmations[-1] if confirmations else None, head])
        if calibration or conclusions.get(3):
            row, head = by_key["SYS-006"], conclusions.get(3)
            row.update(source_kind=_human_source(binding), verdict=_STATUS.get((head or {}).get("status"), "待复核"),
                       actual_result=_manual_summary(head, label="标定检查人工结论", time_key="created_at"),
                       evidence=_reference(store, "calibration_checks", (calibration or {}).get("run_id", "")),
                       _inputs=[calibration, head])
            if calibration:
                report = calibration["report"]
                row["actual_result"] += "\n" + _calibration_summary(report)
                row["criterion"] += _calibration_rule(report)
                row["criterion_source"] += "；原报告计划SHA256：" + str(report.get("source", {}).get("plan_sha256") or "未知")
                statuses = {item["status"] for item in report["results"]}
                if "failed" in statuses:
                    row["verdict"] = "不通过"
                elif report.get("interruption_reason"):
                    row["verdict"] = "中断"
                elif statuses != {"passed"} and row["verdict"] == "通过":
                    row["verdict"] = "待复核"
                row["notes"] += " 原报告来源：" + str(calibration.get("source_kind") or "未知") + "；保存时刻：" + str(calibration.get("saved_at") or "未知") + "。"
        for kind, check in functions.items():
            head = check["latest"]
            related_soc = soc if kind == "battery_hot_swap" else []
            if not head and not related_soc:
                continue
            row = by_key["SYS-007" if kind == "remote_estop" else "SYS-008"]
            linked_segments = len((head or {}).get("segment_ids", []))
            linked_soc = len((head or {}).get("soc_observation_ids", []))
            row.update(source_kind=_human_source(binding) if head else (binding or {}).get("source_kind", ""),
                       verdict=_STATUS.get((head or {}).get("status"), "待复核"),
                       actual_result=_manual_summary(head) + f"\n关联采集片段 {linked_segments}段；本SN已保存SOC证据 {len(related_soc)}条，本次结论关联 {linked_soc}条。",
                       evidence=_reference(store, "functional_checks", (head or {}).get("check_id", "")),
                       _inputs=[head, related_soc])
            if related_soc:
                row["evidence"] += "\n" + "\n".join(str(store.attachments / entry["attachment_id"]) for entry in related_soc)
                row["notes"] += " SOC变化仅为辅助证据，不证明已换电、主控不断电或通过。"
        if pairing["latest"]:
            head, row = pairing["latest"], by_key["SYS-012"]
            row.update(source_kind=_human_source(binding), verdict=_STATUS[head["status"]],
                       actual_result=_manual_summary(head, label="人员对频测试结论"),
                       evidence=_reference(store, "estop_pairing_checks", head["check_id"]), _inputs=[head])
        for kind, key in (("standing", "SYS-009"), ("walking", "SYS-010/walking"),
                          ("running", "SYS-010/running"), ("stunt", "SYS-011")):
            selected = [segment for segment in segments if segment["test_type"] == kind]
            _motion_row(store, by_key[key], selected, summaries, conclusions.get(5 if kind == "stunt" else 4), stunt=kind == "stunt")
        walking, running, parent = by_key["SYS-010/walking"], by_key["SYS-010/running"], by_key["SYS-010"]
        if walking["_inputs"] or running["_inputs"]:
            verdicts = {walking["verdict"], running["verdict"]}
            parent.update(source_kind="simulated" if "simulated" in {walking["source_kind"], running["source_kind"]} else walking["source_kind"] or running["source_kind"],
                          actual_result="行走：" + walking["actual_result"] + "\n跑步：" + running["actual_result"],
                          verdict="不通过" if "不通过" in verdicts else "中断" if "中断" in verdicts else "通过" if verdicts == {"通过"} else "待复核",
                          evidence="\n".join(filter(None, (walking["evidence"], running["evidence"]))),
                          _inputs=[walking["_inputs"], running["_inputs"]])
        if dof == 28:
            by_key["SYS-011"].update(verdict="不适用", actual_result="本SN本地机型为28轴，UFO特技不适用。")
        edits = {entry["row_key"]: entry for entry in store.db.execute("SELECT row_key,source_token,fields FROM report_edits WHERE sn=?", (sn,))}
        for row in rows:
            inputs = row.pop("_inputs")
            token = hashlib.sha256(_json([sn, row["row_key"], dof, row["source_kind"], inputs, row["criterion"], row["criterion_source"]]).encode()).hexdigest()
            row.update(source_token=token, record_id=str(uuid5(NAMESPACE_URL, "rp1-result:" + sn + ":" + row["row_key"] + ":" + token)))
            edit = edits.get(row["row_key"])
            if edit and edit["source_token"] == token:
                row.update(json.loads(edit["fields"]))
                row["edited"] = True
            if dof == 28 and row["row_key"] == "SYS-011":
                row["verdict"] = "不适用"
        return rows


def save_report_edits(store, sn, edits, station_id=""):
    if (not isinstance(station_id, str) or len(station_id) > 64
            or any(ord(char) < 32 or ord(char) == 127 for char in station_id)):
        raise ValueError("上位机编号应不超过64字且不含控制字符")
    if not isinstance(edits, list):
        raise ValueError("结果修改必须为列表")
    if isinstance(sn, str) and not sn.strip() and not edits:
        with store.lock, store.db:
            store.db.execute("INSERT INTO report_settings VALUES('station_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (station_id.strip(),))
        return []
    sn = store.validate_sn(sn)
    with store.lock:
        current = {row["row_key"]: row for row in report_rows(store, sn)}
        validated, seen = [], set()
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"row_key", "source_token", "fields"}:
                raise ValueError("结果修改字段无效")
            key = edit["row_key"]
            if not isinstance(key, str) or key not in current or key in seen:
                raise ValueError("测试行编号未知或重复")
            seen.add(key)
            if edit["source_token"] != current[key]["source_token"]:
                raise ValueError("本项证据已变化，请重新打开结果后修改")
            fields = edit["fields"]
            if not isinstance(fields, dict) or fields.keys() - EDITABLE_FIELDS:
                raise ValueError("仅允许修改结果内容，不能改SN、来源或项目编号")
            if any(not isinstance(value, str) for value in fields.values()):
                raise ValueError("结果内容必须为文字")
            if "verdict" in fields and fields["verdict"] not in VERDICTS:
                raise ValueError("是否通过只能为未测试、通过、不通过、待复核、中断或不适用")
            if (key == "SYS-011" and current[key]["model"] == "28轴"
                    and fields.get("verdict", "不适用") != "不适用"):
                raise ValueError("28轴UFO特技不适用，不能填写其他结论")
            # A partial save preserves the current final values; no edit history is kept.
            merged = {field: current[key][field] for field in EDITABLE_FIELDS}
            merged.update(fields)
            validated.append((sn, key, edit["source_token"], _json(merged)))
        with store.db:
            store.db.execute("INSERT INTO report_settings VALUES('station_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (station_id.strip(),))
            store.db.executemany("INSERT INTO report_edits VALUES(?,?,?,?) ON CONFLICT(sn,row_key) DO UPDATE SET source_token=excluded.source_token,fields=excluded.fields", validated)
        return report_rows(store, sn)


def _csv_text(value):
    text = str(value)
    # Spreadsheet programs can recognize formulas after leading whitespace/control characters.
    probe = text.lstrip(" \t\r\n\v\f\ufeff")
    return "'" + text if probe.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def export_result_summary(store, sn, output_dir):
    from .factory_store import _sync_directory

    sn = store.validate_sn(sn)
    rows = report_rows(store, sn)
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    # SN is never interpreted as a pathname; every export preserves earlier files.
    path = output / ("SYS-summary-" + hashlib.sha256(sn.encode()).hexdigest()[:12] + "-" + uuid4().hex + ".csv")
    columns = _rules()["columns"]
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix=".summary-", dir=output) as temporary:
            staging = Path(temporary) / "summary.csv"
            with staging.open("x", encoding="utf-8-sig", newline="") as stream:
                os.chmod(staging, 0o600)
                writer = csv.writer(stream)
                writer.writerow([column["title"] for column in columns])
                for row in rows:
                    values = dict(row)
                    if row["source_kind"] == "simulated":
                        marker = "数据来源：simulated（模拟数据，不代表实机验收）"
                        values["notes"] = "\n".join(filter(None, (row["notes"], marker)))
                    writer.writerow([_csv_text(values[column["field"]]) for column in columns])
                stream.flush()
                os.fsync(stream.fileno())
            # link publishes a complete file atomically and refuses to overwrite any earlier export.
            os.link(staging, path)
            published = True
            _sync_directory(output)
    except BaseException:
        if published:
            path.unlink(missing_ok=True)
        raise
    return str(path)
