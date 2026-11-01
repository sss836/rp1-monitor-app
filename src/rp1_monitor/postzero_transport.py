"""Dedicated, cancellable SSH capture for an explicitly approved pose recheck."""

import shlex
import time
from pathlib import Path

from .postzero import MAX_LINE_BYTES, read_event, validate_session_id
from .ssh_transport import parse_ssh_target

MAX_STDOUT_BYTES = 512 * 1024
MAX_STDERR_BYTES = 8 * 1024
CAPTURE_TIMEOUT_SECONDS = 35
PASSWORD_PROMPT = b"RP1_POSTZERO_SUDO_PASSWORD:"


class PostzeroCaptureError(ValueError):
    pass


def build_remote_command(source, dof, session_id):
    validate_session_id(session_id)
    if type(dof) is not int or dof not in (24, 28):
        raise ValueError("角度复查仅支持24或28台物理电机")
    if not isinstance(source, str) or not source or len(source.encode("utf-8")) > MAX_STDOUT_BYTES:
        raise ValueError("角度复查helper源码无效")
    return shlex.join([
        "/usr/bin/sudo", "-k", "-S", "-p", PASSWORD_PROMPT.decode("ascii"), "--", "/usr/bin/taskset", "-c", "1-3,7",
        "/usr/bin/nice", "-n", "10", "/usr/bin/python3", "-B", "-u", "-c", source,
        "--dof", str(dof), "--session-id", session_id,
    ])


def helper_source(mode):
    if mode not in ("query", "passive"):
        raise ValueError("反馈读取方式无效")
    source = Path(__file__).with_name("postzero_reader.py").read_text(encoding="utf-8")
    if mode == "query":
        return source
    passive = Path(__file__).with_name("postzero_passive.py").read_text(encoding="utf-8")
    # Register only the bundled read-only support module; no robot project imports.
    return (
        "import sys, types\n"
        "support = types.ModuleType('postzero_reader')\n"
        "sys.modules['postzero_reader'] = support\n"
        f"exec(compile({source!r}, '<postzero-support>', 'exec'), support.__dict__)\n"
        "namespace = {'__name__': 'postzero_passive_capture', '__package__': ''}\n"
        f"exec(compile({passive!r}, '<postzero-passive>', 'exec'), namespace)\n"
        "raise SystemExit(namespace['main']())\n"
    )


def capture_postzero(connection, password, dof, session_id, on_event, cancel_event, *, mode="query"):
    """Call only after the independent dialog's explicit approval; never starts on import.

    The password exists only in the SSH authentication call and sudo stdin. No PTY
    is allocated and remote stderr is counted and discarded, never shown to users.
    """
    if not isinstance(connection, dict) or connection.get("connected") is not True:
        raise ValueError("需要已绑定机器人的现有SSH连接")
    username, host = parse_ssh_target(connection.get("target", ""))
    port = connection.get("ssh_port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SSH端口无效")
    known_hosts = Path(connection.get("known_hosts", "")).expanduser()
    if not known_hosts.is_file():
        raise ValueError("缺少已核对的SSH主机记录，请先通过设备连接核对身份")
    if (not isinstance(password, str) or not password or len(password) > 4096
            or any(char in password for char in "\r\n\0")):
        raise ValueError("复查需要本次SSH和sudo密码")
    source = helper_source(mode)
    command = build_remote_command(source, dof, session_id)
    if cancel_event.is_set():
        return {"cancelled": True, "completed": False}
    import paramiko

    client, channel, stdin = None, None, None
    try:
        client = paramiko.SSHClient()
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(hostname=host, port=port, username=username, password=password,
                       look_for_keys=False, allow_agent=False, timeout=5, banner_timeout=5, auth_timeout=8)
        if cancel_event.is_set():
            return {"cancelled": True, "completed": False}
        stdin, stdout, _ = client.exec_command(command, get_pty=False, timeout=5)
        channel = stdout.channel
        buffer, stdout_size, stderr_size, completed = bytearray(), 0, 0, False
        remote_cancelled = False
        prompt_tail, password_sent, helper_started = b"", False, False
        deadline = time.monotonic() + CAPTURE_TIMEOUT_SECONDS
        while True:
            if cancel_event.is_set():
                if helper_started or password_sent:
                    try:
                        stdin.write("STOP\n")
                        stdin.flush()
                    except Exception:
                        pass
                return {"cancelled": True, "completed": False}
            if time.monotonic() > deadline:
                raise PostzeroCaptureError("角度复查超时，已关闭专用SSH通道")
            if channel.recv_ready():
                chunk = channel.recv(65536)
                stdout_size += len(chunk)
                if stdout_size > MAX_STDOUT_BYTES:
                    raise PostzeroCaptureError("角度复查输出超过512KiB限制")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    try:
                        event = read_event(bytes(line))
                    except ValueError:
                        raise PostzeroCaptureError("远端复查JSONL格式无效") from None
                    if event["session_id"] != session_id:
                        raise PostzeroCaptureError("角度复查返回了其他会话的数据")
                    if event["type"] == "metadata" and event.get("dof") != dof:
                        raise PostzeroCaptureError("角度复查返回的机型不匹配")
                    if event["type"] == "metadata" and event.get("mode", "query") != mode:
                        raise PostzeroCaptureError("反馈读取方式与本次选择不匹配")
                    try:
                        on_event(event)
                    except ValueError:
                        raise PostzeroCaptureError("复查事件未通过绑定或质量校验") from None
                    completed = completed or event["type"] == "complete"
                    if event["type"] == "complete":
                        remote_cancelled = event.get("cancelled") is True
                    helper_started = True
                if len(buffer) > MAX_LINE_BYTES:
                    raise PostzeroCaptureError("角度复查单行输出超过64KiB限制")
            if channel.recv_stderr_ready():
                stderr_chunk = channel.recv_stderr(8192)
                stderr_size += len(stderr_chunk)
                if stderr_size > MAX_STDERR_BYTES:
                    raise PostzeroCaptureError("远端诊断输出超过8KiB限制，已关闭复查")
                window = prompt_tail + stderr_chunk
                prompts = window.count(PASSWORD_PROMPT)
                if prompts:
                    if password_sent or prompts != 1:
                        raise PostzeroCaptureError("sudo认证未通过，已关闭复查；不会重试密码")
                    stdin.write(password + "\n")
                    stdin.flush()
                    password, password_sent = "", True
                prompt_tail = window[-(len(PASSWORD_PROMPT) - 1):]
                stderr_chunk = window = b""
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            cancel_event.wait(0.02)
        if buffer:
            raise PostzeroCaptureError("角度复查输出未形成完整JSONL记录")
        if channel.recv_exit_status() != 0:
            raise PostzeroCaptureError("远端复查未成功结束；请核对SSH、sudo授权及现场状态")
        if not completed:
            raise PostzeroCaptureError("角度复查缺少完成事件")
        return {"cancelled": remote_cancelled, "completed": True}
    except PostzeroCaptureError:
        raise
    except Exception:
        raise ValueError("专用SSH角度复查失败；请核对主机身份、密码、sudo授权和连接状态") from None
    finally:
        password = ""
        for resource in (channel, stdin, client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
