"""Dedicated PC-side SSH control, with heartbeat and an awaited cleanup receipt.

No robot connection is made on import. The desktop's explicit actions are the
only callers; the telemetry relay and central web server never invoke this API.
"""

import base64
import json
import lzma
import math
import queue
import re
import shlex
import struct
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

from .manual_calibration_check import validate_report as validate_manual_report
from .postzero_reader import CONFIGURATIONS, QUERY_TIMEOUT_NS
from .ssh_transport import parse_ssh_target

PASSWORD_PROMPT = b"RP1_MOTOR_CONTROL_SUDO_PASSWORD:"
# Reports retain attempt history (engine bound 8MiB) plus receive evidence.
MAX_LINE_BYTES = 9 * 1024 * 1024
MAX_COMMAND_BYTES = 8 * 1024 * 1024
STOP_TIMEOUT_S = 8.0


class MotorControlError(ValueError):
    pass


def helper_source():
    root = Path(__file__).parent
    sources = []
    for name in ("calibration_runtime", "postzero_reader", "gentle_motion", "torque_estimation",
                 "motor_trajectory", "calibration_mapping", "preparation_gains", "preparation_control", "calibration_check",
                 "manual_check_plan", "manual_calibration_check"):
        source = (root / (name + ".py")).read_text(encoding="utf-8")
        sources.append(
            f"module = types.ModuleType({name!r})\n"
            f"sys.modules[{name!r}] = module\n"
            f"exec(compile({source!r}, '<rp1-{name}>', 'exec'), module.__dict__)\n"
        )
    runtime = (root / "motor_control_runtime.py").read_text(encoding="utf-8")
    return ("import sys, types\n" + "".join(sources)
            + "namespace = {'__name__': 'rp1_motor_control_runtime', '__package__': ''}\n"
            + f"exec(compile({runtime!r}, '<rp1-motor-control>', 'exec'), namespace)\n"
            + "raise SystemExit(namespace['main']())\n")


def build_remote_command(dof, session_id):
    if type(dof) is not int or dof not in (24, 28):
        raise ValueError("电机控制只支持24或28电机")
    if not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
        raise ValueError("电机控制会话ID无效")
    # Keep the single Python -c argument below Linux's per-argument size limit.
    # Only our local source is compressed; this does not load an external archive
    # or create a file on the robot. It expands solely in the helper's memory.
    source = helper_source().encode("utf-8")
    if len(source) > 512 * 1024:
        raise MotorControlError("上位机电机控制源码超过512KiB，尚未连接机器人")
    packed = base64.b64encode(lzma.compress(source)).decode("ascii")
    bootstrap = ("import base64,lzma\n"
                 f"exec(compile(lzma.decompress(base64.b64decode({packed!r})), "
                 "'<rp1-control-memory>', 'exec'))\n")
    if len(bootstrap.encode("utf-8")) > 96 * 1024:
        raise MotorControlError("上位机电机控制启动参数过大，尚未连接机器人")
    return shlex.join([
        "/usr/bin/sudo", "-k", "-S", "-p", PASSWORD_PROMPT.decode("ascii"), "--",
        "/usr/bin/taskset", "-c", "1-3,7", "/usr/bin/nice", "-n", "10",
        "/usr/bin/python3", "-B", "-u", "-c", bootstrap,
        "--dof", str(dof), "--session-id", session_id,
    ])


def _numbers(values, dof):
    return isinstance(values, list) and len(values) == dof and all(
        type(value) in (int, float) and math.isfinite(value) for value in values)


def _encoder_identity(value, dof):
    index, request_id = value.get("index"), value.get("request_id")
    try:
        if type(index) is int and 0 <= index < dof and isinstance(request_id, str) and str(UUID(request_id)) == request_id:
            return index, request_id
    except ValueError:
        pass
    raise MotorControlError("编码器查询目标或请求编号无效")


def _joint_encoder_identity(value):
    joint, request_id = value.get("joint"), value.get("request_id")
    try:
        if (isinstance(joint, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}_joint", joint)
                and isinstance(request_id, str) and str(UUID(request_id)) == request_id):
            return joint, request_id
    except ValueError:
        pass
    raise MotorControlError("关节位置查询身份无效")


def _validate_encoder_position(event, dof):
    _encoder_identity(event, dof)
    motor_id, status = event.get("motor_id"), event.get("status")
    if (event.get("interface") not in ("can0", "can1", "can2", "can3")
            or type(motor_id) is not int or not 1 <= motor_id <= 8
            or status not in ("fresh", "timeout", "invalid", "error")
            or type(event.get("kernel_rx_timestamp")) is not bool):
        raise MotorControlError("编码器反馈身份或状态无效")
    sent, rx, received_at, angle, raw_hex, fault = (
        event.get(key) for key in ("query_sent_monotonic_ns", "rx_monotonic_ns",
                                   "received_at", "angle_deg", "raw_hex", "fault")
    )
    stamped = event["kernel_rx_timestamp"]
    try:
        if status == "error":
            if sent is not None:
                raise ValueError
        elif type(sent) is not int or not 0 < sent < 2**63:
            raise ValueError
        if status in ("timeout", "error"):
            if stamped or any(value is not None for value in (rx, received_at, angle, raw_hex, fault)):
                raise ValueError
            return
        if not isinstance(raw_hex, str) or not re.fullmatch(r"(?:[0-9a-f]{2}){0,72}", raw_hex):
            raise ValueError
        if fault is not None and (type(fault) is not int or not 0 <= fault <= 31):
            raise ValueError
        if stamped:
            if (type(rx) is not int or not sent <= rx < 2**63 or rx - sent > QUERY_TIMEOUT_NS
                    or not isinstance(received_at, str) or len(received_at) > 36):
                raise ValueError
            timestamp = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
                raise ValueError
        elif rx is not None or received_at is not None:
            raise ValueError
        if status == "fresh":
            raw = bytes.fromhex(raw_hex)
            if (not stamped or fault != 0 or type(angle) not in (float, int) or not math.isfinite(angle)
                    or len(raw) != 6 or raw[:2] != b"\xa0\x01" or struct.unpack(">f", raw[2:])[0] != angle):
                raise ValueError
        elif angle is not None:
            raise ValueError
    except (ValueError, OverflowError):
        raise MotorControlError("编码器反馈与原始帧、接收时间或质量状态不一致") from None


def _validate_joint_encoder_positions(event, dof):
    _, request_id = _joint_encoder_identity(event)
    samples, position, error = event.get("samples"), event.get("position_rad"), event.get("error")
    if not isinstance(samples, list) or len(samples) > 2:
        raise MotorControlError("关节位置反馈的轴角数量无效")
    indices = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise MotorControlError("关节位置反馈包含无效轴角")
        _validate_encoder_position({**sample, "request_id": request_id}, dof)
        indices.add(sample["index"])
    if len(indices) != len(samples):
        raise MotorControlError("关节位置反馈包含重复电机")
    if position is None:
        if not isinstance(error, str) or not 0 < len(error) <= 240:
            raise MotorControlError("关节位置反馈缺少无效原因")
        return
    if (error is not None or type(position) not in (int, float) or not -math.pi <= position <= math.pi
            or not samples or any(sample["status"] != "fresh" for sample in samples)):
        raise MotorControlError("关节位置缺少有效电机反馈")
    stamps = [sample["rx_monotonic_ns"] for sample in samples]
    if max(stamps) - min(stamps) > 100_000_000:
        raise MotorControlError("闭链电机位置反馈时间差过大")


def validate_event(raw, session_id, dof):
    if len(raw) > MAX_LINE_BYTES:
        raise MotorControlError("控制反馈单行超过9MiB")
    try:
        event = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise MotorControlError("控制反馈不是有效JSON") from None
    if (not isinstance(event, dict) or event.get("session_id") != session_id
            or event.get("source_kind") != "hardware"):
        raise MotorControlError("控制反馈的会话或来源不匹配")
    kind = event.get("type")
    if kind == "metadata":
        motors = event.get("motors")
        if (event.get("dof") != dof or event.get("position_basis") != "motor_output_sdk_rad"
                or event.get("configuration_sha256") != CONFIGURATIONS[dof][1]
                or not isinstance(motors, list) or len(motors) != dof):
            raise MotorControlError("控制端机型或配置不匹配")
        identities = set()
        for index, motor in enumerate(motors):
            if (not isinstance(motor, dict) or motor.get("index") != index
                    or motor.get("interface") not in {"can0", "can1", "can2", "can3"}
                    or type(motor.get("motor_id")) is not int or not 1 <= motor["motor_id"] <= 8
                    or type(motor.get("model")) is not int or not 0 <= motor["model"] <= 3
                    or type(motor.get("software_offset_rad")) not in (int, float)
                    or not math.isfinite(motor["software_offset_rad"])):
                raise MotorControlError("控制端电机映射无效")
            identity = motor["interface"], motor["motor_id"]
            if identity in identities:
                raise MotorControlError("控制端电机映射重复")
            identities.add(identity)
    elif kind == "feedback":
        if (not _numbers(event.get("positions_rad"), dof)
                or not _numbers(event.get("torques_nm"), dof)
                or type(event.get("enabled")) is not bool
                or type(event.get("progress")) not in (int, float)
                or not math.isfinite(event["progress"]) or not 0 <= event["progress"] <= 1):
            raise MotorControlError("实时电机反馈缺失或数值无效")
    elif kind == "encoder_position":
        _validate_encoder_position(event, dof)
    elif kind == "joint_encoder_positions":
        _validate_joint_encoder_positions(event, dof)
    elif kind == "state":
        if event.get("state") not in {"ready", "enabled", "preparing", "prepared", "running", "disabled", "stopping", "fault"}:
            raise MotorControlError("未知电机控制状态")
    elif kind == "manual_state":
        try:
            if str(UUID(event.get("run_id"))) != event.get("run_id"):
                raise ValueError("run_id")
            validate_manual_report(event.get("report"), dof)
            if (type(event.get("damping_kd")) not in (int, float)
                    or not math.isfinite(event["damping_kd"]) or not 0 <= event["damping_kd"] <= 0.5
                    or event.get("torque_basis") != "estimated_from_motor_iq"
                    or event.get("timestamp_basis") != "kernel_so_timestampns"
                    or type(event.get("sampling_hz")) is not int or event["sampling_hz"] != 50):
                raise ValueError("source")
            evidence = event.get("feedback_evidence", [])
            if not isinstance(evidence, list) or len(evidence) > dof:
                raise ValueError("evidence")
            seen = set()
            for item in evidence:
                if (not isinstance(item, dict) or type(item.get("index")) is not int
                        or not 0 <= item["index"] < dof or item["index"] in seen
                        or type(item.get("kernel_received_ns")) is not int or item["kernel_received_ns"] <= 0
                        or not isinstance(item.get("raw_hex"), str) or len(item["raw_hex"]) not in (16, 24)
                        or any(char not in "0123456789abcdef" for char in item["raw_hex"])
                        or int(item["raw_hex"][:2], 16) >> 5 != 1):
                    raise ValueError("evidence")
                seen.add(item["index"])
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
            raise MotorControlError("人工标定检查结果的来源、参数或逐关节证据无效") from None
    elif kind == "complete":
        if type(event.get("disable_sent")) is not bool or event.get("hardware_stop_verified") is not False:
            raise MotorControlError("控制结束记录无效，不能推断物理停稳")
    elif kind == "enable_diagnostics":
        motors = event.get("motors")
        if (event.get("expected_count") != dof or type(event.get("received_count")) is not int
                or not 0 <= event["received_count"] <= dof
                or not isinstance(motors, list) or len(motors) != dof
                or not isinstance(event.get("rejected"), dict)
                or any(not isinstance(key, str) or type(value) is not int or value < 0
                       for key, value in event["rejected"].items())):
            raise MotorControlError("使能反馈诊断无效")
        identities = set()
        for index, motor in enumerate(motors):
            if (not isinstance(motor, dict) or type(motor.get("index")) is not int or motor["index"] != index
                    or motor.get("interface") not in {"can0", "can1", "can2", "can3"}
                    or type(motor.get("motor_id")) is not int or not 1 <= motor["motor_id"] <= 8
                    or motor.get("status") not in {"fresh", "missing", "stale", "disabled", "fault"}
                    or motor.get("age_ms") is not None and (type(motor["age_ms"]) not in (int, float)
                                                           or not math.isfinite(motor["age_ms"]))
                    or motor.get("enabled") is not None and type(motor["enabled"]) is not bool
                    or motor.get("fault") is not None and (type(motor["fault"]) is not int
                                                           or not 0 <= motor["fault"] <= 7)):
                raise MotorControlError("逐电机使能反馈诊断无效")
            identity = motor["interface"], motor["motor_id"]
            if identity in identities:
                raise MotorControlError("使能反馈诊断电机重复")
            identities.add(identity)
            if "raw_byte0" in motor or "raw_hex" in motor:
                byte0, raw_hex = motor.get("raw_byte0"), motor.get("raw_hex")
                if (type(byte0) is not int or not 0 <= byte0 <= 255
                        or not isinstance(raw_hex, str) or len(raw_hex) not in (16, 24)
                        or any(char not in "0123456789abcdef" for char in raw_hex)
                        or int(raw_hex[:2], 16) != byte0 or byte0 >> 5 != 1
                        or motor.get("enabled") is not bool(byte0 & 0x10)
                        or motor.get("fault") != byte0 & 0x07):
                    raise MotorControlError("逐电机原始使能反馈诊断无效")
            if "enabled_seen" in motor and type(motor["enabled_seen"]) is not bool:
                raise MotorControlError("逐电机使能状态历史无效")
            for key, suffix in (("enable_ack", "0106"), ("setting_failure_ack", "0100")):
                ack = motor.get(key)
                if ack is not None and (
                        not isinstance(ack, dict)
                        or ack.get("raw_hex") != f"{motor['motor_id']:04x}" + suffix
                        or type(ack.get("age_ms")) not in (int, float)
                        or not math.isfinite(ack["age_ms"]) or ack["age_ms"] < 0):
                    raise MotorControlError("逐电机使能应答诊断无效")
        for key, expected in (
            ("fresh_count", sum(row["status"] not in {"missing", "stale"} for row in motors)),
            ("enabled_count", sum(row["status"] == "fresh" for row in motors)),
        ):
            if key in event and (type(event[key]) is not int or event[key] != expected):
                raise MotorControlError("使能反馈诊断计数与逐电机状态不一致")
    elif kind == "command_rejected":
        rows, tolerance = event.get("rows"), event.get("tolerance_deg")
        if ((event.get("command"), event.get("code")) not in {
                ("check", "start_pose_mismatch"), ("prepare_check", "preparation_start_changed")}
                or event.get("state") not in {"enabled", "prepared"}
                or type(tolerance) not in (int, float) or not math.isfinite(tolerance)
                or not 0 <= tolerance <= 15 or not isinstance(rows, list) or len(rows) != dof):
            raise MotorControlError("检查起始姿态拒绝记录无效")
        if event["command"] == "prepare_check" and tolerance != 0.5:
            raise MotorControlError("复位确认起点变化门槛无效")
        names = set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("joint"), str)
                    or not row["joint"] or len(row["joint"]) > 128 or row["joint"] in names
                    or any(type(row.get(key)) not in (int, float) or not math.isfinite(row[key])
                           for key in ("actual_rad", "target_rad", "deviation_deg"))
                    or type(row.get("within_tolerance")) is not bool):
                raise MotorControlError("逐关节起始姿态记录无效")
            delta = row["actual_rad"] - row["target_rad"]
            if (abs(row["deviation_deg"] - math.degrees(delta)) > 1e-7
                    or row["within_tolerance"] != (abs(delta) <= math.radians(tolerance) + 1e-12)):
                raise MotorControlError("起始姿态角度与偏差不一致")
            names.add(row["joint"])
        if all(row["within_tolerance"] for row in rows):
            raise MotorControlError("起始姿态拒绝记录缺少未就位关节")
    elif kind == "prepare_state":
        if (event.get("phase") not in {"gain_ramp", "preparing", "confirm_stop", "prepared"}
                or type(event.get("progress")) not in (int, float)
                or not math.isfinite(event["progress"]) or not 0 <= event["progress"] <= 1):
            raise MotorControlError("检查起始姿态准备状态无效")
    elif kind == "check_state":
        if (not isinstance(event.get("phase"), str) or len(event["phase"]) > 64
                or not isinstance(event.get("group"), str) or len(event["group"]) > 128
                or type(event.get("waiting_review")) is not bool
                or event["waiting_review"] != (event["phase"] == "waiting_review")
                or type(event.get("progress")) not in (int, float)
                or not math.isfinite(event["progress"]) or not 0 <= event["progress"] <= 1):
            raise MotorControlError("标定检查状态无效")
    elif kind == "check_sample":
        if (any(not _numbers(event.get(key), dof) for key in
                ("positions_rad", "torques_nm", "targets_rad"))
                or type(event.get("time_s")) not in (int, float)
                or not math.isfinite(event["time_s"]) or not 0 <= event["time_s"] <= 7200
                or not isinstance(event.get("phase"), str) or len(event["phase"]) > 64
                or not isinstance(event.get("group"), str) or len(event["group"]) > 128):
            raise MotorControlError("标定检查采样缺失或无效")
    elif kind == "check_result":
        result = event.get("result")
        if (not isinstance(result, dict) or result.get("source_kind") != "hardware"
                or result.get("candidate_only") is not True
                or result.get("physical_stop_confirmed") is not False
                or result.get("calibration_passed") is not None
                or not isinstance(result.get("outcomes"), list)
                or len(result["outcomes"]) > dof):
            raise MotorControlError("标定检查结果无效，不能把候选写成实机验收通过")
        names = set()
        for row in result["outcomes"]:
            if (not isinstance(row, dict) or not isinstance(row.get("joint"), str)
                    or not row["joint"] or row["joint"] in names
                    or row.get("candidate_only") is not True
                    or row.get("physical_stop_confirmed") is not False
                    or row.get("calibration_passed") is not None
                    or row.get("status") not in {"candidate_obstruction", "unexpected_obstruction",
                                                  "not_covered", "endpoint_not_detected"}
                    or type(row.get("expected_rad")) not in (int, float)
                    or not math.isfinite(row["expected_rad"])):
                raise MotorControlError("标定检查逐关节结果无效")
            names.add(row["joint"])
            measured, deviation, within = (row.get(k) for k in
                                            ("measured_rad", "deviation_deg", "within_tolerance"))
            if measured is None:
                if deviation is not None or within is not None:
                    raise MotorControlError("无实测端点不得生成偏差或通过结论")
            elif (type(measured) not in (int, float) or not math.isfinite(measured)
                  or type(deviation) not in (int, float) or not math.isfinite(deviation)
                  or type(within) is not bool
                  or abs(deviation - math.degrees(measured - row["expected_rad"])) > 1e-7):
                raise MotorControlError("标定检查角度与偏差不一致")
    elif kind != "error":
        raise MotorControlError("未知控制反馈类型")
    if "message" in event and (not isinstance(event["message"], str) or len(event["message"]) > 500):
        raise MotorControlError("控制端诊断消息无效")
    return event


def run_motor_session(connection, password, dof, session_id, on_event, commands, cancel_event):
    if not isinstance(connection, dict) or connection.get("connected") is not True:
        raise ValueError("需要当前机器人已核对的SSH绑定")
    username, host = parse_ssh_target(connection.get("target", ""))
    if username != "robo":
        raise ValueError("当前控制配置只支持robo用户的既有deploy")
    port = connection.get("ssh_port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SSH端口无效")
    known = Path(connection.get("known_hosts", "")).expanduser()
    if not known.is_file():
        raise ValueError("请先核对并保存目标主机指纹")
    if (not isinstance(password, str) or not password or len(password) > 4096
            or any(char in password for char in "\r\n\0")):
        raise ValueError("请输入本次SSH和sudo密码")
    command = build_remote_command(dof, session_id)
    if cancel_event.is_set():
        return {"cancelled": True, "complete": False}
    import paramiko

    client, channel, stdin = None, None, None
    result = None
    try:
        client = paramiko.SSHClient()
        client.load_host_keys(str(known))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(hostname=host, port=port, username=username, password=password,
                       look_for_keys=False, allow_agent=False, timeout=5, banner_timeout=5, auth_timeout=8)
        if cancel_event.is_set():
            return {"cancelled": True, "complete": False}
        stdin, stdout, _ = client.exec_command(command, get_pty=False, timeout=5)
        channel = stdout.channel
        buffer, prompt_tail = bytearray(), b""
        authenticated = False
        metadata = pending_encoder = None
        encoder_observation_ready = False
        heartbeat_at, stopped_at = 0.0, None
        started = time.monotonic()
        stderr_size = 0

        def send(value):
            data = json.dumps({**value, "session_id": session_id}, allow_nan=False,
                              ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
            if len(data) > MAX_COMMAND_BYTES:
                raise MotorControlError("控制命令超过8MiB限制")
            stdin.write(data.decode("ascii"))
            stdin.flush()

        while True:
            now = time.monotonic()
            if authenticated and now - heartbeat_at >= 0.25 and stopped_at is None:
                send({"command": "heartbeat"})
                heartbeat_at = now
            if cancel_event.is_set() and stopped_at is None and authenticated:
                send({"command": "stop"})
                stopped_at = now
            if stopped_at is not None and now - stopped_at > STOP_TIMEOUT_S:
                raise MotorControlError("停止等待超过8秒，未收到完整失能发送记录；请现场核对")
            if not metadata and now - started > 60:
                raise MotorControlError("控制配置预检超过60秒，已结束连接")
            if now - started > 3610:
                raise MotorControlError("电机控制会话超过1小时")
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(8192)
                stderr_size += len(data)
                if stderr_size > 128 * 1024:
                    raise MotorControlError("远端控制诊断输出过多")
                window = prompt_tail + data
                prompts = window.count(PASSWORD_PROMPT)
                if prompts:
                    if authenticated or prompts != 1:
                        raise MotorControlError("sudo认证失败；不会重试密码")
                    stdin.write(password + "\n")
                    stdin.flush()
                    authenticated = True
                    password = ""
                prompt_tail = window[-(len(PASSWORD_PROMPT) - 1):]
            if channel.recv_ready():
                buffer.extend(channel.recv(65536))
                while b"\n" in buffer:
                    raw, _, remaining = buffer.partition(b"\n")
                    buffer = bytearray(remaining)
                    event = validate_event(bytes(raw), session_id, dof)
                    if event["type"] == "metadata":
                        if metadata:
                            raise MotorControlError("控制端重复发送了配置身份")
                        metadata = event["motors"]
                        encoder_observation_ready = event.get("encoder_observation_ready") is True
                        # NOPASSWD sudo launches without requesting a password;
                        # helper metadata still proves stdin is ready for control.
                        authenticated = True
                        password = ""
                    elif event["type"] not in {"error", "complete"} and not metadata:
                        raise MotorControlError("控制端未确认机型就返回了执行状态")
                    if event["type"] == "encoder_position":
                        index = event["index"]
                        if (pending_encoder != ("read_encoder", index, event["request_id"])
                                or any(event[key] != metadata[index][key] for key in ("interface", "motor_id"))):
                            raise MotorControlError("编码器反馈与本次查询或已核对的电机地址不匹配")
                        pending_encoder = None
                    elif event["type"] == "joint_encoder_positions":
                        if (pending_encoder != ("read_joint_encoders", event["joint"], event["request_id"])
                                or any(sample[key] != metadata[sample["index"]][key]
                                       for sample in event["samples"] for key in ("interface", "motor_id"))):
                            raise MotorControlError("关节位置反馈与查询或已核对的电机地址不匹配")
                        pending_encoder = None
                    on_event(event)
                    if event["type"] == "complete":
                        result = event
                if len(buffer) > MAX_LINE_BYTES:
                    raise MotorControlError("控制反馈单行超过9MiB")
            if result:
                return result
            if metadata and not cancel_event.is_set() and stopped_at is None:
                # Never create an enable/run automatically or replay commands.
                try:
                    value = commands.get_nowait()
                except queue.Empty:
                    value = None
                if value:
                    if value.get("command") not in {"enable", "disable", "run", "stop",
                                                     "prepare_check", "check", "continue_check", "manual_check",
                                                     "manual_pause", "manual_retest", "manual_skip", "manual_confirm_pin",
                                                     "read_encoder", "read_joint_encoders"}:
                        raise MotorControlError("桌面控制命令无效")
                    if value["command"] == "read_encoder":
                        if pending_encoder is not None or set(value) != {"command", "index", "request_id"}:
                            raise MotorControlError("编码器查询参数无效或已有未完成的查询")
                        pending_encoder = ("read_encoder", *_encoder_identity(value, dof))
                    elif value["command"] == "read_joint_encoders":
                        if (not encoder_observation_ready or pending_encoder is not None
                                or set(value) != {"command", "joint", "request_id"}):
                            raise MotorControlError("关节位置查询不可用或已有未完成的查询")
                        pending_encoder = ("read_joint_encoders", *_joint_encoder_identity(value))
                    send(value)
                    if value["command"] == "stop":
                        stopped_at = now
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                raise MotorControlError("控制通道结束但没有完整停止记录，请现场核对")
            time.sleep(0.01)
    except MotorControlError:
        raise
    except Exception:
        # Do not echo a remote command, stderr, password or authentication object.
        raise MotorControlError("控制SSH连接失败或中断，请现场确认电机状态") from None
    finally:
        password = ""
        # EOF independently trips the remote deadman; closing a channel itself
        # is never reported as proof of physical stop or motor disable.
        if channel:
            if result is None:
                try:
                    channel.shutdown_write()
                except Exception:
                    pass
            channel.close()
        if client:
            client.close()
