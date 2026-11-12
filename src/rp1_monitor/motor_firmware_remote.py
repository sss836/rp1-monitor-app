"""Fixed RP1 LRO release maintenance helper executed through verified native SSH."""

import hashlib
import json
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID

from .motor_parameters_remote import digest, guard_processes, maintenance_window

DEPLOY_ROOT = Path("/home/robo/roboparty_deploy")
FIRMWARE = {
    "version": "5.0.0.109",
    "build": "202610281537",
    "sha256": "7277645da5e2d461e0339e87e21b5603abcb13065416fb416a1e279d48eb966a",
    "filename": "L5N_RZT2L_CAN_CDH2_V5.0.0.109_202610281537_release.lsepbin",
}
FIRMWARE_BYTES = 501316
PAYLOAD_READY = b"RP1_FIRMWARE_PAYLOAD_READY\n"
# Bus/model assignments from roboparty_inference@b3eaa0bb6a9dfd83e723b798fb7f986c12c187c8.
# LRO_5550, LRO_6562, LRO_8462, LRO_10062 are the official model numbers 0–3.
LEG_MODELS = (3, 3, 3, 2, 3, 1, 1)
ARM_MODELS = (2, 1, 1, 1, 1, 0, 0)
BUS_NAMES = {"can0", "can1", "can2", "can3"}


def _sha256_string(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_request(request):
    base_fields = {"operation", "request_id", "robot_id", "dof"}
    update_fields = {"targets", "maintenance_confirmed", "firmware_sha256", "plan_sha256"}
    if not isinstance(request, dict) or request.get("operation") not in ("check", "update"):
        raise ValueError("固件操作无效")
    updating = request["operation"] == "update"
    if set(request) != (base_fields | update_fields if updating else base_fields):
        raise ValueError("固件请求字段不完整或含未知字段")
    request_id = request["request_id"]
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("请求编号无效")
    if type(request["dof"]) is not int or request["dof"] not in (24, 28):
        raise ValueError("仅支持24或28轴")
    if not isinstance(request["robot_id"], str) or not 1 <= len(request["robot_id"]) <= 64:
        raise ValueError("机器人编号无效")
    if not updating:
        return request
    if (request["maintenance_confirmed"] is not True or
            request["firmware_sha256"] != FIRMWARE["sha256"] or
            not _sha256_string(request["plan_sha256"])):
        raise ValueError("刷写前请检查目标、核对固件并确认现场维护条件")
    targets = request["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= request["dof"]:
        raise ValueError("请选择要刷写的电机")
    addresses = set()
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {"interface", "motor_id", "model"} or
                not isinstance(target["interface"], str) or target["interface"] not in BUS_NAMES or
                type(target["motor_id"]) is not int or not 1 <= target["motor_id"] <= 7 or
                type(target["model"]) is not int or not 0 <= target["model"] <= 3):
            raise ValueError("存在无法识别的电机目标")
        address = (target["interface"], target["motor_id"])
        if address in addresses:
            raise ValueError("刷写电机目标重复")
        addresses.add(address)
    return request


def _read_map(path, dof):
    import yaml

    try:
        data = yaml.safe_load(path.read_bytes())["motors"]
        counts = data["motor_num"]
        names = data["motor_interface"]
        types = data["motor_interface_type"]
        drivers = data["motor_type"]
        ids, models = data["motor_id"], data["motor_model"]
    except (KeyError, TypeError, yaml.YAMLError) as error:
        raise ValueError("电机配置无法解析") from error
    if (not all(isinstance(items, list) for items in (counts, names, types, drivers, ids, models)) or
            any(len(items) != 4 for items in (counts, names, types, drivers)) or
            any(not isinstance(name, str) for name in names) or set(names) != BUS_NAMES or
            types != ["canfd"] * 4 or drivers != ["LRO"] * 4 or
            any(type(count) is not int or not 1 <= count <= 7 for count in counts) or
            sum(counts) != dof or len(ids) != dof or len(models) != dof):
        raise ValueError("电机配置与官方24/28轴 LRO CAN-FD 清单不符")
    targets, offset = [], 0
    for interface, count in zip(names, counts):
        model_by_id = LEG_MODELS if interface in {"can1", "can2"} else ARM_MODELS[:5 if dof == 24 else 7]
        expected = dict(enumerate(model_by_id, 1))
        target_ids = ids[offset:offset + count]
        target_models = models[offset:offset + count]
        if (any(type(value) is not int for value in target_ids + target_models) or
                count != len(expected) or dict(zip(target_ids, target_models)) != expected):
            raise ValueError(f"{interface} 的电机ID、型号与官方机型清单不符")
        targets.extend(dict(interface=interface, motor_id=motor_id, model=model)
                       for motor_id, model in zip(target_ids, target_models))
        offset += count
    return targets


def configuration(dof, root=None):
    root = DEPLOY_ROOT if root is None else root
    model = "rp1_24dof" if dof == 24 else "rp1"
    source = root / "src/inference/robots" / model / "robot.yaml"
    installed = root / "install/roboparty_inference/share/roboparty_inference/robots" / model / "robot.yaml"
    targets = _read_map(source, dof)
    if installed.exists() and sorted(_read_map(installed, dof), key=_address) != sorted(targets, key=_address):
        raise ValueError("部署源码和已安装程序的电机配置不同，请先核对")
    return targets


def _address(target):
    return target["interface"], target["motor_id"]


def _query_version(target, query, timeout):
    can_id = target["motor_id"]
    with socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as bus:
        bus.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FD_FRAMES, 1)
        bus.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                       struct.pack("=II", can_id, 0xC00007FF))
        bus.bind((target["interface"],))
        bus.send(struct.pack("=IBB2x64s", can_id, 2, 1, bytes((0xE0, query))))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            bus.settimeout(max(.001, deadline - time.monotonic()))
            try:
                frame = bus.recv(72)
            except socket.timeout:
                break
            if len(frame) < 8:
                continue
            reply_id, length = struct.unpack_from("=IB", frame)
            data = frame[8:8 + length]
            if reply_id != can_id or len(data) < 2 or data[0] & 0xE0 != 0xA0 or data[1] != query:
                continue
            if data[0] & 7:
                raise ValueError(f"电机报告故障码 {data[0] & 7}")
            if query == 49 and len(data) >= 20:
                value = data[2:20].split(b"\0", 1)[0].decode("ascii").strip()
                if value and value.isprintable():
                    return {"version": value, "source": "firmware_string"}
            if query == 30 and len(data) in (8, 12):
                return {"version": ".".join(map(str, data[2:5])), "source": "numeric",
                        "hardware": ".".join(map(str, data[5:8])),
                        "protocol": ".".join(map(str, data[8:12])) or None}
            raise ValueError("电机版本回执格式无效")
    raise ValueError("电机版本查询超时")


def read_version(target, timeout=.4):
    for query in (49, 30):
        try:
            return _query_version(target, query, timeout)
        except (OSError, ValueError):
            pass
    return None


def wait_for_version(target):
    deadline = time.monotonic() + 30
    # The vendor sends reset without an acknowledgement. Allow reboot before querying.
    time.sleep(2)
    while time.monotonic() < deadline:
        if result := read_version(target):
            return result
        time.sleep(.5)
    return None


def version_status(after):
    if not after:
        return "version_unconfirmed", "OTA 流程已结束，复位后未读到版本"
    reported = after["version"]
    if after["source"] == "firmware_string":
        if re.search(r"(?<![\d.])5\.0\.0\.109(?![\d.])", reported):
            return "verified", "OTA 流程已结束，复位后读回目标版本 5.0.0.109"
        if re.search(r"(?<![\d.])5\.0\.0\.\d+(?![\d.])", reported):
            return "version_mismatch", f"OTA 流程已结束，复位后仍读到其他版本 {reported}"
    elif reported in {"1.0.8", "0.1.8"}:
        return "version_mismatch", f"OTA 流程已结束，复位后仍读到旧版本响应 {reported}"
    return "version_unconfirmed", f"OTA 流程已结束，复位后版本回执为 {reported}，待核对目标版本"


def check_can_bus(interface):
    probe = subprocess.run(["/usr/sbin/ip", "-json", "-details", "link", "show", "dev", interface],
                           check=True, capture_output=True, timeout=5)
    links = json.loads(probe.stdout)
    if len(links) != 1:
        raise ValueError(f"{interface} 无法确认 CAN-FD 配置")
    link = links[0]
    info = link.get("linkinfo", {})
    data = info.get("info_data", {})
    bitrate = data.get("bittiming", {}).get("bitrate", data.get("bittiming_bitrate"))
    data_bitrate = data.get("data_bittiming", {}).get("bitrate", data.get("data_bittiming_bitrate"))
    if (link.get("ifname") != interface or "UP" not in link.get("flags", ()) or link.get("mtu") != 72 or
            info.get("info_kind") != "can" or data.get("state") != "ERROR-ACTIVE" or
            bitrate != 1_000_000 or data_bitrate != 5_000_000):
        raise ValueError(f"{interface} 未处于正常的 CAN-FD 1/5 Mbps 状态")


def _row(target):
    return dict(**target, address=f"{target['interface']}/{target['motor_id']}",
                eligible=True, before=None, after=None, status="not_attempted",
                ota_completed=False, detail="")


def _flash_bus(targets, flasher, path, ota):
    rows, stop = [], False
    for target in targets:
        row = _row(target)
        rows.append(row)
        if stop:
            row["detail"] = "同一总线此前有刷写或读回异常，待现场核对后再操作"
            continue
        try:
            guard_processes()
        except ValueError as error:
            row["detail"] = str(error)
            stop = True
            continue
        row["before"] = read_version(target)
        ota_id = 0x1000 + 2 * target["motor_id"] - 1
        try:
            flasher.flash(path, ota_id)
        except (ota.FlashError, OSError) as error:
            row.update(status="flash_failed", detail=str(error))
            stop = True
            continue
        after = wait_for_version(target)
        status, detail = version_status(after)
        # The vendor accepts both positive verification and provisional acceptance.
        row.update(after=after, status=status, detail=detail, ota_completed=True)
        stop = status == "version_mismatch"
    return rows


def run_operation(request, *, receive_payload=None, ota=None):
    request = validate_request(request)
    receipt = dict(state="blocked", detail="", firmware=FIRMWARE.copy(), plan_sha256="", rows=[],
                   **{key: request[key] for key in ("request_id", "robot_id", "dof")})
    try:
        with maintenance_window(request["robot_id"]):
            targets = configuration(request["dof"])
            receipt["plan_sha256"] = digest(dict(robot_id=request["robot_id"], dof=request["dof"],
                                                 firmware=FIRMWARE["sha256"], targets=targets))
            if request["operation"] == "check":
                for target in targets:
                    guard_processes()
                    before = read_version(target)
                    receipt["rows"].append(dict(_row(target), before=before,
                                               status="read" if before else "unreadable",
                                               detail="" if before else "未读到当前固件版本"))
                receipt["state"] = "completed"
                return receipt
            if request["plan_sha256"] != receipt["plan_sha256"]:
                raise ValueError("机器人或电机配置与检查结果不同，请重新检查")
            known = {_address(target): target for target in targets}
            groups = {}
            for target in request["targets"]:
                if known.get(_address(target)) != target:
                    raise ValueError("所选电机与当前机器人型号或 CAN 地址不一致")
                groups.setdefault(target["interface"], []).append(target)
            for interface in groups:
                check_can_bus(interface)
            binary = receive_payload()
            if len(binary) != FIRMWARE_BYTES or hashlib.sha256(binary).hexdigest() != FIRMWARE["sha256"]:
                raise ValueError("机器人收到的固件与已核对的 1028 载荷不一致")
            with tempfile.TemporaryDirectory(prefix="rp1-motor-firmware-") as directory, ExitStack() as stack:
                path = Path(directory) / FIRMWARE["filename"]
                path.write_bytes(binary)
                flashers = {interface: stack.enter_context(ota.LinuxFlasher(interface=interface))
                            for interface in groups}
                with ThreadPoolExecutor(max_workers=len(groups)) as pool:
                    jobs = [pool.submit(_flash_bus, group, flashers[interface], path, ota)
                            for interface, group in groups.items()]
                    receipt["state"] = "unknown"
                    for job in jobs:
                        receipt["rows"].extend(job.result())
            receipt["state"] = "completed"
            return receipt
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        receipt["detail"] = str(error)
    except Exception as error:
        if ota is None or not isinstance(error, ota.FlashError):
            raise
        receipt["detail"] = str(error)
    return receipt


def _receive_payload():
    sys.stdout.buffer.write(PAYLOAD_READY)
    sys.stdout.buffer.flush()
    return sys.stdin.buffer.read(FIRMWARE_BYTES)


def entry(request):
    ota = None
    if request["operation"] == "update":
        import rp1_motor_ota as ota
    result = run_operation(request, receive_payload=_receive_payload, ota=ota)
    print(json.dumps(result, ensure_ascii=False))
