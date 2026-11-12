"""Dedicated, cancellable SSH capture for motor-angle observation."""

import json
import shlex
import time
from pathlib import Path

from .calibration_mapping import version_bound_mapping
from .joint_angle_monitor import ANKLE_SKEW_NS, RAW_FLOAT32_MAX, SESSION_LIMIT_NS, valid_raw_utc
from .postzero import CONFIGURATION_SHA256, MAX_LINE_BYTES, _pairs, read_event, validate_session_id
from .ssh_transport import parse_ssh_target

MAX_STDOUT_BYTES = 512 * 1024
MAX_STDERR_BYTES = 8 * 1024
CAPTURE_TIMEOUT_SECONDS = 35
MONITOR_IDLE_SECONDS = 12
PASSWORD_PROMPT = b"RP1_POSTZERO_SUDO_PASSWORD:"
MONITOR_FIELDS = {
    "metadata": {"dof", "joint_names", "configuration_sha256"},
    "frame": {"sequence", "frame_monotonic_ns", "sweep_s", "positions_deg", "rx_monotonic_ns",
              "raw_angles_deg", "raw_rx_monotonic_ns", "raw_received_at"},
    "error": {"message"},
    "complete": {"cancelled", "reason"},
}


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
    if mode not in ("query", "passive", "joint_monitor"):
        raise ValueError("反馈读取方式无效")
    source = Path(__file__).with_name("postzero_reader.py").read_text(encoding="utf-8")
    if mode == "query":
        return source
    if mode == "joint_monitor":
        root = Path(__file__).parent
        support = {"postzero_reader": source,
                   "calibration_mapping": (root / "calibration_mapping.py").read_text(encoding="utf-8")}
        monitor = (root / "joint_angle_monitor.py").read_text(encoding="utf-8")
        return (
            "import sys, types\n"
            f"for name, source in {support!r}.items():\n"
            "    module = types.ModuleType(name)\n"
            "    sys.modules[name] = module\n"
            "    exec(compile(source, '<' + name + '>', 'exec'), module.__dict__)\n"
            "namespace = {'__name__': 'rp1_joint_angle_monitor', '__package__': ''}\n"
            f"exec(compile({monitor!r}, '<joint-angle-monitor>', 'exec'), namespace)\n"
            "raise SystemExit(namespace['main']())\n"
        )
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


class _JointMonitorEvents:
    def __init__(self, session_id, dof):
        self.session_id, self.dof = session_id, dof
        self.binding = version_bound_mapping(dof)
        self.metadata = self.completed = self.failed = False
        self.sequence = self.frame_ns = 0

    def read(self, line):
        if len(line) > MAX_LINE_BYTES or self.completed:
            raise ValueError("关节角度事件超限或会话已结束")
        event = json.loads(line, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限JSON数值")))
        if not isinstance(event, dict) or event.get("session_id") != self.session_id:
            raise ValueError("角度反馈与当前会话不匹配")
        kind = event.get("type")
        if not isinstance(kind, str) or kind not in MONITOR_FIELDS or set(event) != MONITOR_FIELDS[kind] | {
            "type", "session_id",
        }:
            raise ValueError("无法识别角度反馈事件")
        if kind == "metadata":
            if (self.metadata or self.failed or type(event.get("dof")) is not int or event["dof"] != self.dof
                    or event.get("joint_names") != self.binding["joint_names"]
                    or event.get("configuration_sha256") != CONFIGURATION_SHA256[self.dof]):
                raise ValueError("角度反馈机型或关节映射不匹配")
            self.metadata = True
        elif kind == "frame":
            self._frame(event)
        elif kind == "error":
            if self.failed or not self._valid_message(event.get("message")):
                raise ValueError("角度反馈错误格式无效")
            self.failed = True
        elif kind == "complete":
            if (type(event.get("cancelled")) is not bool or not (self.metadata or self.failed or event["cancelled"])
                    or not self._valid_message(event.get("reason"))):
                raise ValueError("角度反馈结束格式无效")
            self.completed = True
        return event

    @staticmethod
    def _valid_message(value):
        return isinstance(value, str) and 0 < len(value) <= 240 and value.isprintable()

    def _frame(self, event):
        sequence, frame_ns, sweep_s = (event.get(key) for key in ("sequence", "frame_monotonic_ns", "sweep_s"))
        positions, times = event.get("positions_deg"), event.get("rx_monotonic_ns")
        raw_angles, raw_times, raw_utc = (event.get(key) for key in (
            "raw_angles_deg", "raw_rx_monotonic_ns", "raw_received_at"))
        if (not self.metadata or self.failed or type(sequence) is not int or sequence != self.sequence + 1
                or type(frame_ns) is not int or not self.frame_ns < frame_ns < 2**63
                or type(sweep_s) not in (int, float) or not 0 < sweep_s <= MONITOR_IDLE_SECONDS
                or any(not isinstance(values, list) or len(values) != self.dof
                       for values in (positions, times, raw_angles, raw_times, raw_utc))):
            raise ValueError("关节角度帧顺序、时间或数据长度无效")
        previous_raw_stamp = self.frame_ns
        for angle, stamp, utc in zip(raw_angles, raw_times, raw_utc):
            if angle is None and stamp is None and utc is None:
                continue
            if (type(angle) not in (int, float) or not -RAW_FLOAT32_MAX <= angle <= RAW_FLOAT32_MAX
                    or type(stamp) is not int or not previous_raw_stamp < stamp <= frame_ns
                    or (frame_ns - stamp) / 1_000_000_000 > sweep_s + 1e-9 or not valid_raw_utc(utc)):
                raise ValueError("原始轴角、顺序或接收时间无效")
            previous_raw_stamp = stamp
        for row, position, stamp in zip(self.binding["rows"], positions, times):
            if position is None and stamp is None:
                continue
            source_stamps = [raw_times[index] for index in row["motor_indices"]]
            if (type(position) not in (int, float) or not -180 <= position <= 180
                    or type(stamp) is not int or None in source_stamps or stamp != min(source_stamps)
                    or max(source_stamps) - min(source_stamps) > ANKLE_SKEW_NS):
                raise ValueError("逻辑角度与本轮轴角接收时间不一致")
        self.sequence, self.frame_ns = sequence, frame_ns


def capture_joint_angles(connection, password, dof, session_id, on_event, cancel_event):
    """Observe measured joint angles until the native worker cancels the session."""
    return _capture(connection, password, dof, session_id, on_event, cancel_event, mode="joint_monitor")


def capture_postzero(connection, password, dof, session_id, on_event, cancel_event, *, mode="query"):
    """Call only after the independent dialog's explicit approval; never starts on import.

    The password exists only in the SSH authentication call and sudo stdin. No PTY
    is allocated and remote stderr is counted and discarded, never shown to users.
    """
    if mode not in ("query", "passive"):
        raise ValueError("反馈读取方式无效")
    return _capture(connection, password, dof, session_id, on_event, cancel_event, mode=mode)


def _capture(connection, password, dof, session_id, on_event, cancel_event, *, mode):
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
    if mode == "joint_monitor" and len(source.encode("utf-8")) > 96 * 1024:
        raise PostzeroCaptureError("上位机关节角度采样启动参数过大，尚未连接机器人")
    command = build_remote_command(source, dof, session_id)
    monitor = _JointMonitorEvents(session_id, dof) if mode == "joint_monitor" else None
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
        hard_deadline = deadline + SESSION_LIMIT_NS / 1_000_000_000 if monitor else deadline
        while True:
            if cancel_event.is_set():
                if helper_started or password_sent:
                    try:
                        stdin.write("STOP\n")
                        stdin.flush()
                    except Exception:
                        pass
                return {"cancelled": True, "completed": False}
            if time.monotonic() > min(deadline, hard_deadline):
                raise PostzeroCaptureError("角度采样等待超时，已关闭专用SSH通道" if monitor
                                           else "角度复查超时，已关闭专用SSH通道")
            if channel.recv_ready():
                chunk = channel.recv(65536)
                if not monitor:
                    stdout_size += len(chunk)
                    if stdout_size > MAX_STDOUT_BYTES:
                        raise PostzeroCaptureError("角度复查输出超过512KiB限制")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    try:
                        event = monitor.read(bytes(line)) if monitor else read_event(bytes(line))
                    except (ValueError, RecursionError):
                        raise PostzeroCaptureError("远端复查JSONL格式无效") from None
                    if event["session_id"] != session_id:
                        raise PostzeroCaptureError("角度复查返回了其他会话的数据")
                    if event["type"] == "metadata" and event.get("dof") != dof:
                        raise PostzeroCaptureError("角度复查返回的机型不匹配")
                    if not monitor and event["type"] == "metadata" and event.get("mode", "query") != mode:
                        raise PostzeroCaptureError("反馈读取方式与本次选择不匹配")
                    if monitor:
                        deadline = time.monotonic() + MONITOR_IDLE_SECONDS
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
                    if password_sent or prompts != 1 or (monitor and helper_started):
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
