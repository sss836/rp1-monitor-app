"""Explicit motor control owner, embedded in RAM over SSH; never runs on import.

Uses the existing deploy motors_py SDK, not ROS control services. The monitor
collector remains read-only. Remote cleanup sends disable, which is distinct
from independently verifying that a supported robot has physically stopped.
"""

import argparse
import errno
import fcntl
import hashlib
import importlib
import json
import math
import os
import queue
import select
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

try:
    from . import calibration_check as check_logic
    from . import calibration_mapping as joint_mapping
    from . import calibration_runtime as verifier
    from . import manual_calibration_check as manual_logic
    from . import manual_check_plan
    from . import motor_trajectory as trajectory
    from . import postzero_reader as reader
    from .gentle_motion import SmoothMove
    from .preparation_control import PreparationController, interpolate_gains
    from .preparation_gains import preparation_gain_profile
    from .torque_estimation import estimate_output_torque
except ImportError:  # Explicit modules supplied by the SSH bootstrap.
    import calibration_check as check_logic
    import calibration_mapping as joint_mapping
    import calibration_runtime as verifier
    import manual_calibration_check as manual_logic
    import manual_check_plan
    import motor_trajectory as trajectory
    import postzero_reader as reader
    from gentle_motion import SmoothMove
    from preparation_control import PreparationController, interpolate_gains
    from preparation_gains import preparation_gain_profile
    from torque_estimation import estimate_output_torque

CONTROL_HZ = 50
ENCODER_PAIR_MAX_SKEW_NS = 100_000_000
SDK_INIT_STEP_S = 0.005  # Verified LRO init_motor normal_sleep_time.
HEARTBEAT_TIMEOUT_S = 2.0
FEEDBACK_TIMEOUT_S = 0.25
SESSION_LIMIT_S = 3600
LOCK_PATH = "/run/lock/rp1-monitor-motor-control.lock"
MAX_COMMAND_BYTES = 8 * 1024 * 1024
DRIVER_SOURCE_SHA256 = {
    "src/motors/src/drivers/lro/lro_motor_driver.cpp":
        "0d593b397b1071dde3642aaab6c79a3e8883206a47e8cc1a02a195ef7a1f3885",
    "src/motors/src/drivers/lro/lro_motor_driver.hpp":
        "ba9c9192ba95737f48c546c7c799cdaae6f2851f6d438b43f20d320a61513d08",
    "src/motors/src/protocol/canfd/socket_canfd.cpp":
        "e1c07b8a059895d2094697a132a1c7bcb7294a6e908b8ad56db9ef56a540f404",
    "src/motors/src/pybind_module.cpp":
        "e3a30eec211356487c5f9eb4779410922bd8402f6dc3234d3d24fb7e7e7be28c",
}
VIDEO_PLAN_SHA256 = {
    24: "cec6bde233713ed0fa3dc4eace3c141a77e1020a209a7016d2b4eef5b132fe9d",
    28: "c182bfdd67a0bedc0e167acc3361cf01be27e40efdb2eb55d60263a9dcadb0c6",
}


def validate_driver_sources(root):
    root = Path(root).resolve(strict=True)
    for relative, digest in DRIVER_SOURCE_SHA256.items():
        source = (root / relative).resolve(strict=True)
        if not source.is_relative_to(root) or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError("LRO协议/SDK与本控制入口核对版本不符，请重新核对：" + relative)


class StopRequested(Exception):
    pass


class StartPoseRejected(ValueError):
    """A check request rejected before any new motion or engine is installed."""

    def __init__(self, rows, tolerance_deg, *, command="check"):
        self.rows, self.tolerance_deg = rows, tolerance_deg
        self.command = command
        self.code = "start_pose_mismatch" if command == "check" else "preparation_start_changed"
        different = [row for row in rows if not row["within_tolerance"]]
        details = "；".join(
            f"{row['joint']} 当前{math.degrees(row['actual_rad']):+.1f}° / 起始{math.degrees(row['target_rad']):+.1f}°"
            for row in different[:4])
        if command == "prepare_check":
            super().__init__("确认后起始姿态发生变化，尚未开始复位；请按最新角度重新确认路线：" + details)
        else:
            super().__init__(f"检查起始姿态尚未就位（{len(different)}个关节），未开始运动：" + details
                             + "。这是姿态准备差值，不是标零误差；请查看完整起始姿态表。")


class FeedbackUnavailable(ValueError):
    """A checked receive failure, distinct from an SDK or control exception."""

    def __init__(self, message, kind):
        super().__init__(message)
        self.kind = kind


class InputPump:
    """Receive stop independently of SDK initialization and trajectory decoding."""

    def __init__(self, stream, session_id):
        self.stream, self.session_id = stream, session_id
        self.commands = queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.disable_requested = threading.Event()
        self.last_heartbeat = time.monotonic()
        self.error = None

    def start(self):
        threading.Thread(target=self._read, daemon=True, name="rp1-control-input").start()

    def _read(self):
        try:
            while not self.stop.is_set():
                line = self.stream.readline(MAX_COMMAND_BYTES + 1)
                if not line:
                    self.stop.set()
                    return
                if len(line) > MAX_COMMAND_BYTES or not line.endswith(b"\n"):
                    raise ValueError("控制命令超过8MiB或未完整结束")
                command = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("控制命令包含非有限数值")))
                if not isinstance(command, dict) or command.get("session_id") != self.session_id:
                    raise ValueError("控制命令会话不匹配")
                name = command.get("command")
                if name == "heartbeat":
                    self.last_heartbeat = time.monotonic()
                elif name == "stop":
                    self.stop.set()
                elif name == "disable":
                    self.disable_requested.set()
                elif name in {"enable", "run", "prepare_check", "check", "continue_check",
                              "manual_check", "manual_pause", "manual_retest", "manual_skip", "manual_confirm_pin",
                              "read_encoder", "read_joint_encoders"}:
                    self.commands.put_nowait(command)
                else:
                    raise ValueError("不支持的电机控制命令")
        except Exception:
            self.error = "控制输入无效或队列已满，停止本次会话"
            self.stop.set()

    def check(self):
        if self.stop.is_set():
            raise StopRequested(self.error or "收到停止请求或SSH输入已关闭")
        if time.monotonic() - self.last_heartbeat > HEARTBEAT_TIMEOUT_S:
            raise StopRequested("上位机心跳中断")


class EncoderQueryControl:
    """Use the control heartbeat and stop signals during a bounded position query."""

    def __init__(self, control):
        self.control = control

    def check(self):
        self.control.check()
        if self.control.disable_requested.is_set():
            raise ValueError("收到失能请求，位置读取已取消")

    def wait(self, readers, timeout):
        self.check()
        ready = select.select(readers, [], [], max(0, min(timeout, 0.05)))[0]
        self.check()
        return ready


def acquire_lock(path=LOCK_PATH):
    # Do not follow a pre-existing symlink when the helper runs under sudo.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise ValueError("另一个上位机电机控制会话正在使用机器人") from None
    return descriptor


def check_other_writers(check_cancel=None):
    if check_cancel is None:
        reader.check_processes()
    else:
        reader.check_processes(check_cancel=check_cancel)
    for entry in Path("/proc").iterdir():
        if check_cancel is not None:
            check_cancel()
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            # /proc may list a PID that exits before cmdline is opened/read.
            continue
        except OSError as error:
            code = errno.errorcode.get(error.errno, "UNKNOWN")
            raise ValueError(f"无法核对其他电机工具：PID {entry.name}，读取cmdline失败（{code}，errno={error.errno}）") from error
        names = {Path(os.fsdecode(arg)).name for arg in args if arg}
        if names & {"cansend", "cangen", "motor_ota.py", "motors_py_example.py"} or any(
            name.startswith("factory_hmi") for name in names
        ):
            raise ValueError("其他电机写入工具仍在运行：PID " + entry.name)


class CanFeedback:
    """Per-motor kernel receive times, separate from the SDK's zero-initialized cache."""

    def __init__(self, motors):
        self.motors = motors
        self.connections = {}
        self.values = [None] * len(motors)
        self.enable_since_ns = [None] * len(motors)
        self.enabled_seen = [False] * len(motors)
        self.enable_acks = [None] * len(motors)
        self.setting_failure_acks = [None] * len(motors)
        self.rejected = {key: 0 for key in (
            "format", "identity", "truncated_or_own_echo", "local_origin",
            "not_type1", "missing_timestamp", "stale_timestamp", "duplicate_timestamp", "before_enable")}
        self.received_frames = 0
        self.addresses = {(m["interface"], m["motor_id"]): m["index"] for m in motors}
        try:
            for name in dict.fromkeys(m["interface"] for m in motors):
                self.connections[name] = reader.open_can(name, [0x7FF] + [
                    m["motor_id"] for m in motors if m["interface"] == name
                ])
        except BaseException:
            self.close()
            raise

    def note_enable_sent(self, index):
        # SDK sends asynchronously. This is an enqueue boundary, not proof of
        # CAN delivery; reject older queued evidence but do not infer an ACK.
        self.enable_since_ns[index] = time.time_ns()
        self.values[index] = None
        self.enabled_seen[index] = False
        self.enable_acks[index] = self.setting_failure_acks[index] = None

    def poll(self):
        for name, connection in self.connections.items():
            for _ in range(256):
                try:
                    packet, ancillary, flags, _ = connection.recvmsg(
                        reader.CANFD_MTU, socket.CMSG_SPACE(reader.TIMESPEC.size))
                except BlockingIOError:
                    break
                self.received_frames += 1
                if len(packet) not in (reader.CAN_MTU, reader.CANFD_MTU):
                    self.rejected["format"] += 1
                    continue
                can_id, length = struct.unpack_from("=IB", packet)
                is_ack = can_id == 0x7FF
                if length not in ((4,) if is_ack else (8, 12)) or len(packet) < length + 8:
                    self.rejected["format"] += 1
                    continue
                if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC | socket.MSG_CONFIRM):
                    self.rejected["truncated_or_own_echo"] += 1
                    continue
                if flags & socket.MSG_DONTROUTE:
                    self.rejected["local_origin"] += 1
                    continue
                payload = packet[8:8 + length]
                stamp = reader.kernel_timestamp(ancillary)
                index = self.addresses.get((name, int.from_bytes(payload[:2], "big") if is_ack else can_id))
                if index is None:
                    self.rejected["identity"] += 1
                    continue
                if not is_ack and payload[0] >> 5 != 1:
                    self.rejected["not_type1"] += 1
                    continue
                if stamp is None:
                    self.rejected["missing_timestamp"] += 1
                    continue
                age = (time.time_ns() - stamp) / 1e9
                if not 0 <= age <= FEEDBACK_TIMEOUT_S:
                    self.rejected["stale_timestamp"] += 1
                    continue
                since = self.enable_since_ns[index]
                if since is not None and stamp <= since:
                    self.rejected["before_enable"] += 1
                    continue
                received = time.monotonic() - age
                if is_ack:
                    # 00 is a generic settings failure, including unlock; it
                    # cannot uniquely identify an enable rejection. Neither ACK
                    # can replace a fresh, enabled, fault-free Type1 response.
                    target = (self.enable_acks if payload[2:] == b"\x01\x06" else
                              self.setting_failure_acks if payload[2:] == b"\x01\x00" else None)
                    if target is not None and since is not None:
                        previous = target[index]
                        if previous is None or received > previous["received"]:
                            target[index] = dict(received=received, raw_hex=payload.hex())
                    continue
                previous = self.values[index]
                if previous and (stamp <= previous["kernel_received_ns"] if "kernel_received_ns" in previous
                                 else received <= previous["received"]):
                    self.rejected["duplicate_timestamp"] += 1
                    continue
                motor = self.motors[index]
                raw_position = (payload[1] << 8) | payload[2]
                raw_iq = ((payload[4] & 15) << 8) | payload[5]
                current_range = (18.0, 32.0, 35.0, 90.0)[motor["model"]]
                iq = raw_iq / 4095.0 * 2 * current_range - current_range
                self.values[index] = {
                    "received": received, "kernel_received_ns": stamp, "enabled": bool(payload[0] & 16),
                    "error": payload[0] & 7,
                    "raw_byte0": payload[0], "raw_hex": payload.hex(),
                    "position": raw_position / 65535.0 * 25.0 - 12.5 + motor["software_offset_rad"],
                    "torque": estimate_output_torque(iq, motor["model"], source_kind="offline")["estimated_torque_nm"],
                }
                if since is not None and payload[0] & 16:
                    self.enabled_seen[index] = True

    def snapshot(self, *, require_enabled=True):
        self.poll()
        now = time.monotonic()
        # Fault frames must abort immediately, rather than be hidden by a
        # different motor's missing response and retried for another second.
        faults = [self._identity(i) + f"=0x{value['error']:x}" for i, value in enumerate(self.values)
                  if value is not None and 0 <= now - value["received"] <= FEEDBACK_TIMEOUT_S
                  and value["error"]]
        if faults:
            raise FeedbackUnavailable("电机上报故障：" + "、".join(faults), "fault")
        if any(value is None or now - value["received"] > FEEDBACK_TIMEOUT_S for value in self.values):
            raise FeedbackUnavailable("电机反馈缺失或超过250ms；" + self.diagnostics()["message"], "missing")
        if require_enabled and not all(value["enabled"] for value in self.values):
            raise FeedbackUnavailable("部分电机未确认使能状态（已收到反馈但使能位为0）；" + self.diagnostics()["message"], "disabled")
        return [v["position"] for v in self.values], [v["torque"] for v in self.values]

    def _identity(self, index):
        motor = self.motors[index]
        return motor["interface"] + "/" + str(motor["motor_id"])

    def diagnostics(self):
        """Report cached receive evidence only; never transmits or polls a motor."""
        now, rows = time.monotonic(), []
        groups = {key: [] for key in ("missing", "stale", "disabled", "fault")}
        for index, (motor, value) in enumerate(zip(self.motors, self.values)):
            age = None if value is None else (now - value["received"]) * 1000
            status = ("missing" if value is None else
                      "stale" if not 0 <= age <= FEEDBACK_TIMEOUT_S * 1000 else
                      "fault" if value["error"] else "disabled" if not value["enabled"] else "fresh")
            if status in groups:
                groups[status].append(self._identity(index) + (f"=0x{value['error']:x}" if status == "fault" else ""))
            rows.append(dict(index=index, interface=motor["interface"], motor_id=motor["motor_id"],
                             status=status, age_ms=age, enabled=None if value is None else value["enabled"],
                             fault=None if value is None else value["error"]))
            if value is not None and "raw_byte0" in value:
                rows[-1].update(raw_byte0=value["raw_byte0"], raw_hex=value["raw_hex"])
            rows[-1]["enabled_seen"] = self.enabled_seen[index]
            for key, evidence in (("enable_ack", self.enable_acks[index]),
                                  ("setting_failure_ack", self.setting_failure_acks[index])):
                rows[-1][key] = (None if evidence is None else dict(
                    raw_hex=evidence["raw_hex"], age_ms=(now - evidence["received"]) * 1000))
        received = sum(value is not None for value in self.values)
        fresh = sum(row["status"] not in {"missing", "stale"} for row in rows)
        enabled = sum(row["status"] == "fresh" for row in rows)
        labels = {"missing": "未收到", "stale": "反馈过期", "disabled": "已收到反馈但使能位为0", "fault": "故障"}
        message = f"已收到反馈 {received}/{len(rows)}；新鲜反馈 {fresh}/{len(rows)}；有效使能反馈 {enabled}/{len(rows)}"
        rejected_labels = {"format": "格式不符", "identity": "身份不符", "truncated_or_own_echo": "截断/自身回显",
                           "local_origin": "本地来源", "not_type1": "非Type1", "missing_timestamp": "无内核时间戳",
                           "stale_timestamp": "时间戳过期", "duplicate_timestamp": "重复时间戳",
                           "before_enable": "使能发送前的旧帧"}
        rejected = [rejected_labels[key] + str(count) for key, count in self.rejected.items() if count]
        if rejected:
            message += f"；本接收器读帧{self.received_frames}，拒绝" + "、".join(rejected)
        for kind, identities in groups.items():
            if identities:
                message += "；" + labels[kind] + " " + "、".join(identities)
        for row in rows:
            if row["status"] == "disabled" and self.enable_since_ns[row["index"]] is not None:
                message += (f"；{row['interface']}/{row['motor_id']}："
                            + ("收到使能ACK" if row["enable_ack"] else "未看到使能ACK")
                            + ("，曾收到使能位1后变0" if row["enabled_seen"] else "，尚未看到使能位1")
                            + ("，观察到通用设置失败ACK（归属未确认）" if row["setting_failure_ack"] else ""))
        return dict(motors=rows, received_count=received, fresh_count=fresh, enabled_count=enabled,
                    expected_count=len(rows), received_frames=self.received_frames,
                    rejected=dict(self.rejected), message=message[:500])

    def close(self):
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()


class MotorDriver:
    """Existing deploy SDK, with per-bus initialization and eight-slot MIT dispatch."""

    def __init__(self, motors):
        self.entries, self.motors, self.feedback = motors, [], None

    def enable(self, control, *, damping_kd=0.5):
        if type(damping_kd) not in (int, float) or not math.isfinite(damping_kd) or not 0 <= damping_kd <= 0.5:
            raise ValueError("手动阻尼Kd须在0至0.5之间")
        root = Path(reader.ROOT)
        version = "python%d.%d" % sys.version_info[:2]
        site = root / "install/roboparty_motors/lib" / version / "site-packages"
        if not site.is_dir():
            raise ValueError("已有deploy电机SDK不存在，请先独立构建；此入口不会安装")
        # Native SDK RX/TX threads explicitly choose ROBOT_CAN_CPU and can
        # override inherited taskset. Pin that choice to an allowed nonzero CPU.
        cpus = set(os.sched_getaffinity(0)) & {1, 2, 3, 7}
        if not cpus:
            raise ValueError("没有可用的非CPU0控制核")
        os.environ["ROBOT_CAN_CPU"] = str(7 if 7 in cpus else max(cpus))
        sys.path.insert(0, str(site))
        sdk = importlib.import_module("motors_py")
        self.feedback = CanFeedback(self.entries)
        groups = {}
        for entry in self.entries:
            control.check()
            if control.disable_requested.is_set():
                raise StopRequested("使能过程中收到失能请求")
            motor = sdk.MotorDriver.create_motor(
                entry["motor_id"], "canfd", entry["interface"], "LRO", entry["model"],
                16, entry["software_offset_rad"])
            self.motors.append(motor)
            groups.setdefault(entry["interface"], []).append((entry, motor))
        for task in Path("/proc/self/task").iterdir():
            try:
                if 0 in os.sched_getaffinity(int(task.name)):
                    raise ValueError("电机SDK线程仍允许运行于CPU0，拒绝继续控制")
            except ProcessLookupError:
                continue
        # Deploy creates every object before per-bus init. Python's init_motor
        # binding holds the GIL through its sleeps, so a Python thread pool would
        # still serialize all 24/28 motors. Interleave the same public SDK steps
        # across buses, preserving each bus's one-motor-at-a-time 5ms cadence.
        def check_init():
            control.check()
            if control.disable_requested.is_set():
                raise StopRequested("使能过程中收到失能请求")

        for ordinal in range(max(map(len, groups.values()))):
            batch = [group[ordinal] for group in groups.values() if ordinal < len(group)]
            for phase in ("unlock_motor", "set_motor_control_mode", "lock_motor", "refresh_motor_status"):
                for entry, motor in batch:
                    check_init()
                    if phase == "lock_motor":
                        self.feedback.note_enable_sent(entry["index"])
                    if phase == "set_motor_control_mode":
                        motor.set_motor_control_mode(sdk.MotorControlMode.MIT)
                    else:
                        getattr(motor, phase)()
                    check_init()
                time.sleep(SDK_INIT_STEP_S)
                check_init()
                self.feedback.poll()
            for entry, motor in batch:
                check_init()
                error_id = motor.get_error_id()
                if type(error_id) is not int or error_id != 0:
                    raise ValueError("电机使能失败：" + entry["interface"] + "/" + str(entry["motor_id"])
                                     + f"，SDK故障码 {error_id}")
        # Enable must earn fresh receive evidence; numeric zero is a valid angle.
        deadline = time.monotonic() + 1.0
        last_error = None
        while time.monotonic() < deadline:
            control.check()
            if control.disable_requested.is_set():
                raise StopRequested("等待使能反馈时收到失能请求")
            self.command([0.0] * len(self.entries), kp=0.0, kd=damping_kd)
            time.sleep(1 / CONTROL_HZ)
            control.check()
            if control.disable_requested.is_set():
                raise StopRequested("等待使能反馈时收到失能请求")
            try:
                result = self.feedback.snapshot()
            except ValueError as error:
                if isinstance(error, FeedbackUnavailable) and error.kind == "fault":
                    raise
                last_error = error
                continue
            control.check()
            if control.disable_requested.is_set():
                raise StopRequested("等待使能反馈时收到失能请求")
            return result
        detail = str(last_error) if last_error is not None else "未收到反馈"
        raise ValueError("使能确认失败：" + detail)

    def command(self, positions, *, kp, kd, feedforward=None):
        size = len(self.entries)

        def vector(value, name, low, high):
            values = [value] * size if type(value) in (int, float) else value
            if (not isinstance(values, (list, tuple)) or len(values) != size
                    or any(type(v) not in (int, float) or not math.isfinite(v)
                           or not low <= v <= high for v in values)):
                raise ValueError(name + "须为完整且在SDK范围内的有限数值")
            return values

        positions = vector(positions, "电机目标", -100, 100)
        kp, kd = vector(kp, "Kp", 0, 500), vector(kd, "Kd", 0, 50)
        feedforward = vector([0.0] * size if feedforward is None else feedforward, "前馈力矩", -30, 30)
        if len(self.motors) != size:
            raise ValueError("SDK电机实例与配置不完整")
        groups = {}
        for index, (entry, motor, position) in enumerate(zip(self.entries, self.motors, positions)):
            group = groups.setdefault(entry["interface"], {"motor": motor, "p": [0.0] * 8,
                                      "kp": [0.0] * 8, "kd": [0.0] * 8, "ff": [0.0] * 8})
            slot = entry["motor_id"] - 1
            # Reject saturation rather than silently clipping in the SDK.
            if abs(position - entry["software_offset_rad"]) > 12.5:
                raise ValueError("目标超出电机SDK位置编码范围")
            group["p"][slot], group["kp"][slot], group["kd"][slot] = position, kp[index], kd[index]
            group["ff"][slot] = feedforward[index]
        for group in groups.values():
            group["motor"].motors_mit_cmd(group["p"], [0.0] * 8, group["kp"], group["kd"], group["ff"])

    def disable(self):
        sent = bool(self.motors)
        for motor in self.motors:
            try:
                motor.deinit_motor()
            except Exception:
                sent = False
        return sent

    def close(self):
        if self.feedback:
            self.feedback.close()
        self.feedback = None
        self.motors.clear()


class ControlSession:
    def __init__(self, motors, dof, control, emit, *, driver=None, clock=time.monotonic, encoder_mapping=None):
        self.motors, self.dof, self.control, self.emit = motors, dof, control, emit
        self.driver = driver or MotorDriver(motors)
        self.clock = clock
        self.enabled, self.plan, self.disable_sent = False, None, False
        self.start_time, self.last_emit, self.last_tick = 0.0, 0.0, None
        self.intro, self.intro_duration = [], 0.0
        self.check_engine, self.check_mapping = None, None
        self.check_phase, self.check_group = None, None
        self.logical_positions, self.check_target = None, None
        self.check_run_id = None
        self.preparation, self.preparation_mapping = None, None
        self.preparation_phase, self.preparation_target = None, None
        self.preparation_speed_deg_s = None
        self.preparation_controller = None
        self.check_handoff = None
        self.check_feedforward = None
        self.check_control_context = None
        self.check_pause_target = self.check_pause_logical = None
        self.check_pause_motors = set()
        self.damping_kd = 0.5
        self.manual_engine = self.manual_mapping = self.manual_logical = None
        self.manual_run_id = None
        self._last_encoder_query = float("-inf")
        self.encoder_mapping = encoder_mapping

    def state(self, state, message):
        self.emit({"type": "state", "state": state, "message": message})

    def _holding_gains(self):
        if self.preparation_controller is not None:
            return self.preparation_controller.snapshot()
        options = self.preparation.options if self.preparation is not None else dict(kp=0.0, kd=self.damping_kd)
        return dict(kp=[options["kp"]] * self.dof, kd=[options["kd"]] * self.dof,
                    feedforward=[0.0] * self.dof)

    def _send_target(self, positions, *, kp, kd, feedforward=None):
        self.control.check()
        if self.control.disable_requested.is_set():
            raise StopRequested("发送目标前收到失能请求")
        elapsed = self.clock() - self.last_tick
        if elapsed > 0.15:
            raise ValueError(f"控制循环调度延迟超过150ms（目标计算{elapsed * 1000:.1f}ms），已中止")
        gains = dict(kp=kp, kd=kd)
        if feedforward is not None:
            gains["feedforward"] = feedforward
        self.driver.command(positions, **gains)

    def disable(self):
        self.state("stopping", "正在结束目标发送并发送失能指令")
        if self.manual_engine is not None:
            self.manual_engine.interrupt("控制会话结束；本轮记录保留，未完成项目不判通过")
            self._emit_manual()
            self.manual_engine = self.manual_mapping = self.manual_logical = None
        if self.check_engine is not None:
            self._check_result("控制结束，保留已读取证据；未完成项目不判通过")
            self.check_engine = None
        self.plan = None
        self.preparation, self.preparation_mapping = None, None
        self.preparation_phase, self.preparation_target = None, None
        self.preparation_speed_deg_s = None
        self.preparation_controller = None
        self.check_handoff = None
        self.check_feedforward = None
        self.check_control_context = None
        self.check_pause_target = self.check_pause_logical = None
        self.check_pause_motors = set()
        self.disable_sent = self.driver.disable()
        self.enabled = False
        self.driver.close()
        self.state("disabled", "已向SDK提交失能指令；请现场核对机器人，未验证物理停稳" if self.disable_sent
                   else "失能发送未得到完整执行回执，请现场核对")

    def handle(self, command):
        name = command.get("command")
        if name == "read_encoder":
            self._read_encoder(command)
        elif name == "read_joint_encoders":
            self._read_joint_encoders(command)
        elif name == "enable":
            if self.enabled:
                raise ValueError("电机已经使能，不会重复使能")
            try:
                if "damping_kd" in command:
                    kd = command["damping_kd"]
                    if type(kd) not in (int, float) or not math.isfinite(kd) or not 0 <= kd <= 0.5:
                        raise ValueError("手动阻尼Kd须在0至0.5之间")
                    self.driver.enable(self.control, damping_kd=kd)
                    self.damping_kd = kd
                else:
                    self.driver.enable(self.control)
                    self.damping_kd = 0.5
            except BaseException:
                diagnostic = getattr(self.driver.feedback, "diagnostics", None)
                if callable(diagnostic):
                    self.emit({"type": "enable_diagnostics", **diagnostic()})
                raise
            self.enabled, self.disable_sent, self.last_tick = True, False, None
            self.state("enabled", f"已收到全部电机使能反馈；Kp=0、Kd={self.damping_kd:g}，不保持站立")
        elif name == "manual_check":
            self._start_manual(command)
        elif name in {"manual_pause", "manual_retest", "manual_skip", "manual_confirm_pin"}:
            if not self.enabled or self.manual_engine is None:
                raise ValueError("请先开始人工标定检查")
            if name == "manual_pause":
                self.manual_engine.pause(command.get("paused"))
            elif name == "manual_retest":
                self.manual_engine.retest(command.get("index"))
            elif name == "manual_confirm_pin":
                self.manual_engine.confirm_pin(self.clock())
            else:
                self.manual_engine.skip()
            self._emit_manual()
        elif name == "run":
            if (not self.enabled or self.plan or self.check_engine is not None or self.manual_engine is not None
                    or self.preparation is not None or self.check_handoff is not None):
                raise ValueError("请先使能，且不能重复启动轨迹")
            positions, _ = self.driver.feedback.snapshot()
            options = {key: command.get(key) for key in (
                "speed_deg_s", "position_min_deg", "position_max_deg", "kp", "kd", "max_torque_nm")}
            plan = trajectory.prepare_run(command.get("trajectory"), self.dof, positions, options)
            expected = [m["interface"] + "/" + str(m["motor_id"]) for m in self.motors]
            if plan["trajectory"].get("motors") not in (None, expected):
                raise ValueError("轨迹电机列顺序与实际CAN配置不一致")
            for frame in plan["trajectory"]["positions_rad"]:
                if any(abs(p - m["software_offset_rad"]) > 12.5 for p, m in zip(frame, self.motors)):
                    raise ValueError("轨迹超出SDK位置编码范围")
            speed = math.radians(options["speed_deg_s"])
            self.intro = [SmoothMove.bounded(a, b, speed_rad_s=speed,
                          acceleration_rad_s2=math.radians(2), jerk_rad_s3=math.radians(5),
                          maximum_stroke_rad=math.radians(3)) for a, b in zip(
                              positions, plan["trajectory"]["positions_rad"][0])]
            self.intro_duration = max(move.duration_s for move in self.intro)
            # Synchronize every motor's first-frame approach to the slowest one.
            self.intro = [SmoothMove(move.start_rad, move.end_rad, self.intro_duration) for move in self.intro]
            self.plan, self.start_time, self.last_tick = plan, self.clock(), None
            self.state("running", "缓慢接近首帧并回放轨迹；结束后自动发送失能")
        elif name == "prepare_check":
            self._start_preparation(command)
        elif name == "check":
            self._start_check(command)
        elif name == "continue_check":
            if (not self.enabled or self.check_engine is None
                    or self.check_engine.phase != "waiting_review"
                    or command.get("confirmed") is not True):
                raise ValueError("仅能在标定检查暂停后，现场确认再继续下一组")
            positions, torques = self.driver.feedback.snapshot()
            logical, _ = joint_mapping.motor_to_logical(
                positions, torques, self.check_mapping, previous=self.logical_positions)
            self.check_engine.continue_group(self.clock(), logical, confirmed=True)
            self.state("running", "已确认恢复姿态，低速接续下一检查组")
        else:
            raise ValueError("电机控制命令无效")

    def _read_encoder(self, command):
        index, request_id = command.get("index"), command.get("request_id")
        if type(index) is not int or not 0 <= index < len(self.motors) or not isinstance(request_id, str):
            return
        try:
            if str(UUID(request_id)) != request_id:
                return
        except ValueError:
            return
        motor = self.motors[index]
        event = {"type": "encoder_position", "index": index, "request_id": request_id,
                 "interface": motor["interface"], "motor_id": motor["motor_id"]}
        try:
            control = self._encoder_query_control()
            with reader.open_can(motor["interface"], [motor["motor_id"]]) as connection:
                sample = reader.query_once(connection, motor["motor_id"], control, reader.WriterGuard(control))
        except (OSError, ValueError) as error:
            sample = dict(status="error", angle_deg=None, received_at=None, rx_monotonic_ns=None,
                          query_sent_monotonic_ns=None, raw_hex=None, fault=None, kernel_rx_timestamp=False,
                          message="".join(c if c.isprintable() else " " for c in str(error))[:240])
        self.emit({**event, **sample})

    def _encoder_query_control(self):
        control = EncoderQueryControl(self.control)
        control.check()
        if self.enabled or self.driver.motors:
            raise ValueError("仅未使能时可读取电机轴角度")
        now = self.clock()
        if now - self._last_encoder_query < reader.QUERY_TIMEOUT_NS / 1_000_000_000:
            raise ValueError("位置读取过于频繁，请稍后再试")
        self._last_encoder_query = now
        if reader.load_configuration(self.dof)[0] != self.motors:
            raise ValueError("控制会话中电机配置发生改变")
        check_other_writers(check_cancel=control.check)
        return control

    def _read_joint_encoders(self, command):
        joint, request_id = command.get("joint"), command.get("request_id")
        if not isinstance(joint, str) or not 1 <= len(joint) <= 64 or not joint.isascii() or not joint.isidentifier():
            return
        try:
            if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
                return
        except ValueError:
            return
        samples = []
        position = error = None
        try:
            if self.encoder_mapping is None:
                raise ValueError("当前版本未核对只读关节角映射")
            row = next((row for row in self.encoder_mapping["rows"] if row["name"] == joint), None)
            if row is None:
                raise ValueError("请求关节不在已核对的只读映射中")
            control = self._encoder_query_control()
            mapping = joint_mapping.encoder_observation_mapping(self.dof, self.motors, reader.ROOT)
            if mapping != self.encoder_mapping:
                raise ValueError("控制会话中只读关节映射发生改变")
            guard = reader.WriterGuard(control)
            for index in row["motor_indices"]:
                control.check()
                motor = self.motors[index]
                interface, motor_id = motor["interface"], motor["motor_id"]
                with reader.open_can(interface, [motor_id]) as connection:
                    sample = reader.query_once(connection, motor_id, control, guard)
                samples.append({"index": index, "interface": interface, "motor_id": motor_id, **sample})
                if sample["status"] != "fresh":
                    raise ValueError("当前关节缺少本次编码器反馈")
            skew = samples[-1]["rx_monotonic_ns"] - samples[0]["rx_monotonic_ns"]
            if not 0 <= skew <= ENCODER_PAIR_MAX_SKEW_NS:
                raise ValueError("踝部两路编码器反馈间隔过长")
            position = joint_mapping.encoder_joint_angle(
                mapping, joint, {sample["index"]: sample["angle_deg"] for sample in samples})
        except (OSError, ValueError) as failure:
            error = "".join(c if c.isprintable() else " " for c in str(failure))[:240] or "关节编码器读取失败"
        self.emit({"type": "joint_encoder_positions", "request_id": request_id, "joint": joint,
                   "samples": samples, "position_rad": position, "error": error})

    def _check_inputs(self, command, *, service=None):
        if (not self.enabled or self.plan or self.check_engine is not None or self.manual_engine is not None or self.check_handoff is not None
                or self.preparation is not None and self.preparation.phase != "prepared"):
            raise ValueError("请先使能，且不能同时运行轨迹或标定检查")
        if (command.get("mapping_confirmed") is not True
                or command.get("reference_confirmed") is not True):
            raise ValueError("请先核对具名关节映射、机械参考端点与本次检查参数")
        plan = check_logic.validate_video_plan(command.get("plan"), self.dof)
        if plan["plan_sha256"] != VIDEO_PLAN_SHA256[self.dof]:
            raise ValueError("检查路线与本版本的视频方案摘要不一致；不执行修改后的模型/轨迹")
        validator = (check_logic.validate_preparation_options if command.get("command") == "prepare_check"
                     else check_logic.validate_probe_pairing)
        options = validator(command.get("options"))
        if service is not None:
            service()
        mapping = joint_mapping.version_bound_mapping(self.dof, self.motors, root=reader.ROOT)
        if options.get("gain_profile"):
            profile = preparation_gain_profile(self.dof)
            if (options["gain_profile"] != profile["profile_id"]
                    or mapping["source_sha256"].get(profile["robot_yaml_path"]) != profile["robot_yaml_sha256"]):
                raise ValueError("复位逐电机增益与已核对deploy版本不符")
        if plan["joint_names"] != mapping["joint_names"]:
            raise ValueError("视频具名关节与当前deploy绑定不一致")
        if service is not None:
            service()
        positions, torques = self.driver.feedback.snapshot()
        logical, _ = joint_mapping.motor_to_logical(positions, torques, mapping)
        return plan, options, mapping, positions, logical

    def _start_manual(self, command):
        if (not self.enabled or self.plan or self.check_engine is not None
                or self.manual_engine is not None and self.manual_engine.phase != "complete"
                or self.preparation is not None or self.check_handoff is not None):
            raise ValueError("请先使能，且不能同时运行轨迹或其他检查")
        if command.get("mapping_confirmed") is not True or command.get("reference_confirmed") is not True:
            raise ValueError("请先核对关节映射和本次限位参考")
        plan = manual_check_plan.validate_manual_plan(command.get("plan"), self.dof)
        self.tick()
        mapping = joint_mapping.version_bound_mapping(self.dof, self.motors, root=reader.ROOT)
        self.tick()
        if plan["joint_names"] != mapping["joint_names"]:
            raise ValueError("人工检查关节名称与deploy映射不符")
        engine = manual_logic.ManualCalibrationCheck(plan, command.get("options"))
        if engine.options.get("torque_required") is not False:
            raise ValueError("人工检查的力矩只作辅助记录")
        self.manual_engine, self.manual_mapping = engine, mapping
        self.manual_logical = None
        self.manual_run_id = str(uuid4())
        self._emit_manual()

    def _emit_manual(self):
        self.emit({"type": "manual_state", "report": self.manual_engine.report(),
                   "run_id": self.manual_run_id, "damping_kd": self.damping_kd,
                   "torque_basis": "estimated_from_motor_iq", "timestamp_basis": "kernel_so_timestampns",
                   "sampling_hz": CONTROL_HZ,
                   "feedback_evidence": [dict(index=i, kernel_received_ns=value.get("kernel_received_ns"),
                                              raw_hex=value.get("raw_hex"))
                                         for i, value in enumerate(self.driver.feedback.values)
                                         if value is not None]})

    def _tick_manual(self, now, positions, torques):
        logical, joint_torques = joint_mapping.motor_to_logical(
            positions, torques, self.manual_mapping, previous=self.manual_logical)
        if self.manual_logical is None:
            # First solve ankles without an invented previous pose; their FK
            # has its own continuity guard. Choose the URDF winding for direct
            # joints only (shoulder homes cross +/-pi), then track continuously.
            ranges = self.manual_engine.plan["original_ranges_rad"]
            for row in self.manual_mapping["rows"]:
                if row["coupled_side"] is None:
                    j = row["joint_index"]
                    middle = sum(ranges[j]) / 2
                    logical[j] += math.tau * round((middle - logical[j]) / math.tau)
        self.manual_logical = logical
        times = [min(self.driver.feedback.values[i]["received"] for i in row["motor_indices"])
                 for row in self.manual_mapping["rows"]]
        before = (self.manual_engine.phase, self.manual_engine.current_index)
        after = self.manual_engine.update(now, logical, joint_torques, sample_times_s=times)
        # No preparation, search, spring or retreat target exists in this path.
        self._send_target(positions, kp=0.0, kd=self.damping_kd, feedforward=[0.0] * self.dof)
        if now - self.last_emit >= 0.2 or before != (after["phase"], after["current_index"]):
            self.last_emit = now
            self._emit_manual()
            self.emit({"type": "feedback", "positions_rad": positions, "torques_nm": torques,
                       "progress": sum(row["status"] == "passed" for row in after["results"]) / len(after["results"]),
                       "enabled": self.enabled})

    def _start_preparation(self, command):
        if command.get("preparation_confirmed") is not True:
            raise ValueError("请先确认从实际姿态低速复位的逐关节目标与现场运动路线")
        options = check_logic.validate_preparation_options(command.get("options"))
        requested_at = serviced_at = self.clock()
        timeout = options["preparation_timeout_s"]
        deadline = requested_at + timeout

        def service():
            nonlocal serviced_at
            self.control.check()
            if self.control.disable_requested.is_set():
                raise StopRequested("姿态准备预检期间收到失能请求")
            now = self.clock()
            if now >= deadline:
                raise ValueError(f"复位预检已耗尽{timeout:g}秒完成期限，尚未发送复位目标")
            if now - serviced_at > 0.15:
                raise ValueError("姿态准备预检调度延迟超过150ms，已中止")
            if now - serviced_at >= 1 / CONTROL_HZ:
                # The SDK consumes queued frames; it does not keep resending.
                # Keep the existing idle damping (or prepared hold) and drain
                # feedback while previewing IK, before any new spring target.
                self.tick(abort_torque_nm=options["abort_torque_nm"])
                self.control.check()
                if not self.enabled or self.control.disable_requested.is_set():
                    raise StopRequested("姿态准备预检期间收到失能请求")
                serviced_at = self.last_tick

        plan, options, mapping, positions, logical = self._check_inputs(command, service=service)
        confirmed = check_logic.start_pose_report(plan, command.get("confirmed_start_rad"))
        rows = check_logic.start_pose_report(
            dict(plan, initial_pose_rad=[row["actual_rad"] for row in confirmed]), logical, 0.5)
        if options["require_pose_settling"] and not all(row["within_tolerance"] for row in rows):
            raise StartPoseRejected(rows, 0.5, command="prepare_check")
        now = self.clock()
        self._check_times(mapping, now)
        preparation = check_logic.PreparationMove(plan, options, logical, now_s=now, deadline_s=deadline)
        motor_speed_limit = None if preparation.deploy_reset else options["speed_deg_s"]
        # Preview the paired IK route before accepting any target. A logical
        # ankle degree need not equal a physical motor degree; slow the shared
        # profile when needed, and keep a strict per-tick motor speed check.
        previous = positions[:]
        maximum_speed = 0.0
        samples = 128
        for index in range(1, samples + 1):
            service()
            at = preparation.duration_s * index / samples
            pose = preparation.sample_targets(at)
            motors = joint_mapping.logical_to_motor(pose, mapping, previous=previous)
            if any(abs(value - motor["software_offset_rad"]) > 12.5
                   for value, motor in zip(motors, self.motors)):
                raise ValueError("姿态准备路线超出电机SDK位置编码范围")
            maximum_speed = max(maximum_speed, max(abs(a - b) for a, b in zip(motors, previous))
                                / (preparation.duration_s / samples))
            previous = motors
        service()
        factor = 1.0 if preparation.deploy_reset else max(1.0, maximum_speed / math.radians(motor_speed_limit) * 1.1)
        if factor > 1.0:
            options = dict(options, speed_deg_s=preparation.effective_speed_deg_s / factor)
        # Planning must not turn an old receive snapshot into the first stiff
        # target. Re-read after IK work and start the clock/profile from it.
        positions, torques = self.driver.feedback.snapshot()
        logical, _ = joint_mapping.motor_to_logical(positions, torques, mapping, previous=logical)
        rows = check_logic.start_pose_report(
            dict(plan, initial_pose_rad=[row["actual_rad"] for row in confirmed]), logical, 0.5)
        if options["require_pose_settling"] and not all(row["within_tolerance"] for row in rows):
            raise StartPoseRejected(rows, 0.5, command="prepare_check")
        now = self.clock()
        self._check_times(mapping, now)
        preparation = check_logic.PreparationMove(plan, options, logical, now_s=now, deadline_s=deadline)
        old_gains = self._holding_gains()
        self.preparation_controller = (PreparationController(
            preparation_gain_profile(self.dof), options["abort_torque_nm"], positions, now,
            initial_gains=old_gains,
            initial_feedforward=old_gains["feedforward"], deploy_reset=preparation.deploy_reset)
            if options.get("gain_profile") else None)
        self.preparation, self.preparation_mapping = preparation, mapping
        self.preparation_target, self.preparation_phase = positions[:], None
        self.preparation_speed_deg_s = motor_speed_limit
        self.logical_positions = logical[:]
        detail = (f"本次准备允许偏差±{options['preparation_tolerance_deg']:g}°，须反馈确认后保持"
                  if options["require_pose_settling"] else "目标发送完毕后保持，不以角差或停稳窗口阻断；未验证实际到位")
        if preparation.deploy_reset:
            self.state("preparing", "按deploy固定4秒平滑复位到检查起点，20ms周期；运动Kp=配置值÷2.5，"
                       "Kd沿用配置，结束恢复完整Kp；目标发完后保持，等待单独开始检查")
        else:
            self.state("preparing", f"{timeout:g}秒内发送复位目标，逻辑/电机目标均不超过{motor_speed_limit:g}°/s，"
                       f"运动预计{preparation.duration_s:.1f}s；{detail}，等待单独开始检查")

    def _start_check(self, command):
        plan, options, mapping, positions, logical = self._check_inputs(command)
        rows = check_logic.start_pose_report(plan, logical, options["start_tolerance_deg"])
        if options["require_pose_settling"] and not all(row["within_tolerance"] for row in rows):
            raise StartPoseRejected(rows, options["start_tolerance_deg"])
        now = self.clock()
        self._check_times(mapping, now)
        run_id = str(UUID(command["run_id"])) if command.get("run_id") else str(uuid4())
        if self.preparation_controller is not None or command.get("gain_handoff_confirmed") is True:
            if command.get("gain_handoff_confirmed") is not True:
                raise ValueError("请先确认复位保持到检查增益的平滑交接及补偿冻结")
            gains = self._holding_gains()
            ff_limit = 2.5 if options["abort_torque_nm"] is None else min(2.5, options["abort_torque_nm"] * 0.25)
            if any(abs(value) > ff_limit for value in gains["feedforward"]):
                raise ValueError("已有复位补偿超过本次检查的补偿限幅，请先失能并按本次力矩重新确认复位参数")
            target = self.preparation_target[:] if self.preparation is not None else positions[:]
            holding_logical = (self.preparation.targets[:] if self.preparation is not None else logical[:])
            self.check_handoff = dict(plan=plan, options=options, mapping=mapping, run_id=run_id,
                                      started=now, target=target, logical_target=holding_logical, gains=gains,
                                      preparation_profile=(preparation_gain_profile(self.dof)
                                                           if self.preparation_controller is not None else None),
                                      windows=[[] for _ in range(self.dof)], times=None, positions=logical[:])
            self.preparation, self.preparation_mapping, self.preparation_controller = None, None, None
            self.logical_positions = logical
            self.state("preparing", "正在2秒平滑切换到本次检查增益并保持原目标；"
                       + ("随后核对实际起点与停稳" if options["require_pose_settling"] else "切换结束后续接，不核对角差或停稳窗口")
                       + "，尚未开始检查")
            return
        self._install_check(plan, options, mapping, positions, logical, now, run_id)

    def _install_check(self, plan, options, mapping, positions, logical, now, run_id, feedforward=None,
                       holding_target=None, preparation_profile=None, holding_logical=None):
        engine = check_logic.CalibrationCheck(plan, options, logical, now_s=now)
        if holding_target is not None:
            if holding_logical is None:
                holding_logical, _ = joint_mapping.motor_to_logical(holding_target, [0.0] * self.dof, mapping)
            # Start the new command path at the command actually sent during
            # handoff. Starting at measured angles would abruptly remove the
            # residual spring load, and would evade the per-command step gate.
            engine.approach = engine.approach | {"start": holding_logical}
            engine.targets = holding_logical[:]
            engine._approach_timing()
        # Establish complete paired kernel timestamps before accepting motion.
        self._check_times(mapping, now)
        self.check_mapping, self.check_engine = mapping, engine
        self.check_feedforward = feedforward
        self.check_control_context = dict(
            preparation_profile=preparation_profile,
            frozen_feedforward_nm=[0.0] * self.dof if feedforward is None else feedforward[:],
            feedforward_motor_order="physical_motor",
            feedforward_unloaded_on_obstruction=False,
            handoff_ramp_s=2.0 if holding_target is not None else 0.0)
        self.logical_positions = logical
        self.check_target = list(positions if holding_target is None else holding_target)
        self.check_phase, self.check_group = None, None
        self.check_run_id = run_id
        self.check_pause_target = self.check_pause_logical = None
        self.check_pause_motors = set()
        self.preparation, self.preparation_mapping = None, None
        self.preparation_phase, self.preparation_target = None, None
        self.preparation_speed_deg_s = None
        self.preparation_controller = None
        self.check_handoff = None
        self.start_time = now
        self.state("running", "按视频分组进行单侧角度/力矩候选检查；不是固定轨迹回放")

    def _tick_handoff(self, now, positions, torques):
        handoff = self.check_handoff
        options, mapping = handoff["options"], handoff["mapping"]
        if options["abort_torque_nm"] is not None and any(abs(value) >= options["abort_torque_nm"] for value in torques):
            raise ValueError("检查参数交接期间达到中止力矩，尚未开始检查")
        logical, joint_torques = joint_mapping.motor_to_logical(
            positions, torques, mapping, previous=self.logical_positions)
        if options["abort_torque_nm"] is not None and any(abs(value) >= options["abort_torque_nm"] for value in joint_torques):
            raise ValueError("检查参数交接期间关节力矩达到中止值，尚未开始检查")
        self.logical_positions = logical
        times = self._check_times(mapping, now)
        tracking_limit = max(5.0, options["start_tolerance_deg"])
        if options["require_pose_settling"] and any(abs(actual - target) > math.radians(tracking_limit) + 1e-12
               for actual, target in zip(logical, handoff["logical_target"])):
            raise ValueError(f"检查参数交接期间逻辑关节偏差超过{tracking_limit:g}°，尚未开始检查")
        elapsed = now - handoff["started"]
        gains = interpolate_gains(handoff["gains"], dict(kp=[options["kp"]] * self.dof,
                                   kd=[options["kd"]] * self.dof), elapsed, duration_s=2.0)
        frozen = handoff["gains"]["feedforward"]
        if options["require_pose_settling"] and elapsed > 6.0:
            rows = check_logic.start_pose_report(handoff["plan"], logical, options["start_tolerance_deg"])
            failed = [row["joint"] + f" {row['deviation_deg']:+.3f}°" for row in rows if not row["within_tolerance"]]
            raise ValueError("检查增益交接后实际起点或停稳核对未通过，尚未开始检查；"
                             + ("；".join(failed[:3]) if failed else "连续0.5秒反馈仍未停稳"))
        if elapsed >= 2.0 and not options["require_pose_settling"]:
            self._install_check(handoff["plan"], options, mapping, positions, logical, now,
                                handoff["run_id"], frozen[:], holding_target=handoff["target"],
                                holding_logical=handoff["logical_target"], preparation_profile=handoff["preparation_profile"])
        elif elapsed >= 2.0:
            for index, window in enumerate(handoff["windows"]):
                old = handoff["times"]
                if old is not None and times[index] < old[index]:
                    raise ValueError("检查参数交接反馈时间倒退")
                if old is not None and times[index] == old[index]:
                    if logical[index] != handoff["positions"][index]:
                        window.clear()
                    continue
                if window and times[index] - window[-1][0] > FEEDBACK_TIMEOUT_S:
                    window.clear()
                window.append((times[index], logical[index]))
                while len(window) >= 3 and window[1][0] <= times[index] - 0.5 + 1e-9:
                    window.pop(0)
            rows = check_logic.start_pose_report(handoff["plan"], logical, options["start_tolerance_deg"])
            settled = all(len(w) >= 3 and w[-1][0] - w[0][0] >= 0.5 - 1e-9
                          and max(p for _, p in w) - min(p for _, p in w) <= math.radians(0.075) + 1e-12
                          for w in handoff["windows"])
            if settled and all(row["within_tolerance"] for row in rows):
                self._install_check(handoff["plan"], options, mapping, positions, logical, now,
                                    handoff["run_id"], frozen[:], holding_target=handoff["target"],
                                    holding_logical=handoff["logical_target"],
                                    preparation_profile=handoff["preparation_profile"])
        handoff["times"], handoff["positions"] = times, logical[:]
        self._send_target(handoff["target"], **gains, feedforward=frozen)
        if now - self.last_emit >= 0.2:
            self.last_emit = now
            self.emit({"type": "feedback", "positions_rad": positions, "torques_nm": torques,
                       "progress": 0.0, "enabled": self.enabled})

    def _tick_preparation(self, now, positions, torques):
        preparation, mapping = self.preparation, self.preparation_mapping
        if preparation.options["abort_torque_nm"] is not None and any(
                abs(value) >= preparation.options["abort_torque_nm"] for value in torques):
            raise ValueError("姿态准备电机输出力矩达到本次中止值")
        logical, joint_torques = joint_mapping.motor_to_logical(
            positions, torques, mapping, previous=self.logical_positions)
        self.logical_positions = logical
        elapsed = now - preparation.now
        step = preparation.step(now, logical, joint_torques,
                                feedback_times=self._check_times(mapping, now))
        # Once the endpoint is sent, keep that exact motor target. Re-solving a
        # closed chain from noisy measured seeds would move the fixed endpoint
        # and repeatedly reset the bounded load-trim controller.
        seed = positions if preparation.options["require_pose_settling"] else self.preparation_target
        target = (self.preparation_target[:] if self.preparation_phase in ("confirm_stop", "prepared") else
                  joint_mapping.logical_to_motor(step["targets_rad"], mapping, previous=seed))
        # Position tolerance is checked in named logical coordinates above.
        # A coupled ankle's physical motor residual need not have that magnitude.
        if preparation.options["require_pose_settling"] and any(abs(a - b) > math.radians(0.5) for a, b in zip(self.preparation_target, target)):
            raise ValueError("姿态准备电机目标单步变化超过0.5°，已中止")
        if not preparation.deploy_reset and any(abs(a - b) > math.radians(self.preparation_speed_deg_s) * elapsed + 1e-6
               for a, b in zip(self.preparation_target, target)):
            raise ValueError("姿态准备物理电机目标速度超过本次限速，已中止")
        if self.preparation_phase != "prepared" and self.clock() > preparation.deadline:
            raise ValueError(f"复位超过{preparation.options['preparation_timeout_s']:g}秒完成期限，"
                             "未确认到位停稳，须中止并失能")
        self.preparation_target = target
        if self.preparation_controller is not None:
            gains = self.preparation_controller.command(
                now, target, positions, torques,
                settling=preparation.options["require_pose_settling"] and step["phase"] in ("confirm_stop", "prepared"),
                feedback_times=[value["received"] for value in self.driver.feedback.values])
            self._send_target(target, **gains)
        else:
            self._send_target(target, kp=preparation.options["kp"], kd=preparation.options["kd"])
        if step["phase"] != self.preparation_phase:
            self.preparation_phase = step["phase"]
            self.emit({"type": "prepare_state", "phase": step["phase"],
                       "progress": step["progress"], "message": {
                           "gain_ramp": "保持新鲜实际起点，2秒平滑建立逐电机复位增益",
                           "preparing": "正在从实际角度低速复位，不执行限位搜索",
                           "confirm_stop": f"复位目标已发送，正在按本次±{preparation.options['preparation_tolerance_deg']:g}°核对姿态及停稳",
                           "prepared": (f"检查起点已满足本次±{preparation.options['preparation_tolerance_deg']:g}°准备允许偏差并保持"
                                        if preparation.options["require_pose_settling"] else "复位目标已发送并保持，未验证实际到位；可单独开始检查"),
                       }[step["phase"]]})
            if step["phase"] == "prepared":
                self.state("prepared", (f"检查起点已满足±{preparation.options['preparation_tolerance_deg']:g}°准备允许偏差并保持"
                                       if preparation.options["require_pose_settling"] else "复位目标已发送并保持，未验证实际到位")
                           + "，尚未开始标定检查")
        if now - self.last_emit >= 0.2:
            self.last_emit = now
            self.emit({"type": "feedback", "positions_rad": positions, "torques_nm": torques,
                       "progress": step["progress"], "enabled": self.enabled})

    def _check_times(self, mapping, now):
        values = getattr(self.driver.feedback, "values", None)
        if (not isinstance(values, list) or len(values) != self.dof
                or any(not isinstance(value, dict) or type(value.get("received")) not in (int, float)
                       or not math.isfinite(value["received"])
                       or not 0 <= now - value["received"] <= FEEDBACK_TIMEOUT_S for value in values)):
            raise ValueError("标定检查缺少逐电机新鲜内核接收时间")
        return [min(values[i]["received"] for i in indices)
                for indices in mapping["joint_motor_indices"]]

    def _check_result(self, reason):
        result = self.check_engine.result()
        if result.get("phase") == "aborted":
            reason = result.get("abort_reason") or "检查已中止，控制端未提供具体原因"
        result.update(
            source_kind="hardware", run_id=self.check_run_id,
            source_plan_kind="simulated", robot_dof=self.dof,
            plan_sha256=self.check_engine.plan["plan_sha256"],
            urdf_sha256=self.check_engine.plan["urdf_sha256"],
            model_sha256=self.check_engine.plan["model_sha256"],
            joint_names=self.check_engine.plan["joint_names"],
            mapping_rows=self.check_mapping["rows"],
            motor_control=dict(self.check_control_context),
            options=self.check_engine.options, reason=reason,
            candidate_only=True, physical_stop_confirmed=False, calibration_passed=None,
        )
        self.emit({"type": "check_result", "result": result})

    def _tick_check(self, now, positions, torques):
        engine, mapping = self.check_engine, self.check_mapping
        if engine.options["abort_torque_nm"] is not None and any(
                abs(t) >= engine.options["abort_torque_nm"] for t in torques):
            raise ValueError("电机输出力矩达到本次检查中止值")
        logical, joint_torques = joint_mapping.motor_to_logical(
            positions, torques, mapping, previous=self.logical_positions)
        self.logical_positions = logical
        buffer = math.radians(engine.options["search_overtravel_deg"])
        if engine.options["require_pose_settling"] and any(not low - buffer <= position <= high + buffer
               for position, (low, high) in zip(logical, engine.plan["original_ranges_rad"])):
            raise ValueError("逻辑关节反馈超过参考范围与本次受限搜索余量")
        phase_before = engine.phase
        step = engine.step(now, logical, joint_torques, feedback_times=self._check_times(mapping, now))
        grouped_pause = not engine.options["require_pose_settling"]
        if grouped_pause and phase_before == "transition":
            self.check_pause_target = self.check_pause_logical = None
            self.check_pause_motors = set()
        if grouped_pause and self.check_pause_target is None and (
                step["phase"] == "confirm_stop" or phase_before == "confirm_stop"):
            # Include both motors and both logical coordinates of a closed
            # ankle pair. Partially unloading roll while holding pitch can
            # leave a spring torque in the supposedly unloaded direction.
            affected = {i for joint in engine.selected for i in mapping["joint_motor_indices"][joint]}
            paused = self.check_target[:]
            for index in affected:
                paused[index] = positions[index]
            self.check_pause_target, self.check_pause_motors = paused, affected
            self.check_pause_logical, _ = joint_mapping.motor_to_logical(
                paused, [0.0] * self.dof, mapping, previous=logical)
            engine.targets = self.check_pause_logical[:]
            step["targets_rad"] = engine.targets[:]
            if self.check_feedforward is not None:
                self.check_feedforward = self.check_feedforward[:]
                for index in affected:
                    self.check_feedforward[index] = 0.0
                self.check_control_context["feedforward_unloaded_on_obstruction"] = True
            self.check_control_context["group_pauses"] = [*self.check_control_context.get("group_pauses", []), {
                "group": engine.group, "unloaded_motor_indices": sorted(affected),
                "held_motor_indices": [i for i in range(self.dof) if i not in affected],
                "commanded_pause_targets_rad": self.check_pause_logical[:],
            }]
        if grouped_pause and self.check_pause_target is not None and phase_before == "confirm_stop" and step["phase"] == "retreat":
            affected_joints = [joint for joint, indices in enumerate(mapping["joint_motor_indices"])
                               if any(i in self.check_pause_motors for i in indices)]
            index = max(affected_joints, key=lambda joint: abs(logical[joint] - self.check_pause_logical[joint]))
            delta = math.degrees(logical[index] - self.check_pause_logical[index])
            # The measured replay must join the fixed pause command. Reusing
            # the existing 0.2-degree logical-path edge bound here prevents a
            # new jump to a rebounded pose without disguising it as a retreat.
            if abs(delta) > 0.2:
                engine.phase = step["phase"] = "aborted"
                engine.abort_reason = f"缓退起点与固定暂停目标不连续：{engine.plan['joint_names'][index]} 偏移{delta:+.3f}°，未发送缓退目标"
                engine.retreat_path_diagnostics = {**(engine.retreat_path_diagnostics or {}), "pause_join": {
                    "joint": engine.plan["joint_names"][index], "deviation_deg": delta,
                    "limit_deg": 0.2, "verified": False,
                }}
                axes = engine.candidates or set(engine.selected)
                engine.retreat_actual_deg, engine.retreat_verified = None, False
                engine.retreat_joint_distances = [
                    {"joint": engine.plan["joint_names"][joint], "requested_deg": engine.options["retreat_deg"],
                     "actual_deg": None, "verified": False} for joint in sorted(axes)
                ]
                engine.retreats.append({
                    "group": engine.group, "requested_deg": engine.options["retreat_deg"],
                    "actual_deg": None, "verified": False, "reason": engine.abort_reason,
                    "joint_distances": engine.retreat_joint_distances,
                })
                step["done"] = True
                step["events"] = [event for event in step["events"] if event.get("type") != "retreat_started"]
                step["events"].append({"type": "review_required", "reason": engine.abort_reason})
        unloading = (phase_before == "confirm_stop" or step["phase"] == "confirm_stop") and all(
            abs(actual - requested) <= 1e-12 for actual, requested in zip(logical, step["targets_rad"]))
        seed = (positions if engine.options["require_pose_settling"] or unloading else self.check_target)
        if grouped_pause and self.check_pause_target is not None and (step["phase"] == "confirm_stop" or phase_before == "confirm_stop"):
            target = self.check_pause_target[:]
            engine.targets = self.check_pause_logical[:]
            step["targets_rad"] = engine.targets[:]
        else:
            target = joint_mapping.logical_to_motor(step["targets_rad"], mapping, previous=seed)
            if grouped_pause and self.check_pause_target is not None and step["phase"] in ("retreat", "confirm_retreat_stop", "waiting_review"):
                for index in range(self.dof):
                    if index not in self.check_pause_motors:
                        target[index] = self.check_pause_target[index]
                engine.targets, _ = joint_mapping.motor_to_logical(target, [0.0] * self.dof, mapping, previous=logical)
                step["targets_rad"] = engine.targets[:]
        # The engine checks logical tracking with the confirmed MIT tolerance;
        # do not impose a second, unrelated five-degree motor-coordinate gate.
        tracking_limit = max(5.0, engine.options["start_tolerance_deg"])
        if engine.options["require_pose_settling"] and any(abs(actual - requested) > math.radians(tracking_limit) + 1e-12
               for actual, requested in zip(logical, step["targets_rad"])):
            raise ValueError(f"检查逻辑关节跟踪偏差超过{tracking_limit:g}°，已中止")
        # A detected obstruction must immediately remove the advancing spring
        # target. Setting every target to its fresh measured position unloads
        # that spring; it is not a requested movement of the stalled joint.
        # Legacy callers retain the step limit. The desktop calibration path
        # follows its confirmed logical curve; ankle transmission ratios do not
        # impose a second fixed 20°/s motor-command abort.
        freeze_at_feedback = (unloading and all(
            abs(a - b) <= 1e-6 for a, b in zip(positions, target)))
        if freeze_at_feedback and not grouped_pause and self.check_feedforward is not None:
            # Removing the advancing spring must also unload any preparation
            # compensation. Never keep pushing a detected obstruction with FF.
            self.check_feedforward = [0.0] * self.dof
            self.check_control_context["feedforward_unloaded_on_obstruction"] = True
        if engine.options["require_pose_settling"] and self.check_target is not None and not freeze_at_feedback:
            delta = max(abs(a - b) for a, b in zip(self.check_target, target))
            if delta > math.radians(0.5):
                raise ValueError("标定检查电机目标单步变化超过0.5°，已中止")
        self.check_target = target
        self._send_target(target, kp=engine.options["kp"], kd=engine.options["kd"],
                          feedforward=self.check_feedforward)
        if step["phase"] != self.check_phase or step["group"] != self.check_group or step["events"]:
            self.check_phase, self.check_group = step["phase"], step["group"]
            self.emit({"type": "check_state", "phase": step["phase"], "group": step["group"],
                       "progress": step["progress"], "waiting_review": step["phase"] == "waiting_review",
                       "message": "；".join(str(event.get("reason") or event.get("message")
                                                or event.get("status") or event.get("type", ""))
                                            for event in step["events"])[:400]})
            if step["events"] or step["phase"] == "waiting_review":
                self._check_result("本组反馈记录；候选阻挡不自动等于机械止挡")
        if now - self.last_emit >= 0.2:
            self.last_emit = now
            self.emit({"type": "check_sample", "time_s": max(0.0, now - self.start_time),
                       "positions_rad": logical, "torques_nm": joint_torques,
                       "targets_rad": step["targets_rad"], "phase": step["phase"], "group": step["group"]})
            self.emit({"type": "feedback", "positions_rad": positions, "torques_nm": torques,
                       "progress": step["progress"], "enabled": self.enabled})
        if step["done"]:
            self._check_result("检查阶段结束；各项目以实测候选证据与未覆盖状态分别保存")
            self.check_engine = None
            self.disable()

    def tick(self, *, abort_torque_nm=None):
        self.control.check()
        if self.control.disable_requested.is_set():
            self.control.disable_requested.clear()
            self.disable()
            # A queued run must not survive a higher-priority disable request.
            while not self.control.commands.empty():
                self.control.commands.get_nowait()
            return
        if not self.enabled:
            return
        before_receive = self.clock()
        if self.last_tick is not None and before_receive - self.last_tick > 0.15:
            gap = (before_receive - self.last_tick) * 1000
            raise ValueError(f"控制循环调度延迟超过150ms（等待控制周期{gap:.1f}ms），已中止")
        positions, torques = self.driver.feedback.snapshot()
        # snapshot polls CAN and may receive frames newer than before_receive.
        # Date this cycle afterwards, retaining the bound on blocking receive
        # work even when this is the first enabled control tick.
        now = self.clock()
        if now - (self.last_tick if self.last_tick is not None else before_receive) > 0.15:
            elapsed = (now - before_receive) * 1000
            raise ValueError(f"控制循环调度延迟超过150ms（接收反馈{elapsed:.1f}ms），已中止")
        self.last_tick = now
        if abort_torque_nm is not None and any(abs(value) >= abort_torque_nm for value in torques):
            raise ValueError("姿态准备电机输出力矩达到本次中止值")
        if self.manual_engine is not None:
            self._tick_manual(now, positions, torques)
            return
        if self.check_handoff is not None:
            self._tick_handoff(now, positions, torques)
            return
        if self.preparation is not None:
            self._tick_preparation(now, positions, torques)
            return
        if self.check_engine is not None:
            self._tick_check(now, positions, torques)
            return
        progress = 0.0
        if self.plan:
            options = self.plan["options"]
            if any(abs(t) >= options["max_torque_nm"] for t in torques):
                raise ValueError("达到本次力矩中止值")
            lower, upper = math.radians(options["position_min_deg"]), math.radians(options["position_max_deg"])
            if any(not lower <= p <= upper for p in positions):
                raise ValueError("电机实际位置超出本次允许范围")
            elapsed = now - self.start_time
            if elapsed < self.intro_duration:
                target = [move.sample(elapsed)[0] for move in self.intro]
            else:
                target = trajectory.sample_trajectory(self.plan, elapsed - self.intro_duration)
            if any(abs(a - b) > math.radians(5) for a, b in zip(positions, target)):
                raise ValueError("位置跟踪偏差超过5°，已中止")
            self._send_target(target, kp=options["kp"], kd=options["kd"])
            progress = min(1.0, elapsed / (self.intro_duration + self.plan["duration_s"]))
            if progress >= 1:
                self.disable()
        else:
            # Keep fresh telemetry with no position spring while awaiting a run.
            self._send_target(positions, kp=0.0, kd=self.damping_kd)
        if now - self.last_emit >= 0.2:
            self.last_emit = now
            self.emit({"type": "feedback", "positions_rad": positions, "torques_nm": torques,
                       "progress": progress, "enabled": self.enabled})


def run_session(dof, session_id):
    session_id = str(UUID(session_id))
    lock, session, control = None, None, InputPump(sys.stdin.buffer, session_id)
    def emit(event):
        print(json.dumps({**event, "session_id": session_id, "source_kind": "hardware"},
                         ensure_ascii=False, allow_nan=False), flush=True)
    reason = "会话结束"
    try:
        if os.geteuid() != 0:
            raise ValueError("电机控制需要本次sudo授权")
        affinity = reader.restrict_affinity()
        control.start()  # Every helper/SDK thread inherits the non-CPU0 affinity.
        lock = acquire_lock()
        check_other_writers()
        motors, digest = reader.load_configuration(dof)
        try:
            mapping = joint_mapping.version_bound_mapping(dof, motors, root=reader.ROOT)
            check_ready, check_reason = True, ""
            mapping_summary = {"rows": mapping["rows"], "joint_names": mapping["joint_names"]}
        except (OSError, ValueError) as error:
            check_ready, check_reason, mapping_summary = False, str(error)[:400], None
        try:
            encoder_mapping = joint_mapping.encoder_observation_mapping(dof, motors, reader.ROOT)
        except (OSError, ValueError):
            encoder_mapping = None
        emit({"type": "metadata", "dof": dof, "motors": motors, "configuration_sha256": digest,
              "position_basis": "motor_output_sdk_rad", "affinity": affinity,
              "torque_basis": "estimated_from_motor_iq", "timestamp_basis": "kernel_so_timestampns",
              "calibration_check_ready": check_ready, "calibration_check_reason": check_reason,
              "calibration_mapping": mapping_summary, "encoder_observation_ready": encoder_mapping is not None})
        validate_driver_sources(reader.ROOT)
        report = verifier.verify_runtime_receipt(
            reader.ROOT, "/home/robo/.local/state/rp1-monitor/calibration/runtime-build.json")
        if report["status"] != "code_built":
            raise ValueError("现有驱动缺少匹配的构建记录，请先独立构建：" + "；".join(report["reasons"]))
        session = ControlSession(motors, dof, control, emit, encoder_mapping=encoder_mapping)
        session.state("ready", "配置核对完成；尚未导入SDK、使能或发送电机指令")
        began, next_tick = time.monotonic(), time.monotonic()
        last_process_check = began

        def service_process_scan():
            # /proc can contain many processes. Yield between entries to keep
            # normal feedback/control alive; an individual blocking operation
            # still trips the original 150ms gate before another target sends.
            control.check()
            if (control.disable_requested.is_set() or session.enabled and (
                    session.last_tick is None or time.monotonic() - session.last_tick >= 1 / CONTROL_HZ)):
                session.tick()

        while True:
            control.check()
            if time.monotonic() - began >= SESSION_LIMIT_S:
                raise StopRequested("控制会话达到1小时上限")
            if time.monotonic() - last_process_check >= 1.0:
                check_other_writers(check_cancel=service_process_scan)
                last_process_check = time.monotonic()
                next_tick = last_process_check
            if control.disable_requested.is_set():
                session.tick()
            try:
                command = control.commands.get_nowait()
            except queue.Empty:
                command = None
            if command:
                if command["command"] == "enable":
                    check_other_writers()
                    if reader.load_configuration(dof)[1] != digest:
                        raise ValueError("控制会话中配置发生改变")
                try:
                    session.handle(command)
                except StartPoseRejected as error:
                    # Only this no-motion precondition is recoverable. Receive,
                    # mapping, timing and in-motion failures still reach cleanup.
                    emit({"type": "command_rejected", "command": error.command,
                          "code": error.code,
                          "state": "prepared" if session.preparation is not None else "enabled",
                          "rows": error.rows, "tolerance_deg": error.tolerance_deg,
                          "message": str(error)[:500]})
                next_tick = time.monotonic()
            session.tick()
            next_tick += 1 / CONTROL_HZ
            time.sleep(max(0.0, next_tick - time.monotonic()))
    except StopRequested as error:
        reason = str(error)
        if session and session.manual_engine is not None:
            session.manual_engine.interrupt(reason)
        if session and session.check_engine is not None:
            session.check_engine.phase, session.check_engine.abort_reason = "aborted", reason
    except BaseException as error:
        reason = str(error) or "控制会话异常"
        if session and session.manual_engine is not None:
            session.manual_engine.interrupt(reason)
        if session and session.check_engine is not None:
            session.check_engine.phase, session.check_engine.abort_reason = "aborted", reason
        try:
            emit({"type": "error", "message": "".join(c if c.isprintable() else " " for c in reason)[:300]})
        except (OSError, ValueError):
            pass
    finally:
        disable_sent = False
        if session:
            try:
                if session.enabled or session.driver.motors:
                    session.disable()
                disable_sent = session.disable_sent
            except BaseException:
                # Cleanup must keep trying every motor even if stdout closed.
                disable_sent = session.driver.disable()
                session.driver.close()
        if lock is not None:
            os.close(lock)
        try:
            emit({"type": "complete", "reason": reason, "disable_sent": disable_sent,
                  "hardware_stop_verified": False})
        except (OSError, ValueError):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dof", type=int, choices=(24, 28), required=True)
    parser.add_argument("--session-id", type=str, required=True)
    args = parser.parse_args(argv)
    # A catchable termination requests cleanup; SIGKILL/power loss cannot do so.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(StopRequested("会话被终止")))
    run_session(args.dof, args.session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
