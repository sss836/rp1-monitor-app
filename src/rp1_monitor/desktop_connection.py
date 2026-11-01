"""Read-only desktop connection validation and service identity preflight."""

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PySide6.QtCore import QThread, Signal

from .operations_client import validate_url


def local_endpoint(value):
    value = value.strip()
    parsed = urlparse(validate_url(value, local=True))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("服务端口应为 1–65535") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("服务端口应为 1–65535")
    if parsed.path not in {"", "/"}:
        raise ValueError("请填写上位机 relay 根地址，不要填写网页或 API 路径")
    return parsed.geturl().rstrip("/")


def station_control_token(viewer_token_file):
    """Only native PC controls read the separate local operation credential."""
    return Path(viewer_token_file).with_name("station-token.txt").read_text().strip()


def ssh_arguments(target, key, local_port, remote_port):
    target = target.strip()
    match = re.fullmatch(r"([a-zA-Z_][a-zA-Z0-9_.-]*)@([^\s/@]+)", target)
    if not match:
        raise ValueError("SSH目标应为 用户名@机器人IP，不包含命令、端口或空格")
    host = match.group(2)
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host):
            raise ValueError("SSH目标主机名或IP无效") from None
    if any(type(port) is not int or not 1 <= port <= 65535 for port in (local_port, remote_port)):
        raise ValueError("SSH转发端口应为 1–65535")
    args = [
        "-N",
        "-T",
        "-oBatchMode=yes",
        "-oStrictHostKeyChecking=yes",
        "-oExitOnForwardFailure=yes",
        "-oConnectTimeout=5",
        "-oServerAliveInterval=5",
        "-oServerAliveCountMax=2",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
    ]
    if key.strip():
        path = Path(key.strip()).expanduser()
        if not path.is_file():
            raise ValueError("SSH密钥文件不存在；也可留空使用系统 ssh-agent")
        args += ["-i", str(path)]
    return args + ["--", target]


def identify_service(report, mode):
    if not isinstance(report, dict) or not isinstance(report.get("snapshot"), dict):
        raise ValueError("地址未返回 RP1 只读自检，请核对 relay 端口；不要填写中央网页地址")
    snapshot = report["snapshot"]
    if (
        not isinstance(snapshot.get("robot_id"), str)
        or not snapshot["robot_id"]
        or snapshot.get("source_kind") not in {"simulated", "ros2"}
        or not isinstance(snapshot.get("upload", {}), dict)
    ):
        raise ValueError("只读自检缺少机器人身份或来源，暂不连接")
    stage = snapshot.get("upload", {}).get("stage")
    relay = stage == "upper_computer"
    if (mode == "relay") != relay:
        raise ValueError(
            "此地址是机器人采集端；请连接上位机后台 relay，或切换临时SSH直读模式"
            if mode == "relay"
            else "SSH端口返回上位机 relay；请核对机器人采集端口"
        )
    return {"robot_id": snapshot["robot_id"], "source_kind": snapshot["source_kind"]}


class ConnectionProbe(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, url, mode):
        super().__init__()
        self.url, self.mode = url, mode

    def run(self):
        try:
            with httpx.Client(timeout=1.5, trust_env=False, follow_redirects=False) as client:
                for attempt in range(5):
                    if self.isInterruptionRequested():
                        return
                    try:
                        response = client.get(self.url + "/api/v1/doctor")
                        response.raise_for_status()
                        result = identify_service(response.json(), self.mode)
                        if not self.isInterruptionRequested():
                            self.done.emit(result)
                        return
                    except (httpx.ConnectError, httpx.TimeoutException):
                        if attempt == 4:
                            raise
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 503 or attempt == 4:
                            raise
                    # Allow SSH authentication and the first relay preview to arrive.
                    for _ in range(5):
                        if self.isInterruptionRequested():
                            return
                        self.msleep(100)
        except httpx.HTTPStatusError as exc:
            self.failed.emit(
                "上位机尚未收到机器人预览，请检查后台 relay、网线隧道和采集端后重试"
                if exc.response.status_code == 503
                else f"只读预检返回 HTTP {exc.response.status_code}；请核对本机 relay 地址与端口"
            )
        except httpx.HTTPError:
            self.failed.emit("无法读取监控服务；请确认后台 relay 或SSH隧道已运行，再点击连接重试")
        except (ValueError, TypeError) as exc:
            self.failed.emit(str(exc) if isinstance(exc, ValueError) else "只读自检格式无效")


class StationStateProbe(QThread):
    """Read an existing PC connection without creating a tunnel or consuming data."""

    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, url, token_file):
        super().__init__()
        self.url, self.token_file = url, token_file

    def run(self):
        try:
            url = local_endpoint(self.url)
            token = Path(self.token_file).read_text().strip()
            if self.isInterruptionRequested():
                return
            with httpx.Client(timeout=1.5, trust_env=False, follow_redirects=False,
                              headers={"Authorization": f"Bearer {token}"}) as client:
                response = client.get(url + "/api/v1/station")
                response.raise_for_status()
                state = response.json()
            if not isinstance(state, dict) or type(state.get("connected")) is not bool:
                raise ValueError("电脑后台连接状态格式无效，请使用SSH表单连接")
            if state["connected"]:
                state["relay_url"] = local_endpoint(state.get("relay_url", ""))
            for connection in state.get("connections", []):
                if not isinstance(connection, dict) or type(connection.get("connected")) is not bool:
                    raise ValueError("电脑后台连接列表格式无效")
                if connection["connected"]:
                    connection["relay_url"] = local_endpoint(connection.get("relay_url", ""))
            if not self.isInterruptionRequested():
                self.done.emit(state)
        except (OSError, ValueError, TypeError, AttributeError, httpx.HTTPError):
            if not self.isInterruptionRequested():
                self.failed.emit("未能读取电脑后台已有连接，可填写SSH地址与密码后重试")


class StationConnector(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, url, token_file, body):
        super().__init__()
        self.url, self.token_file, self.body = url, token_file, body

    def run(self):
        try:
            url = local_endpoint(self.url)
            token = Path(self.token_file).read_text().strip()
            with httpx.Client(timeout=40, trust_env=False, follow_redirects=False,
                              headers={"Authorization": f"Bearer {token}"}) as client:
                state = client.get(url + "/api/v1/station")
                state.raise_for_status()
                response = client.post(
                    url + "/api/v1/station/connect", json=self.body,
                    headers={"X-RP1-Station-Token": station_control_token(self.token_file)},
                )
                if response.status_code != 200:
                    detail = response.json().get("detail")
                    self.failed.emit(detail if isinstance(detail, str) else "连接失败，请核对SSH参数")
                elif not self.isInterruptionRequested():
                    self.done.emit(response.json())
        except (OSError, ValueError, KeyError, httpx.HTTPError):
            self.failed.emit("无法连接电脑快捷后台，请使用 start-monitor.sh 启动后重试")
        finally:
            self.body.clear()


class StationDisconnector(QThread):
    """Disconnect only the robot explicitly chosen in this native window."""

    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, url, token_file, robot_id):
        super().__init__()
        self.url, self.token_file, self.robot_id = url, token_file, robot_id

    def run(self):
        try:
            url = local_endpoint(self.url)
            token = Path(self.token_file).read_text().strip()
            with httpx.Client(timeout=15, trust_env=False, follow_redirects=False,
                              headers={"Authorization": f"Bearer {token}"}) as client:
                response = client.post(
                    url + "/api/v1/station/disconnect", json={"robot_id": self.robot_id},
                    headers={"X-RP1-Station-Token": station_control_token(self.token_file)},
                )
                response.raise_for_status()
                if not self.isInterruptionRequested():
                    self.done.emit(response.json())
        except (OSError, ValueError, httpx.HTTPError):
            if not self.isInterruptionRequested():
                self.failed.emit("未能断开所选后台机器人，请刷新连接状态后重试")
