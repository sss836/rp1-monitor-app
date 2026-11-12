"""Native transport and verified local package for the fixed LRO firmware release."""

import base64
import hashlib
import json
import shlex
import time
import zipfile
import zlib
from pathlib import Path

import httpx

from .deploy_policy import station_headers
from .factory_client import FactoryClient, _local_url
from .motor_firmware_remote import FIRMWARE, FIRMWARE_BYTES, PAYLOAD_READY, validate_request
from .ssh_transport import parse_ssh_target

VENDOR_SHA256 = "a34858d37ee8fcae03d8a7cc0530f02858549f5de835926b05403025139acbf4"
BUNDLE_PATH = Path(__file__).with_name("data") / "lro-firmware-109-1028.zip"
SUDO_PROMPT = b"RP1_FIRMWARE_SUDO:"
CHECK_TIMEOUT = 90
UPDATE_TIMEOUT = 3600


def firmware_release(*, verify=False):
    if verify:
        load_bundle()
    return FIRMWARE.copy()


def load_bundle(path=None):
    path = BUNDLE_PATH if path is None else path
    def checked_entry(bundle, name, size, sha256):
        if bundle.getinfo(name).file_size != size:
            raise ValueError("电机固件包的文件大小与已核对版本不一致")
        data = bundle.read(name)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("电机固件包的 SHA-256 校验失败")
        return data

    try:
        with zipfile.ZipFile(path) as bundle:
            payload = checked_entry(bundle, FIRMWARE["filename"], FIRMWARE_BYTES, FIRMWARE["sha256"])
            vendor = checked_entry(bundle, "motor_ota.py", 23064, VENDOR_SHA256)
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
        raise ValueError("上位机的 1028 电机固件包缺失或损坏，请更新安装包") from error
    return payload, vendor


def _command(request, vendor):
    validate_request(request)
    module_sources = {
        "rp1_monitor.motor_parameters_remote": Path(__file__).with_name("motor_parameters_remote.py").read_bytes(),
        "rp1_monitor.motor_firmware_remote": Path(__file__).with_name("motor_firmware_remote.py").read_bytes(),
    }
    if vendor:
        module_sources["rp1_motor_ota"] = vendor
    packed = {name: base64.b64encode(zlib.compress(data)).decode("ascii") for name, data in module_sources.items()}
    source = """import base64, sys, types, zlib
package = types.ModuleType("rp1_monitor")
package.__path__ = []
sys.modules[package.__name__] = package
for name, data in MODULE_SOURCES.items():
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(zlib.decompress(base64.b64decode(data)), name, "exec"), module.__dict__)
sys.modules["rp1_monitor.motor_firmware_remote"].entry(REQUEST)
"""
    source = "MODULE_SOURCES = " + repr(packed) + "\nREQUEST = " + repr(request) + "\n" + source
    return shlex.join(["sudo", "-k", "-S", "-p", SUDO_PROMPT.decode(), "--",
                       "/usr/bin/python3", "-I", "-B", "-u", "-c", source])


def _uncertain(request):
    return dict(state="unknown", detail="固件写入回执中断，请现场核对版本后再决定下一步。",
                firmware=firmware_release(), plan_sha256=request["plan_sha256"], rows=[],
                **{key: request[key] for key in ("request_id", "robot_id", "dof")})


def _remote(tunnel, password, request):
    updating = request["operation"] == "update"
    payload, vendor = load_bundle() if updating else (None, None)
    channel = tunnel.transport.open_session(timeout=8)
    transferred = False
    password_sent = False
    try:
        channel.settimeout(30)
        channel.exec_command(_command(request, vendor))
        out, error = bytearray(), bytearray()
        deadline = time.monotonic() + CHECK_TIMEOUT
        while time.monotonic() < deadline:
            if channel.recv_ready():
                out.extend(channel.recv(65536))
                if updating and not transferred and out.startswith(PAYLOAD_READY):
                    del out[:len(PAYLOAD_READY)]
                    transferred = True
                    channel.sendall(payload)
                    channel.shutdown_write()
                    deadline = time.monotonic() + UPDATE_TIMEOUT
            elif channel.recv_stderr_ready():
                error.extend(channel.recv_stderr(4096))
                prompt = error.find(SUDO_PROMPT)
                if prompt != -1:
                    if password_sent:
                        raise ValueError("机器人未接受本次sudo密码")
                    del error[:prompt + len(SUDO_PROMPT)]
                    channel.sendall((password + "\n").encode())
                    password_sent = True
                    if not updating:
                        channel.shutdown_write()
            elif channel.exit_status_ready() or not tunnel.transport.is_active():
                break
            else:
                time.sleep(.02)
            if len(out) > 512 * 1024 or len(error) > 16384:
                raise ValueError("固件操作回执超过上限")
        if not out:
            raise ValueError("未收到固件操作回执，请核对本次sudo密码与机器人连接")
        return json.loads(out)
    except Exception:
        if transferred:
            return _uncertain(request)
        raise
    finally:
        channel.close()


def perform(context, password, request):
    request = validate_request(request)
    if not password or len(password) > 4096 or any(c in password for c in "\r\n"):
        raise ValueError("请输入本次单行sudo密码")
    if context.get("robot_id") != request["robot_id"] or parse_ssh_target(context.get("target", ""))[0] != "robo":
        raise ValueError("固件操作与已绑定的机器人身份不匹配")
    if context.get("station_url"):
        timeout = UPDATE_TIMEOUT if request["operation"] == "update" else CHECK_TIMEOUT
        with httpx.Client(timeout=timeout + 30, trust_env=False, follow_redirects=False) as client:
            try:
                response = client.post(_local_url(context["station_url"]) + "/api/v1/station/motor-firmware",
                                       headers=station_headers(context), json=dict(
                                           robot_id=request["robot_id"], relay_url=context["relay_url"],
                                           request=request, sudo_password=password))
            except httpx.TransportError:
                if request["operation"] == "update":
                    return _uncertain(request)
                raise ValueError("未收到电脑后台的固件检查回执") from None
        if response.status_code == 404:
            raise ValueError("电脑后台尚未支持电机固件入口，请更新后台")
        if response.status_code != 200:
            raise ValueError(response.json().get("detail") or "电脑后台未确认固件操作")
        result = response.json()
    else:
        tunnel = context.get("tunnel")
        if not tunnel or not tunnel.transport.is_active():
            raise ValueError("请先在设备设置连接待维护的机器人")
        identity = FactoryClient(tunnel.url).probe()
        if (identity.get("robot_id"), identity.get("source_kind")) != (request["robot_id"], "ros2"):
            raise ValueError("已连接机器人的身份或来源不匹配")
        result = _remote(tunnel, password, request)
    if (not isinstance(result, dict) or
            any(result.get(key) != request[key] for key in ("request_id", "robot_id", "dof")) or
            result.get("firmware") != FIRMWARE):
        if request["operation"] == "update":
            return _uncertain(request)
        raise ValueError("固件检查回执身份不匹配")
    return result
