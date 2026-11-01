"""Observe existing LRO Type1 feedback without requesting or controlling motors.

Motor processes may keep running. No process is started, stopped or excluded.
The 12.5-radian scale comes from reviewed local source, remains unverified on the
device, and cannot establish mechanical zero or a safe operating range.
"""

import argparse
import math
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from uuid import UUID

try:
    from . import postzero_reader as reader
except ImportError:  # Standalone/embedded helper with its support module supplied.
    import postzero_reader as reader

POSITION_SCALE_RAD = 12.5
ROUND_WINDOW_NS = 1_000_000_000
SESSION_LIMIT_NS = 10_000_000_000
DRAIN_LIMIT = 256
ROUND_RECEIVE_LIMIT = 4096
SAMPLE_FIELDS = (
    "status", "angle_deg", "received_at", "rx_monotonic_ns", "raw_hex", "fault",
    "kernel_rx_timestamp", "enabled",
)


def decode_type1(packet, ancillary, flags, *, motor_id, window_realtime_ns,
                 window_monotonic_ns, now_realtime_ns, now_monotonic_ns,
                 previous_rx_monotonic_ns=0):
    """Decode one receive observation; raw speed/Iq have no asserted unit scale."""
    result = dict(status="invalid", angle_deg=None, received_at=None, rx_monotonic_ns=None,
                  raw_hex=packet[:64].hex(), fault=None, kernel_rx_timestamp=False, enabled=None,
                  position_raw=None, speed_raw=None, iq_raw=None,
                  motor_temperature=None, mos_temperature=None)
    if len(packet) not in (reader.CAN_MTU, reader.CANFD_MTU):
        return result
    can_id, length = struct.unpack_from("=IB", packet)
    payload = packet[8:8 + min(length, 64)]
    result["raw_hex"] = payload.hex()
    if (flags & (socket.MSG_DONTROUTE | socket.MSG_TRUNC | socket.MSG_CTRUNC | socket.MSG_CONFIRM)
            or can_id != motor_id or length not in (8, 12) or len(payload) != length
            or len(packet) == reader.CAN_MTU and length > 8 or payload[0] >> 5 != 1):
        return result
    position_raw = (payload[1] << 8) | payload[2]
    result.update(
        fault=payload[0] & 0x07, enabled=bool(payload[0] & 0x10), position_raw=position_raw,
        speed_raw=(payload[3] << 4) | (payload[4] >> 4),
        iq_raw=((payload[4] & 0x0F) << 8) | payload[5],
        motor_temperature=payload[6] - 25, mos_temperature=payload[7] - 25,
    )
    stamp = reader.kernel_timestamp(ancillary)
    if (stamp is None or not window_realtime_ns < stamp <= now_realtime_ns
            or now_realtime_ns - stamp > ROUND_WINDOW_NS
            or not 0 <= now_monotonic_ns - window_monotonic_ns <= ROUND_WINDOW_NS):
        return result
    received_mono = now_monotonic_ns - (now_realtime_ns - stamp)
    if received_mono < window_monotonic_ns or received_mono <= previous_rx_monotonic_ns:
        return result
    seconds, nanoseconds = divmod(stamp, 1_000_000_000)
    result.update(
        received_at=datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + f".{nanoseconds:09d}Z", rx_monotonic_ns=received_mono, kernel_rx_timestamp=True,
    )
    if result["fault"] == 0 and not payload[0] & 0x08:
        result.update(status="fresh", angle_deg=math.degrees(((position_raw / 65535.0) * 25.0) - 12.5))
    return result


class PassiveControl(reader.SessionControl):
    def __init__(self, stdin, started_ns):
        super().__init__(stdin, started_ns)
        self.deadline = started_ns + SESSION_LIMIT_NS

    def check(self):
        if time.monotonic_ns() >= self.deadline:
            raise ValueError("被动角度观测已达到10秒会话上限")
        super().check()


def collect_round(connections, motors, control, previous_rx):
    for connection in connections.values():
        for _ in range(DRAIN_LIMIT):
            control.check()
            try:
                connection.recvmsg(reader.CANFD_MTU, socket.CMSG_SPACE(reader.TIMESPEC.size))
            except BlockingIOError:
                break
    window_mono, window_real = time.monotonic_ns(), time.time_ns()
    addresses = {(motor["interface"], motor["motor_id"]): motor["index"] for motor in motors}
    interfaces = {connection: interface for interface, connection in connections.items()}
    samples, fresh, received = {}, set(), 0
    while (time.monotonic_ns() - window_mono < ROUND_WINDOW_NS
           and len(fresh) < len(motors) and received < ROUND_RECEIVE_LIMIT):
        control.check()
        remaining = (ROUND_WINDOW_NS - (time.monotonic_ns() - window_mono)) / 1_000_000_000
        for connection in control.wait(list(interfaces), remaining):
            if received >= ROUND_RECEIVE_LIMIT:
                break
            control.check()
            try:
                packet, ancillary, flags, _ = connection.recvmsg(
                    reader.CANFD_MTU, socket.CMSG_SPACE(reader.TIMESPEC.size),
                )
            except BlockingIOError:
                continue
            received += 1
            if len(packet) < 4:
                continue
            can_id = struct.unpack_from("=I", packet)[0]
            identity = can_id & reader.CAN_SFF_MASK
            index = addresses.get((interfaces[connection], identity))
            if index is None or index in fresh:
                continue
            sample = decode_type1(
                packet, ancillary, flags, motor_id=identity, window_realtime_ns=window_real,
                window_monotonic_ns=window_mono, now_realtime_ns=time.time_ns(),
                now_monotonic_ns=time.monotonic_ns(), previous_rx_monotonic_ns=previous_rx.get(index, 0),
            )
            samples[index] = sample
            if sample["status"] == "fresh":
                fresh.add(index)
                previous_rx[index] = sample["rx_monotonic_ns"]
    result = []
    for motor in motors:
        sample = samples.get(motor["index"]) or dict(
            status="timeout", angle_deg=None, received_at=None, rx_monotonic_ns=None,
            raw_hex=None, fault=None, kernel_rx_timestamp=False, enabled=None,
        )
        result.append({key: sample[key] for key in SAMPLE_FIELDS})
    return window_mono, result


def run_session(dof, session_id, *, stdin=None, output=None):
    session_id = str(UUID(str(session_id)))
    stdin, output = stdin or sys.stdin, output or sys.stdout
    started_ns, connections = time.monotonic_ns(), {}
    cancelled, failed = False, False
    try:
        control = PassiveControl(stdin, started_ns)
        control.check()
        affinity = reader.restrict_affinity()
        motors, digest = reader.load_configuration(dof)
        for interface in dict.fromkeys(motor["interface"] for motor in motors):
            control.check()
            connections[interface] = reader.open_can(interface, [
                motor["motor_id"] for motor in motors if motor["interface"] == interface
            ])
            connections[interface].setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
        reader.emit(output, dict(
            type="metadata", schema="rp1.postzero.v1", session_id=session_id, dof=dof,
            motors=motors, configuration_sha256=digest, position_basis="motor_output_raw_deg",
            source_kind="lro_type1_passive", mode="passive", affinity=affinity,
            timestamp_basis="kernel_so_timestampns", started_monotonic_ns=started_ns,
            protocol_evidence="local_deploy_type1_firmware109_layout", firmware109_verified=False,
            position_scale_rad=POSITION_SCALE_RAD, position_scale_verified=False,
        ))
        previous_rx = {}
        for round_number in range(1, 4):
            window_start, samples = collect_round(connections, motors, control, previous_rx)
            for motor, sample in zip(motors, samples):
                reader.emit(output, dict(
                    type="sample", session_id=session_id, index=motor["index"], round=round_number,
                    query_sent_monotonic_ns=window_start, **sample,
                ))
            if round_number < 3:
                control.pause(1)
    except reader.Cancelled:
        cancelled = True
    except Exception as error:
        cancelled, failed = True, True
        message = "".join(character if character.isprintable() else " " for character in str(error))[:240]
        reader.emit(output, dict(type="error", session_id=session_id,
                                 message=message or "被动角度观测失败"))
    finally:
        for connection in connections.values():
            try:
                connection.close()
            except OSError:
                pass
        reader.emit(output, dict(type="complete", session_id=session_id, cancelled=cancelled,
                                 finished_monotonic_ns=time.monotonic_ns()))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Listen to existing LRO Type1 feedback only")
    parser.add_argument("--dof", type=int, choices=(24, 28), required=True)
    parser.add_argument("--sid", "--session-id", dest="session_id", type=UUID, required=True)
    args = parser.parse_args(argv)
    return run_session(args.dof, args.session_id)


if __name__ == "__main__":
    raise SystemExit(main())
