"""Native BMS recovery over the station's already authenticated SSH connection."""

import shlex
import threading
import time

import httpx
from pydantic import ValidationError

from .deploy_policy import station_headers
from .factory_basic_checks import FactoryCheckReport
from .factory_client import FactoryClient, _local_url

_BOOTSTRAP = """import json, os, re, sys
from pathlib import Path
root = Path.home() / '.local/share/rp1-collector-managed'
with (root / 'installation.json').open() as source:
    info = json.load(source)
robot_id = sys.argv[1]
release_name = info.get('release', '')
if info.get('robot_id') != robot_id or not re.fullmatch('[0-9a-f]{32}', release_name):
    sys.exit('RP1_COLLECTOR_BINDING_MISMATCH')
release = root / 'releases' / release_name
if release.resolve() != release or not release.is_dir():
    sys.exit('RP1_COLLECTOR_BINDING_MISMATCH')
python = str(release / '.venv/bin/python')
entry = 'import sys; from rp1_robot.factory_basic_runner import native_bms_recovery; native_bms_recovery(sys.argv[1])'
os.execv(python, [python, '-I', '-B', '-u', '-c', entry, robot_id])
"""


def _report(data, robot_id):
    try:
        result = FactoryCheckReport.model_validate(data)
    except ValidationError:
        raise ValueError("BMS重启恢复回执格式无效，请先读取状态。") from None
    if (result.robot_id, result.source_kind, result.check_id) != (robot_id, "ros2", "BMS_RECOVERY"):
        raise ValueError("BMS重启恢复回执与当前实机不一致。")
    return result


def request_recovery(context, password):
    robot_id = context["robot_id"]
    with httpx.Client(timeout=80, trust_env=False, follow_redirects=False) as client:
        response = client.post(
            _local_url(context["station_url"]) + "/api/v1/station/bms-recovery",
            headers=station_headers(context),
            json={"robot_id": robot_id, "relay_url": context["relay_url"], "sudo_password": password},
        )
    if response.status_code == 404:
        raise ValueError("电脑后台尚未加载BMS重启更新，请在停止记录后重启电脑后台。")
    result = response.json()
    if response.status_code != 200:
        detail = result.get("detail") if isinstance(result, dict) else None
        raise ValueError(detail if isinstance(detail, str) else "BMS重启回执未确认，请先读取状态。")
    return _report(result, robot_id)


def run_recovery(tunnel, robot_id, password):
    if not password or any(char in password for char in "\r\n\x00"):
        raise ValueError("请输入单行sudo密码。")
    identity = FactoryClient(tunnel.url, timeout=5).probe()
    if (identity.get("robot_id"), identity.get("source_kind")) != (robot_id, "ros2"):
        raise ValueError("采集身份已变化，未发送重启命令。")
    channel = tunnel.transport.open_session(timeout=5)
    deadline = time.monotonic() + 65
    timer = threading.Timer(65, channel.close)
    timer.daemon = True
    timer.start()
    try:
        command = shlex.join(["/usr/bin/python3", "-I", "-B", "-u", "-c", _BOOTSTRAP, robot_id])
        channel.exec_command(command)
        channel.sendall((password + "\n").encode())
        channel.shutdown_write()
        out, err = bytearray(), bytearray()
        while time.monotonic() < deadline:
            if channel.recv_ready():
                out.extend(channel.recv(4096))
            elif channel.recv_stderr_ready():
                err.extend(channel.recv_stderr(4096))
            elif channel.exit_status_ready():
                break
            else:
                time.sleep(0.02)
            if len(out) > 64 * 1024 or len(err) > 16 * 1024:
                raise ValueError("BMS回执过大，重启状态未确认，请先读取状态。")
        if channel.exit_status_ready() and time.monotonic() < deadline:
            code = channel.recv_exit_status()
            if code == 0:
                try:
                    result = FactoryCheckReport.model_validate_json(out)
                except ValidationError:
                    raise ValueError("BMS重启恢复回执格式无效，请先读取状态。") from None
                return _report(result, robot_id)
            if b"cannot import name 'native_bms_recovery'" in err:
                raise ValueError("机器人采集端尚未支持手动sudo授权，请先更新采集端。")
            if b"RP1_COLLECTOR_BINDING_MISMATCH" in err:
                raise ValueError("采集端安装身份与当前实机不一致，未执行重启。")
        raise ValueError("BMS重启回执未确认，请先读取状态，勿直接重复重启。")
    finally:
        timer.cancel()
        channel.close()
