"""Native desktop transport for explicit motor range maintenance."""

import json
import shlex
import time
from pathlib import Path

import httpx

from .deploy_policy import station_headers
from .factory_client import FactoryClient, _local_url
from .motor_parameters_remote import validate_request
from .ssh_transport import parse_ssh_target


def command(request):
    request = validate_request(dict(request))
    source = Path(__file__).with_name("motor_parameters_remote.py").read_text()
    source += "\nentry(" + repr(request) + ")\n"
    return shlex.join(["sudo", "-S", "-p", "RP1_PARAMETER_SUDO:", "--", "/usr/bin/taskset",
                       "-c", "1-3,7", "/usr/bin/nice", "-n", "10", "/usr/bin/python3", "-B", "-u", "-c", source])


def perform(context, password, request):
    request = validate_request(dict(request))
    if not password or any(c in password for c in "\r\n"):
        raise ValueError("请输入本次sudo密码，密码不保存")
    if context.get("robot_id") != request["robot_id"]:
        raise ValueError("参数操作与已绑定机器人不一致")
    if parse_ssh_target(context.get("target", ""))[0] != "robo":
        raise ValueError("参数维护只适用于已绑定的robo用户")
    if context.get("station_url"):
        timeout = 250 if request["operation"] == "check" else 470
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            response = client.post(_local_url(context["station_url"]) + "/api/v1/station/motor-parameters",
                                   headers=station_headers(context), json=dict(
                                       robot_id=request["robot_id"], relay_url=context["relay_url"],
                                       request=request, sudo_password=password))
        if response.status_code == 404:
            raise ValueError("电脑后台尚未支持电机参数入口，请更新后台；未改用第二次SSH登录")
        if response.status_code != 200:
            raise ValueError("电脑后台未确认参数操作，请核对连接；更新状态不明时先重新读取，勿重复写入")
        result = response.json()
    else:
        tunnel = context.get("tunnel")
        if not tunnel or not tunnel.transport.is_active():
            raise ValueError("请先在设备设置连接机器人；不会使用可编辑地址猜测目标")
        identity = FactoryClient(tunnel.url).probe()
        if identity.get("robot_id") != request["robot_id"] or identity.get("source_kind") != "ros2":
            raise ValueError("已连接机器人的身份或来源不匹配")
        result = _remote(tunnel, password, request)
    if (result.get("request_id") != request["request_id"] or result.get("robot_id") != request["robot_id"]
            or result.get("dof") != request["dof"]):
        raise ValueError("参数操作回执身份不匹配；请重新读取核对实际状态")
    return result


def _remote(tunnel, password, request):
    channel = tunnel.transport.open_session(timeout=8)
    dispatched = False
    try:
        # No screen or PTY. sudo consumes one line through stdin, never argv.
        dispatched = True
        channel.exec_command(command(request))
        channel.sendall(password + "\n")
        channel.shutdown_write()
        out, error = bytearray(), bytearray()
        deadline = time.monotonic() + (220 if request["operation"] == "check" else 430)
        while time.monotonic() < deadline:
            if channel.recv_ready():
                out.extend(channel.recv(65536))
            elif channel.recv_stderr_ready():
                error.extend(channel.recv_stderr(4096))
            elif channel.exit_status_ready() or not tunnel.transport.is_active():
                break
            else:
                time.sleep(.02)
            if len(out) > 512 * 1024 or len(error) > 16384:
                raise ValueError("参数操作响应超过上限，请检查机器人日志")
        if not out:
            raise ValueError("未收到参数回执，请检查本次sudo密码与机器人状态")
        return json.loads(out)
    except Exception as error:
        if dispatched and request["operation"] != "check":
            return dict(state="unknown", request_id=request["request_id"], robot_id=request["robot_id"],
                        dof=request["dof"], detail="更新回执中断，部分参数可能已写入。请重新检查，禁止直接重复写入。")
        raise ValueError(str(error).replace(password, "[已隐藏]")) from None
    finally:
        channel.close()
