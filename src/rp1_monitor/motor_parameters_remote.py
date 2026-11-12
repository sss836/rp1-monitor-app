"""Native maintenance helper, transferred in memory; never loaded by the observer.

The versioned LRO profiles define the only accepted parameter writes.
Enable, zero, motion, flash, ID and arbitrary registers are outside this helper.
"""

import hashlib
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path("/home/robo/roboparty_deploy")
# Official LRO table pinned to the motors submodule of deploy main 95be350.
PARAMETER_STANDARD = "roboparty_motors@a5c930a06a1648c2bc15c2c83ad0512ee9185258"
LIMITS = [(12.5, 20., 30., 18., 500., 50.), (12.5, 40., 40., 32., 500., 50.),
          (12.5, 35., 65., 35., 500., 50.), (12.5, 16., 165., 90., 500., 50.)]
# Recognize the older runtime for read-only checks against the official table.
LIMITS_80 = [(12.5, 21., 36., 500., 50.), (12.5, 21.89, 35.3, 500., 50.),
             (12.5, 16.13, 56.5, 500., 50.), (12.5, 14.24, 141.7, 500., 50.)]
DRIVER_80 = {
    "lro_motor_driver.cpp": "162a94c4e93350fc267fb10b06c575a043ef4da5176b2ca44c1499f8ca80ec18",
    "lro_motor_driver.hpp": "3b757aa3efcfc0bec036d518b3f5fa29eed440be5a2491582cb967d57299c995",
}
PARAMETER_WRITE_BLOCKED = "已安装驱动量程与官网基准不一致。请更新并编译机器人驱动后重新检查；当前仅可读取。"
PARAMS = {"KP": (0x17, 0x05, 1, False, ""), "KD": (0x18, 0x06, 1, False, ""),
          "POS": (0x19, 0x07, 100, True, "rad"), "SPD": (0x1A, 0x08, 100, True, "rad/s"),
          "TOR": (0x1B, 0x09, 10, True, "N·m")}
# Target values from deploy_motorlro_params (3).py, reviewed 2026-11-11.
EXTENDED_STANDARD = "LRO参数脚本(3) · 2026-11-11"
KT_BY_MODEL = (1.74, 1.62, 2.31, 2.38)
COMMON_PN = ((0x0401, 200), (0x0402, 300), (0x0403, 300), (0x0E71, 75))
MODEL_PN = {
    0: (),
    1: ((0x0407, 10000), (0x0E05, 56), (0x0EB2, 45), (0x0F09, 65), (0x0F0A, 287)),
    2: ((0x0407, 10000), (0x0E05, 56), (0x0EB2, 39)),
    3: ((0x0407, 5000), (0x0E05, 56), (0x0EB2, 40)),
}


def extended_definitions(model):
    kt, current = KT_BY_MODEL[model], LIMITS[model][3]
    # Request, approved write frame, response encoding, scale, unit, expected.
    fields = {
        "Kt": (b"\xe0\x16", b"\xc0\x04" + struct.pack(">H", round(kt * 100)), ">H", 100, "", kt),
        "CUR": (b"\xe0\x1c", b"\xc0\x0a" + struct.pack(">hh", round(-current * 10), round(current * 10)),
                ">hh", 10, "A", [-current, current]),
        "CAN_TIMEOUT": (b"\xe0\x1f", b"\xc0\x0b\x00\x00", ">H", 1, "ms", 0),
    }
    for number, value in COMMON_PN + MODEL_PN[model]:
        fields[f"Pn{number:03X}"] = (b"\xc0\x11" + struct.pack(">H", number),
                                   b"\xc0\x10" + struct.pack(">HI", number, value), ">I", 1,
                                   "V" if number == 0x0E71 else "", value)
    return fields


def parameter_write_items(row, *, rewrite=False):
    if row["status"] == "unreadable":
        return []
    return [(key, reading) for group in (row["parameters"], row.get("extended_parameters", {}))
            for key, reading in group.items() if reading.get("actual") is not None
            and (reading.get("matches") is False or (rewrite and reading.get("matches") is True))]


def parameter_differences(row):
    return parameter_write_items(row)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def validate_request(request):
    if set(request) != {"request_id", "robot_id", "dof", "operation", "expected_digest", "selected"}:
        raise ValueError("电机参数请求字段不完整或含未知字段")
    if str(UUID(request["request_id"])) != request["request_id"]:
        raise ValueError("请求编号无效")
    if type(request["dof"]) is not int or request["dof"] not in (24, 28):
        raise ValueError("仅支持24或28轴")
    if (request["operation"] not in ("check", "update", "rewrite") or not isinstance(request["robot_id"], str)
            or not 1 <= len(request["robot_id"]) <= 64):
        raise ValueError("参数操作无效")
    selected = request["selected"]
    if (not isinstance(selected, list) or len(selected) > 28 or any(
            not isinstance(value, str) or len(value) > 32 for value in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("所选电机无效")
    if request["operation"] == "check":
        if selected or request["expected_digest"]:
            raise ValueError("检查请求不能携带写入目标")
    elif (not selected or not isinstance(request["expected_digest"], str)
          or len(request["expected_digest"]) != 64):
        raise ValueError("更新前必须先检查并选择要更新的电机")
    return request


def compiled_limits(path, driver_dir=None):
    symbols = subprocess.run(["readelf", "-Ws", str(path)], capture_output=True,
                             text=True, timeout=8, check=True).stdout
    symbol = next((line.split() for line in symbols.splitlines()
                   if line.strip().endswith(" lro_limit_param")), None)
    if not symbol or int(symbol[2]) not in {80, 96}:
        raise ValueError("已安装驱动的参数表未识别，不能套用新版标准")
    size = int(symbol[2])
    if size == 80:
        if driver_dir is None or any(
            not (driver_dir / name).is_file() or hashlib.sha256((driver_dir / name).read_bytes()).hexdigest() != digest
            for name, digest in DRIVER_80.items()
        ):
            raise ValueError("已安装驱动的80字节参数表缺少匹配的已核对驱动源码")
        limits = LIMITS_80
    else:
        limits = LIMITS
    expected_table = struct.pack(f"<{size // 4}f", *(value for row in limits for value in row))
    address = int(symbol[1], 16)
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("已安装推理程序不是所支持的ELF格式")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phsize, phnum = struct.unpack_from("<HH", data, 54)
    for index in range(phnum):
        kind, _, offset, vaddr, _, filesz, _, _ = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phsize)
        if kind == 1 and vaddr <= address and address + size <= vaddr + filesz:
            start = offset + address - vaddr
            if data[start:start + size] != expected_table:
                raise ValueError("已安装驱动量程与本工具标准不同，请更新适配；未写入")
            return hashlib.sha256(data).hexdigest(), limits
    raise ValueError("无法定位已安装驱动的完整量程表")


def configuration(dof):
    import yaml

    robot = "rp1_24dof" if dof == 24 else "rp1"
    source = ROOT / "src/inference/robots" / robot / "robot.yaml"
    installed = ROOT / "install/roboparty_inference/share/roboparty_inference/robots" / robot / "robot.yaml"
    raw = source.read_bytes()
    if raw != installed.read_bytes():
        raise ValueError("源码与已安装机型配置不同，请先核对；未写入")
    motors = yaml.safe_load(raw)["motors"]
    counts = motors["motor_num"]
    if not isinstance(counts, list) or not counts or any(type(count) is not int or not 1 <= count <= 8 for count in counts):
        raise ValueError("电机总线数量无效")
    names = ("motor_interface", "motor_interface_type", "motor_type")
    if sum(counts) != dof or any(len(motors[name]) != len(counts) for name in names):
        raise ValueError("机型和电机总线数量不一致")
    if any(len(motors[name]) != dof for name in ("motor_id", "motor_model")):
        raise ValueError("电机ID或型号数量不完整")
    targets, offset = [], 0
    for bus, count in enumerate(counts):
        interface = motors["motor_interface"][bus]
        if (interface not in {"can0", "can1", "can2", "can3"}
                or motors["motor_interface_type"][bus] != "canfd"
                or motors["motor_type"][bus] != "LRO"):
            raise ValueError("仅适配原生四路LRO CAN-FD配置")
        for i in range(offset, offset + count):
            motor_id, model = motors["motor_id"][i], motors["motor_model"][i]
            if type(motor_id) is not int or not 1 <= motor_id <= 8 or type(model) is not int or not 0 <= model <= 3:
                raise ValueError("未知的电机ID或型号")
            targets.append(dict(address=f"{interface}/{motor_id}", interface=interface,
                                motor_id=motor_id, model=model))
        offset += count
    if len({row["address"] for row in targets}) != dof:
        raise ValueError("电机地址重复")
    executable = ROOT / "install/roboparty_inference/lib/roboparty_inference/inference_node"
    binary_hash, limits = compiled_limits(executable, ROOT / "src/motors/src/drivers/lro")
    return targets, hashlib.sha256(raw).hexdigest(), binary_hash, limits


def guard_processes():
    forbidden = {"inference_node", "start_robot.sh", "set_zero.py", "read_joint_angles.py",
                 "deploy_motorlro_params.py", "deploy_motorlro_firmware.py", "motor_ota.py",
                 "025-lro_motor_parameters.py"}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = [part.decode("utf8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
        except FileNotFoundError:
            continue
        names = {Path(arg).name for arg in args}
        found = names & forbidden
        if found or "--session-id" in args:
            tool = "、".join(sorted(found)) if found else "上位机电机控制会话"
            raise ValueError(f"已有电机工具运行：{tool}（PID {entry.name}），请按原流程停止后再检查参数")


@contextmanager
def maintenance_window(robot_id):
    import fcntl

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8891/api/v1/state", timeout=5) as response:
        identity = json.load(response)
    if identity.get("robot_id") != robot_id or identity.get("source_kind") != "ros2":
        raise ValueError("机器人身份或来源不匹配")
    if os.geteuid() != 0:
        raise ValueError("参数查询需要本次sudo认证")
    with open("/run/roboparty-realtime.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("deploy实时控制仍在运行，请先按原流程停止") from None
        guard_processes()
        yield


def exchange(target, payload, query):
    # All callers are below; no arbitrary payload is accepted from the desktop.
    pn_read = query == 0x11 and payload[:2] == b"\xc0\x11"
    if pn_read:
        model = target.get("model")
        if model not in MODEL_PN or payload not in {field[0] for field in extended_definitions(model).values()}:
            raise ValueError("拒绝非白名单Pn查询")
    elif payload[:1] == b"\xe0":
        if len(payload) != 2 or payload[1] != query or query not in {
            0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1E, 0x1F
        }:
            raise ValueError("拒绝非白名单查询")
    else:
        model = target.get("model")
        mit_write = len(payload) == 6 and payload[0] == 0xC0 and payload[1] in {5, 6, 7, 8, 9}
        if query is not None or (not mit_write and (
            model not in MODEL_PN or payload not in {field[1] for field in extended_definitions(model).values()}
        )):
            raise ValueError("拒绝非白名单参数写入")
    with socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as bus:
        bus.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FD_FRAMES, 1)
        bus.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                       # CAN_ERR_FLAG in can_mask selects the kernel error list,
                       # rather than excluding errors from a normal data filter.
                       struct.pack("=II", target["motor_id"], 0xC00007FF))
        bus.bind((target["interface"],))
        bus.send(struct.pack("=IBB2x64s", target["motor_id"], len(payload), 1, payload))
        if query is None:
            time.sleep(.1)
            return None
        deadline = time.monotonic() + .35
        while time.monotonic() < deadline:
            bus.settimeout(max(.001, deadline - time.monotonic()))
            try:
                raw = bus.recv(72)
            except socket.timeout:
                break
            can_id, length = struct.unpack_from("=IB", raw)
            data = raw[8:8 + length]
            if can_id != target["motor_id"] or len(data) < 2:
                continue
            if pn_read:
                # Pn responses carry no parameter number. Each request uses a fresh socket.
                if len(data) >= 7 and data[:3] == b"\xff\xfe\x11":
                    return data
                continue
            if data[0] >> 5 == 5 and data[1] == query:
                if data[0] & 7:
                    raise ValueError(f"电机返回错误码{data[0] & 7}")
                return data
    raise ValueError(f"{target['address']} 查询0x{query:02X}无响应")


def expected(model, key):
    pos, speed, torque, _, kp, kd = LIMITS[model]
    maximum = {"KP": kp, "KD": kd, "POS": pos, "SPD": speed, "TOR": torque}[key]
    return [0 if key in {"KP", "KD"} else -maximum, maximum]


def read_parameter(target, key, transact=exchange):
    code, _, scale, signed, unit = PARAMS[key]
    payload = transact(target, bytes([0xE0, code]), code)
    if len(payload) < 6:
        raise ValueError(f"{key}回复长度无效")
    values = struct.unpack_from(">hh" if signed else ">HH", payload, 2)
    actual = [value / scale for value in values]
    wanted = expected(target["model"], key)
    return dict(actual=actual, expected=wanted, matches=actual == wanted, unit=unit,
                raw_reply=payload.hex())


def read_extended_parameter(target, definition, transact=exchange):
    request, _, encoding, scale, unit, wanted = definition
    reading = dict(actual=None, expected=wanted, matches=None, unit=unit)
    try:
        reply = transact(target, request, request[1])
        reading["raw_reply"] = reply.hex()
        offset = 3 if request[1] == 0x11 else 2
        if len(reply) < offset + struct.calcsize(encoding):
            raise ValueError("回复长度无效")
        raw = struct.unpack_from(encoding, reply, offset)
        values = [number if scale == 1 else number / scale for number in raw]
        actual = values if len(values) > 1 else values[0]
        reading.update(actual=actual, matches=actual == wanted)
    except (OSError, ValueError) as error:
        reading["error"] = str(error)
    return reading


def read_extended_parameters(target, transact=exchange, *, timeout_reply=None):
    def receive(motor, payload, code):
        if code == 0x1F and timeout_reply is not None:
            return timeout_reply
        return transact(motor, payload, code)

    return {key: read_extended_parameter(target, definition, receive)
            for key, definition in extended_definitions(target["model"]).items()}


def read_all(targets, config_hash, binary_hash, transact=exchange, *, installed_limits=LIMITS, extended=False):
    rows = []
    for target in targets:
        guard_processes()
        row = dict(**target, parameters={}, errors=[])
        for key in PARAMS:
            try:
                row["parameters"][key] = read_parameter(target, key, transact)
            except (OSError, ValueError) as error:
                row["errors"].append(str(error))
        timeout_reply = None
        try:
            reply = transact(target, b"\xe0\x1f", 0x1F)
            if len(reply) < 4:
                raise ValueError("CAN_TIMEOUT回复长度无效")
            row["can_timeout_ms"] = struct.unpack_from(">H", reply, 2)[0]
            timeout_reply = reply
            version = transact(target, b"\xe0\x1e", 0x1E)
            if len(version) != 12:
                raise ValueError("版本回复长度无效")
            row["firmware"] = ".".join(map(str, version[2:5]))
            row["hardware"] = ".".join(map(str, version[5:8]))
            row["protocol"] = ".".join(map(str, version[8:12]))
        except (OSError, ValueError) as error:
            row["errors"].append(str(error))
        row["status"] = "unreadable" if row["errors"] else (
            "match" if all(value["matches"] for value in row["parameters"].values()) else "different")
        if extended:
            row["extended_parameters"] = read_extended_parameters(target, transact, timeout_reply=timeout_reply)
        rows.append(row)
    value = dict(configuration_sha256=config_hash, binary_sha256=binary_hash, rows=rows,
                 parameter_standard=PARAMETER_STANDARD, parameter_write_allowed=installed_limits == LIMITS)
    if extended:
        value["extended_parameter_standard"] = EXTENDED_STANDARD
    return dict(**value, snapshot_digest=digest(value))


def journal_write(path, value):
    with path.open("a", encoding="utf8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def update(snapshot, request, journal, transact=exchange):
    if snapshot["snapshot_digest"] != request["expected_digest"]:
        raise ValueError("机器人配置、版本或参数已变化，请重新检查；未写入")
    rows = {row["address"]: row for row in snapshot["rows"]}
    selected = request["selected"]
    rewrite = request["operation"] == "rewrite"
    write_items = {address: parameter_write_items(rows[address], rewrite=rewrite)
                   for address in selected if address in rows}
    if any(not write_items.get(address) for address in selected):
        if rewrite:
            raise ValueError("只能重写本次检查完整读出的电机参数")
        raise ValueError("只能更新本次检查完整读出并确认差异的电机")
    changed = []
    journal(dict(event="before", request=request, snapshot=snapshot))
    try:
        for address in selected:
            target = rows[address]
            definitions = extended_definitions(target["model"])
            expected_timeout = target["can_timeout_ms"]
            for key, value in write_items[address]:
                guard_processes()
                if key in PARAMS:
                    _, code, scale, signed, _ = PARAMS[key]
                    encoded = [round(number * scale) for number in value["expected"]]
                    payload = bytes([0xC0, code]) + struct.pack(">hh" if signed else ">HH", *encoded)
                else:
                    payload = definitions[key][1]
                journal(dict(event="write_requested", address=address, key=key, before=value))
                changed.append(dict(address=address, key=key, verified=False))
                transact(target, payload, None)
                result = (read_parameter(target, key, transact) if key in PARAMS else
                          read_extended_parameter(target, definitions[key], transact))
                if not result["matches"]:
                    raise ValueError(f"{address} {key}读回不匹配，后续写入已停止")
                changed[-1]["verified"] = True
                journal(dict(event="readback", **changed[-1], result=result))
                if key == "CAN_TIMEOUT":
                    expected_timeout = result["expected"]
            timeout = transact(target, b"\xe0\x1f", 0x1F)
            if len(timeout) < 4 or struct.unpack_from(">H", timeout, 2)[0] != expected_timeout:
                raise ValueError("CAN_TIMEOUT发生变化，后续写入已停止")
    except Exception as error:
        result = dict(state="partial", changes=changed, detail=str(error))
        journal(dict(event="stopped", **result))
        return result
    detail = ("已发送所选电机的重写指令并逐项核对读回；重新上电后请复查保存值。" if rewrite
              else "所选差异参数已写入并逐项读回；重新上电后请复查保存值。")
    result = dict(state="updated", changes=changed, detail=detail)
    journal(dict(event="complete", **result))
    return result


def main(request):
    request = validate_request(request)
    with maintenance_window(request["robot_id"]):
        targets, config_hash, binary_hash, installed_limits = configuration(request["dof"])
        if request["operation"] != "check" and installed_limits != LIMITS:
            raise ValueError(PARAMETER_WRITE_BLOCKED)
        snapshot = read_all(targets, config_hash, binary_hash, installed_limits=installed_limits, extended=True)
        if request["operation"] == "check":
            return dict(state="checked", **snapshot)
        directory = Path("/var/lib/rp1-monitor/motor-parameters")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (request["request_id"] + ".jsonl")
        # A repeated or interrupted update is never replayed.
        with path.open("x"):
            pass
        result = update(snapshot, request, lambda value: journal_write(path, value))
        return dict(**result, before=snapshot, journal=str(path))


def entry(request):
    try:
        result = main(request)
    except FileExistsError:
        result = dict(state="unknown", detail="此更新请求已有记录，未重复执行。请先重新读取参数并核对原更新日志。")
    except Exception as error:
        result = dict(state="rejected", detail=str(error)[:500])
    print(json.dumps(dict(**result, request_id=request["request_id"], robot_id=request["robot_id"],
                          dof=request["dof"], observed_at=datetime.now(timezone.utc).isoformat()),
                     ensure_ascii=True, allow_nan=False), flush=True)
