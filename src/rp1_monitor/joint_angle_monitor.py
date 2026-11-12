"""Native read-only joint-angle stream from the disabled LRO encoders."""

import argparse
import math
import os
import re
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from uuid import UUID

try:
    from . import calibration_mapping as mapping
    from . import postzero_reader as reader
except ImportError:  # Standalone SSH helper loads the two modules in memory.
    import calibration_mapping as mapping
    import postzero_reader as reader

REALTIME_LOCK = "/run/roboparty-realtime.lock"
SESSION_LIMIT_NS = 30 * 60 * 1_000_000_000
QUERY_INTERVAL_NS = 35_000_000
ANKLE_SKEW_NS = 100_000_000
RAW_FLOAT32_MAX = 3.4028234663852886e38
RAW_UTC_PATTERN = re.compile(r"[0-9]{4}(?:-[0-9]{2}){2}T[0-9]{2}(?::[0-9]{2}){2}\.[0-9]{9}Z")
OTHER_WRITERS = {"start_robot.sh", "realtime_run.py", "deploy_motorlro_params.py",
                 "deploy_motorlro_firmware.py", "025-lro_motor_parameters.py", "motor_ota.py",
                 "motors_py_example.py", "postzero_reader.py", "cansend", "cangen"}


def valid_raw_utc(value):
    if not isinstance(value, str) or not RAW_UTC_PATTERN.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _other_writer(executable, arguments, session_id):
    names = {Path(executable.removesuffix(" (deleted)")).name, *(Path(arg).name for arg in arguments)}
    if names & OTHER_WRITERS or any(name.startswith("factory_hmi") for name in names):
        return True
    other_session = any(value == "--session-id" and arguments[index + 1] != session_id
                        for index, value in enumerate(arguments[:-1]))
    # Old inline active capture builds did not take the motor lock. The
    # separately wrapped passive listener may still run because it never sends.
    return other_session and any("Bounded, standalone LRO position queries" in arg
                                 and "postzero_passive_capture" not in arg for arg in arguments)


def _sweep(motors, connections, control, guard, next_query_ns):
    samples = {}
    for motor in motors:
        control.check()
        connection = connections.get(motor["interface"])
        if connection is None:
            continue
        delay_ns = next_query_ns - time.monotonic_ns()
        if delay_ns > 0:
            control.pause(delay_ns / 1_000_000_000)
        try:
            sample = reader.query_once(connection, motor["motor_id"], control, guard)
        except OSError:
            # A socket failure on one axis does not supply any angle evidence.
            next_query_ns = time.monotonic_ns() + QUERY_INTERVAL_NS
            continue
        next_query_ns = sample["query_sent_monotonic_ns"] + QUERY_INTERVAL_NS
        if sample["status"] == "fresh":
            samples[motor["index"]] = sample
    return samples, next_query_ns


def _logical_positions(binding, samples, previous_ankles):
    angles = {index: sample["angle_deg"] for index, sample in samples.items()}
    stamps = {index: sample["rx_monotonic_ns"] for index, sample in samples.items()}
    positions, received = [], []
    ankles = {}
    for row in binding["rows"]:
        indices, side = row["motor_indices"], row["coupled_side"]
        position = stamp = None
        if all(index in stamps for index in indices):
            times = [stamps[index] for index in indices]
            if max(times) - min(times) <= ANKLE_SKEW_NS:
                try:
                    position = math.degrees(mapping.encoder_joint_angle(
                        binding, row["name"], angles, previous=previous_ankles.get(side)))
                    stamp = min(times)
                except ValueError:
                    pass
        positions.append(position)
        received.append(stamp)
        if side is not None:
            ankles.setdefault(side, []).append((row["name"], len(positions) - 1))
    for side, rows in ankles.items():
        if any(positions[index] is None for _, index in rows):
            for _, index in rows:
                positions[index] = received[index] = None
            previous_ankles.pop(side, None)
        else:
            previous_ankles[side] = tuple(math.radians(positions[index]) for _, index in sorted(rows))
    return positions, received


def run_session(dof, session_id, *, stdin=None, output=None):
    session_id = str(UUID(str(session_id)))
    stdin, output = stdin or sys.stdin, output or sys.stdout
    started_ns = time.monotonic_ns()
    deadline = started_ns + SESSION_LIMIT_NS
    reason, cancelled, failed = "采样达到30分钟上限", False, False

    def emit(kind, **event):
        reader.emit(output, {"type": kind, "session_id": session_id, **event})

    try:
        if os.geteuid() != 0:
            raise ValueError("只读位置查询需要本次sudo认证")
        control = reader.SessionControl(stdin, started_ns)
        control.deadline = deadline
        control.check()
        reader.restrict_affinity()
        with ExitStack() as resources:
            for path in (reader.MOTOR_CONTROL_LOCK, REALTIME_LOCK):
                try:
                    resources.callback(os.close, reader.acquire_motor_lock(path))
                except BlockingIOError:
                    raise ValueError("推理或其他电机工具正在使用机器人，请先按原流程停止") from None
            guard = reader.WriterGuard(control, extra_conflict=lambda executable, args: _other_writer(
                executable, args, session_id))
            guard.check(force=True)
            motors, digest = reader.load_configuration(dof)
            binding = mapping.encoder_observation_mapping(dof, motors, reader.ROOT)
            connections = {}
            for interface in dict.fromkeys(motor["interface"] for motor in motors):
                control.check()
                try:
                    connections[interface] = resources.enter_context(reader.open_can(interface, [
                        motor["motor_id"] for motor in motors if motor["interface"] == interface
                    ]))
                except OSError:
                    continue
            if not connections:
                raise ValueError("无法打开任何已核对的CAN总线")
            emit("metadata", dof=dof, joint_names=binding["joint_names"], configuration_sha256=digest)
            sweep_number, previous_ankles, next_query_ns = 0, {}, 0
            while time.monotonic_ns() < deadline:
                sweep_started = time.monotonic_ns()
                if reader.load_configuration(dof) != (motors, digest) or (
                    mapping.encoder_observation_mapping(dof, motors, reader.ROOT) != binding
                ):
                    raise ValueError("采样期间机器人配置或角度映射已改变，请重新连接核对")
                samples, next_query_ns = _sweep(motors, connections, control, guard, next_query_ns)
                positions, received = _logical_positions(binding, samples, previous_ankles)
                control.check()
                finished = time.monotonic_ns()
                sweep_number += 1
                raw = [samples.get(motor["index"], {}) for motor in motors]
                emit("frame", sequence=sweep_number, positions_deg=positions, rx_monotonic_ns=received,
                     frame_monotonic_ns=finished, sweep_s=(finished - sweep_started) / 1_000_000_000,
                     raw_angles_deg=[sample.get("angle_deg") for sample in raw],
                     raw_rx_monotonic_ns=[sample.get("rx_monotonic_ns") for sample in raw],
                     raw_received_at=[sample.get("received_at") for sample in raw])
                # Bound frame rate when some or all interfaces are unavailable.
                control.pause(max(0, 0.8 - (time.monotonic_ns() - sweep_started) / 1_000_000_000))
    except reader.Cancelled:
        reason, cancelled = "角度采样已停止", True
    except (OSError, ValueError) as error:
        if time.monotonic_ns() < deadline:
            failed, reason = True, "".join(char if char.isprintable() else " " for char in str(error))[:240]
            emit("error", message=reason or "只读角度采样失败")
    emit("complete", reason=reason or "只读角度采样失败", cancelled=cancelled)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Continuous read-only LRO encoder angles")
    parser.add_argument("--dof", type=int, choices=(24, 28), required=True)
    parser.add_argument("--session-id", type=UUID, required=True)
    args = parser.parse_args(argv)
    return run_session(args.dof, args.session_id)


if __name__ == "__main__":
    raise SystemExit(main())
