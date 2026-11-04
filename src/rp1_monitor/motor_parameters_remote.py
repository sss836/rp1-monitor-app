"""Native maintenance helper, transferred in memory; never loaded by the observer.

Only MIT encoding ranges are configurable here. No enable, zero, motion, flash,
ID, timeout or arbitrary register command is exposed.
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
# Same table verified in the installed inference_node, motors commit 2ee21b2.
LIMITS = [(12.5, 20., 30., 18., 500., 50.), (12.5, 40., 40., 32., 500., 50.),
          (12.5, 35., 65., 35., 500., 50.), (12.5, 16., 165., 90., 500., 50.)]
PARAMS = {"KP": (0x17, 0x05, 1, False, ""), "KD": (0x18, 0x06, 1, False, ""),
          "POS": (0x19, 0x07, 100, True, "rad"), "SPD": (0x1A, 0x08, 100, True, "rad/s"),
          "TOR": (0x1B, 0x09, 10, True, "N·m")}


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
    if (request["operation"] not in ("check", "update") or not isinstance(request["robot_id"], str)
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
        raise ValueError("更新前必须先检查并选择差异电机")
    return request


def compiled_limits(path):
    symbols = subprocess.run(["readelf", "-Ws", str(path)], capture_output=True,
                             text=True, timeout=8, check=True).stdout
    symbol = next((line.split() for line in symbols.splitlines()
                   if line.strip().endswith(" lro_limit_param")), None)
    if not symbol or int(symbol[2]) != 96:
        raise ValueError("已安装驱动的参数表未识别，不能套用新版标准")
    address = int(symbol[1], 16)
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("已安装推理程序不是所支持的ELF格式")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phsize, phnum = struct.unpack_from("<HH", data, 54)
    for index in range(phnum):
        kind, _, offset, vaddr, _, filesz, _, _ = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phsize)
        if kind == 1 and vaddr <= address and address + 96 <= vaddr + filesz:
            values = struct.unpack_from("<24f", data, offset + address - vaddr)
            rows = [tuple(values[i:i + 6]) for i in range(0, 24, 6)]
            if rows != LIMITS:
                raise ValueError("已安装驱动量程与本工具标准不同，请更新适配；未写入")
            return hashlib.sha256(data).hexdigest()
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
    return targets, hashlib.sha256(raw).hexdigest(), compiled_limits(executable)


def guard_processes():
    forbidden = {"inference_node", "start_robot.sh", "set_zero.py", "read_joint_angles.py",
                 "deploy_motorlro_params.py", "025-lro_motor_parameters.py"}
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
    if payload[0] == 0xE0:
        if len(payload) != 2 or payload[1] not in {0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1E, 0x1F}:
            raise ValueError("拒绝非白名单查询")
    elif payload[0] != 0xC0 or len(payload) != 6 or payload[1] not in {5, 6, 7, 8, 9}:
        raise ValueError("拒绝非量程写入")
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


def read_all(targets, config_hash, binary_hash, transact=exchange):
    rows = []
    for target in targets:
        guard_processes()
        row = dict(**target, parameters={}, errors=[])
        for key in PARAMS:
            try:
                row["parameters"][key] = read_parameter(target, key, transact)
            except (OSError, ValueError) as error:
                row["errors"].append(str(error))
        try:
            reply = transact(target, b"\xe0\x1f", 0x1F)
            if len(reply) < 4:
                raise ValueError("CAN_TIMEOUT回复长度无效")
            row["can_timeout_ms"] = struct.unpack_from(">H", reply, 2)[0]
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
        rows.append(row)
    value = dict(configuration_sha256=config_hash, binary_sha256=binary_hash, rows=rows)
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
    if any(address not in rows or rows[address]["status"] != "different" for address in selected):
        raise ValueError("只能更新本次检查完整读出的差异电机")
    changed = []
    journal(dict(event="before", request=request, snapshot=snapshot))
    try:
        for address in selected:
            target = rows[address]
            for key, value in target["parameters"].items():
                if value["matches"]:
                    continue
                guard_processes()
                _, code, scale, signed, _ = PARAMS[key]
                encoded = [round(number * scale) for number in value["expected"]]
                payload = bytes([0xC0, code]) + struct.pack(">hh" if signed else ">HH", *encoded)
                journal(dict(event="write_requested", address=address, key=key, before=value))
                changed.append(dict(address=address, key=key, verified=False))
                transact(target, payload, None)
                result = read_parameter(target, key, transact)
                if not result["matches"]:
                    raise ValueError(f"{address} {key}读回不匹配，后续写入已停止")
                changed[-1]["verified"] = True
                journal(dict(event="readback", **changed[-1], result=result))
            timeout = transact(target, b"\xe0\x1f", 0x1F)
            if len(timeout) < 4 or struct.unpack_from(">H", timeout, 2)[0] != target["can_timeout_ms"]:
                raise ValueError("CAN_TIMEOUT发生变化，后续写入已停止")
    except Exception as error:
        result = dict(state="partial", changes=changed, detail=str(error))
        journal(dict(event="stopped", **result))
        return result
    result = dict(state="updated", changes=changed, detail="所选差异量程已写入并逐项读回；未验证掉电保持，需重新检查。")
    journal(dict(event="complete", **result))
    return result


def main(request):
    request = validate_request(request)
    with maintenance_window(request["robot_id"]):
        targets, config_hash, binary_hash = configuration(request["dof"])
        snapshot = read_all(targets, config_hash, binary_hash)
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
