"""Bounded, standalone LRO position queries after an independently run zeroing.

Importing this file does nothing. Only E0 01 queries are sent; no SDK, ROS,
enable, disable, zeroing or control fallback is used, including during cleanup.
There is no shared writer lock or protocol transaction ID: process checks and
kernel receive windows cannot eliminate external writers or correlate arbitrary
late responses. Raw output-shaft degrees are not a mechanical-limit validation.
"""

import argparse
import hashlib
import json
import math
import os
import select
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = "/home/robo/roboparty_deploy"
CONFIGURATIONS = {
    24: (ROOT + "/scripts/config/set_zero_rp1_24dof.yaml",
         "81f9a92fa9cf0e91273fbb5817acea2566a298fe8ee06eb5ef6c0e1fd8c5a9a5"),
    28: (ROOT + "/scripts/config/set_zero_rp1.yaml",
         "a91ef44a2d83083765802f75cfa5af83708ada9f9c5dccc356e6c7122dff877f"),
}
CAN_MTU, CANFD_MTU = 16, 72
CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG, CAN_SFF_MASK = 0x80000000, 0x40000000, 0x20000000, 0x7FF
CAN_RAW_FILTER, CAN_RAW_RECV_OWN_MSGS, CAN_RAW_FD_FRAMES = 1, 4, 5
CANFD_BRS = 1
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
FRAME = struct.Struct("=IBBBB64s")
TIMESPEC = struct.Struct("@ll")
QUERY_TIMEOUT_NS = 250_000_000
SESSION_LIMIT_NS = 30_000_000_000


def load_configuration(dof):
    if type(dof) is not int or dof not in CONFIGURATIONS:
        raise ValueError("仅支持24或28电机配置")
    filename, expected = CONFIGURATIONS[dof]
    path = Path(filename)
    if not path.is_file() or str(path.resolve()) != filename:
        raise ValueError("配置文件路径与审查版本不符")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("标零配置SHA256与审查版本不符")
    try:
        import yaml  # Runtime data parser only; no project or motor modules.

        config = yaml.safe_load(raw)
    except Exception as error:
        raise ValueError("无法读取已审查的YAML配置") from error
    if not isinstance(config, dict):
        raise ValueError("标零配置必须为映射")
    interfaces, counts = config.get("motor_interface"), config.get("motor_num")
    expected_counts = [7, 7, 5, 5] if dof == 24 else [7, 7, 7, 7]
    if (not isinstance(interfaces, list) or len(interfaces) != 4
            or any(not isinstance(name, str) for name in interfaces)
            or set(interfaces) != {"can0", "can1", "can2", "can3"}
            or counts != expected_counts or any(type(count) is not int for count in counts)
            or config.get("motor_interface_type") != ["canfd"] * 4
            or config.get("motor_type") != ["LRO"] * 4
            or type(config.get("master_id_offset")) is not int):
        raise ValueError("配置不是已审查的四路CANFD LRO总线布局")
    ids, models, offsets = (config.get(key) for key in (
        "motor_id", "motor_model", "motor_zero_offset",
    ))
    if any(not isinstance(items, list) or len(items) != dof for items in (ids, models, offsets)):
        raise ValueError("电机配置数量不完整")
    if (any(type(identity) is not int or not 1 <= identity <= 8 for identity in ids)
            or any(type(model) is not int or not 0 <= model <= 3 for model in models)
            or any(type(offset) not in (int, float) or not math.isfinite(offset) for offset in offsets)):
        raise ValueError("电机编号、型号或软件偏移无效")
    motors, index = [], 0
    for interface, count in zip(interfaces, counts):
        if len(set(ids[index:index + count])) != count:
            raise ValueError("同一CAN总线上电机编号重复")
        for _ in range(count):
            motors.append(dict(index=index, interface=interface, motor_id=ids[index],
                               model=models[index], software_offset_rad=offsets[index]))
            index += 1
    return motors, expected


def process_conflict(executable, arguments):
    names = {Path(executable.removesuffix(" (deleted)")).name}
    names.update(Path(argument).name for argument in arguments)
    if names & {"inference_node", "set_zero.py", "read_joint_angles.py"}:
        return True
    return any(arguments[index:index + 2] == ["launch", "roboparty_inference"]
               for index in range(len(arguments) - 1))


def check_processes(proc=Path("/proc"), check_cancel=lambda: None):
    for entry in proc.iterdir():
        check_cancel()
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            if not raw:
                continue
            arguments = [argument.decode("utf-8", "replace") for argument in raw.split(b"\0") if argument]
            executable = os.readlink(entry / "exe")
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError("无法确认进程状态，拒绝查询：PID " + entry.name) from error
        if process_conflict(executable, arguments):
            raise ValueError("推理、标零或其他电机工具仍在运行：PID " + entry.name)


def restrict_affinity():
    selected = set(os.sched_getaffinity(0)) & {1, 2, 3, 7}
    if not selected:
        raise ValueError("当前CPU限制没有允许的非CPU0查询核")
    os.sched_setaffinity(0, selected)
    if set(os.sched_getaffinity(0)) != selected:
        raise ValueError("无法确认查询进程CPU亲和性")
    return sorted(selected)


def open_can(interface, motor_ids):
    connection = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        connection.setblocking(False)
        # CAN_ERR_FLAG in a filter mask selects Linux's error-frame receive list;
        # it does not exclude error frames. Match ordinary standard data frames.
        mask = CAN_SFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
        connection.setsockopt(socket.SOL_CAN_RAW, CAN_RAW_FILTER,
                              b"".join(struct.pack("=II", identity, mask) for identity in motor_ids))
        connection.setsockopt(socket.SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1)
        connection.setsockopt(socket.SOL_CAN_RAW, CAN_RAW_RECV_OWN_MSGS, 0)
        connection.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        connection.bind((interface,))
    except BaseException:
        connection.close()
        raise
    return connection


def position_query(motor_id):
    if type(motor_id) is not int or not 1 <= motor_id <= 8:
        raise ValueError("查询电机编号无效")
    return FRAME.pack(motor_id, 2, CANFD_BRS, 0, 0, b"\xe0\x01")


def kernel_timestamp(ancillary):
    stamps = []
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS:
            if len(value) < TIMESPEC.size:
                return None
            seconds, nanoseconds = TIMESPEC.unpack_from(value)
            if seconds <= 0 or not 0 <= nanoseconds < 1_000_000_000:
                return None
            stamps.append(seconds * 1_000_000_000 + nanoseconds)
    return stamps[0] if len(stamps) == 1 else None


def decode_response(packet, ancillary, flags, *, motor_id, sent_realtime_ns,
                    sent_monotonic_ns, now_realtime_ns, now_monotonic_ns):
    result = dict(status="invalid", angle_deg=None, received_at=None, rx_monotonic_ns=None,
                  raw_hex=packet.hex(), fault=None, kernel_rx_timestamp=False)
    if len(packet) not in (CAN_MTU, CANFD_MTU):
        return result
    can_id, length = struct.unpack_from("=IB", packet)
    payload = packet[8:8 + min(length, 64)]
    result["raw_hex"] = payload.hex()
    if (flags & (socket.MSG_DONTROUTE | socket.MSG_TRUNC | socket.MSG_CTRUNC | socket.MSG_CONFIRM)
            or can_id != motor_id or length != 6
            or len(payload) != 6 or payload[0] >> 5 != 5 or payload[1] != 1):
        return result
    result["fault"] = payload[0] & 0x1F  # Type5 documented status bits; no Type1 firmware assumption.
    stamp = kernel_timestamp(ancillary)
    if (stamp is None or not sent_realtime_ns < stamp <= now_realtime_ns
            or now_realtime_ns - stamp > QUERY_TIMEOUT_NS
            or not 0 <= now_monotonic_ns - sent_monotonic_ns <= QUERY_TIMEOUT_NS):
        return result
    received_mono = now_monotonic_ns - (now_realtime_ns - stamp)
    if received_mono < sent_monotonic_ns:
        return result
    seconds, nanoseconds = divmod(stamp, 1_000_000_000)
    result.update(
        received_at=datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + f".{nanoseconds:09d}Z",
        rx_monotonic_ns=received_mono, kernel_rx_timestamp=True,
    )
    angle = struct.unpack(">f", payload[2:6])[0]
    if result["fault"] == 0 and math.isfinite(angle):
        result.update(status="fresh", angle_deg=angle)
    return result


class Cancelled(Exception):
    pass


class SessionControl:
    def __init__(self, stdin, started_ns):
        self.fd = stdin.fileno()
        self.deadline = started_ns + SESSION_LIMIT_NS
        self.buffer = b""

    def check(self):
        if time.monotonic_ns() >= self.deadline:
            raise ValueError("角度复查已达到30秒会话上限")
        if select.select([self.fd], [], [], 0)[0]:
            data = os.read(self.fd, 1024)
            if not data:
                raise Cancelled()
            self.buffer += data
            if self.buffer.strip() == b"STOP":
                raise Cancelled()
            if len(self.buffer) > 1024 or b"\n" in self.buffer:
                raise ValueError("角度复查控制输入无效")

    def wait(self, readers, timeout):
        self.check()
        remaining = (self.deadline - time.monotonic_ns()) / 1_000_000_000
        ready = select.select([self.fd, *readers], [], [], max(0, min(timeout, remaining, 0.05)))[0]
        if self.fd in ready:
            self.check()
        self.check()
        return [reader for reader in ready if reader != self.fd]

    def pause(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.wait([], deadline - time.monotonic())


class WriterGuard:
    def __init__(self, control):
        self.control, self.checked_ns = control, None

    def check(self, force=False):
        self.control.check()
        now = time.monotonic_ns()
        if force or self.checked_ns is None or now - self.checked_ns >= 500_000_000:
            check_processes(check_cancel=self.control.check)
            self.checked_ns = time.monotonic_ns()


def query_once(connection, motor_id, control, guard):
    # Discard already queued frames, without sending a probe or retry.
    for _ in range(256):
        control.check()
        try:
            connection.recvmsg(CANFD_MTU, socket.CMSG_SPACE(TIMESPEC.size))
        except BlockingIOError:
            break
    else:
        raise ValueError("CAN接收队列持续繁忙，拒绝增加查询")
    guard.check()
    sent_mono, sent_real = time.monotonic_ns(), time.time_ns()
    request = position_query(motor_id)
    if connection.send(request) != len(request):
        raise ValueError("位置查询未完整发送，不会重试")
    invalid = None
    while time.monotonic_ns() - sent_mono < QUERY_TIMEOUT_NS:
        guard.check()
        remaining = (QUERY_TIMEOUT_NS - (time.monotonic_ns() - sent_mono)) / 1_000_000_000
        if not control.wait([connection], remaining):
            continue
        try:
            packet, ancillary, flags, _ = connection.recvmsg(CANFD_MTU, socket.CMSG_SPACE(TIMESPEC.size))
        except BlockingIOError:
            continue
        sample = decode_response(
            packet, ancillary, flags, motor_id=motor_id, sent_realtime_ns=sent_real,
            sent_monotonic_ns=sent_mono, now_realtime_ns=time.time_ns(),
            now_monotonic_ns=time.monotonic_ns(),
        )
        if sample["status"] == "fresh":
            sample["query_sent_monotonic_ns"] = sent_mono
            return sample
        invalid = sample
    sample = invalid or dict(status="timeout", angle_deg=None, received_at=None,
                             rx_monotonic_ns=None, raw_hex=None, fault=None,
                             kernel_rx_timestamp=False)
    sample["query_sent_monotonic_ns"] = sent_mono
    return sample


def emit(output, event):
    output.write(json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n")
    output.flush()


def run_session(dof, session_id, *, rounds=3, stdin=None, output=None):
    session_id = str(UUID(str(session_id)))
    if type(rounds) is not int or not 1 <= rounds <= 3:
        raise ValueError("复查轮数只支持1至3")
    stdin, output = stdin or sys.stdin, output or sys.stdout
    started_ns, connections = time.monotonic_ns(), {}
    cancelled, failed = False, False
    try:
        control = SessionControl(stdin, started_ns)
        control.check()
        affinity = restrict_affinity()
        motors, digest = load_configuration(dof)
        guard = WriterGuard(control)
        guard.check(force=True)
        for interface in dict.fromkeys(motor["interface"] for motor in motors):
            control.check()
            connections[interface] = open_can(interface, [
                motor["motor_id"] for motor in motors if motor["interface"] == interface
            ])
        emit(output, dict(
            type="metadata", schema="rp1.postzero.v1", session_id=session_id, dof=dof,
            motors=motors, configuration_sha256=digest, position_basis="motor_output_raw_deg",
            source_kind="lro_position_query", affinity=affinity,
            timestamp_basis="kernel_so_timestampns", started_monotonic_ns=started_ns,
            protocol_evidence="local_v1.02_and_upstream_query", firmware109_verified=False,
        ))
        for round_number in range(1, rounds + 1):
            previous_interface = None
            for motor in motors:
                interface = motor["interface"]
                guard.check(force=interface != previous_interface)
                previous_interface = interface
                sample = query_once(connections[interface], motor["motor_id"], control, guard)
                emit(output, dict(type="sample", session_id=session_id, index=motor["index"],
                                  round=round_number, **sample))
            if round_number < rounds:
                control.pause(1)
    except Cancelled:
        cancelled = True
    except Exception as error:
        cancelled, failed = True, True
        message = "".join(character if character.isprintable() else " " for character in str(error))[:240]
        emit(output, dict(type="error", session_id=session_id,
                          message=message or "角度复查失败"))
    finally:
        for connection in connections.values():
            try:
                connection.close()
            except OSError:
                pass  # Closing another socket never sends a cleanup command.
        emit(output, dict(type="complete", session_id=session_id, cancelled=cancelled,
                          finished_monotonic_ns=time.monotonic_ns()))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded LRO E0 01 position queries only")
    parser.add_argument("--dof", type=int, choices=(24, 28), required=True)
    parser.add_argument("--session-id", type=UUID, required=True)
    parser.add_argument("--rounds", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args(argv)
    return run_session(args.dof, args.session_id, rounds=args.rounds)


if __name__ == "__main__":
    raise SystemExit(main())
