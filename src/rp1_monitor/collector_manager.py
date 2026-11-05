"""Native-PC SSH management for the robot-only read-only collector."""

import base64
import hashlib
import json
import socket
import stat
import time
import uuid
from pathlib import Path, PurePosixPath

from .ssh_transport import check_host_key, parse_ssh_target

ACTIONS = {"status", "install", "update", "start", "stop", "uninstall"}
MAX_BUNDLE_BYTES = 128 * 1024 * 1024


def default_bundle_path():
    path = Path(__file__).with_name("data") / "collector-bundle.zip"
    return path if path.is_file() else None


def _runtime_command():
    source = Path(__file__).with_name("collector_runtime.py").read_bytes()
    encoded = base64.b64encode(source).decode("ascii")
    # Only the constant reviewed helper is shell-quoted; requests use stdin JSON.
    code = (
        "import base64,json,sys\nnamespace={'__name__':'rp1_collector_runtime'}\n"
        f"exec(compile(base64.b64decode('{encoded}'),'<rp1-collector-manager>','exec'),namespace)\n"
        "try:\n"
        " request=json.loads(sys.stdin.read())\n result=namespace['main'](request)\n"
        "except Exception as error:\n result={'ok':False,'error':str(error)}\n"
        "print(json.dumps(result,ensure_ascii=False))\n"
    )
    import shlex

    return "python3 -B -c " + shlex.quote(code)


class CollectorManager:
    """No credentials are persisted; no control of deploy, TUI or motors."""

    def __init__(self, target, password, known_hosts, *, confirmed_fingerprint=""):
        self.username, self.host = parse_ssh_target(target)
        self.password = password
        self.known_hosts = Path(known_hosts)
        self.confirmed_fingerprint = confirmed_fingerprint
        self.transport = None

    def _connect(self):
        if self.transport is not None and self.transport.is_active():
            return
        import paramiko

        connection = None
        try:
            connection = socket.create_connection((self.host, 22), timeout=8)
            transport = paramiko.Transport(connection)
            self.transport = transport
            transport.auth_timeout = 10
            transport.start_client(timeout=8)
            keys = paramiko.HostKeys()
            if self.known_hosts.is_file():
                keys.load(str(self.known_hosts))
            changed = check_host_key(
                keys, self.host, transport.get_remote_server_key(), self.confirmed_fingerprint,
            )
            try:
                transport.auth_password(self.username, self.password, fallback=True)
            except paramiko.BadAuthenticationType:
                raise ValueError("机器人未允许当前 SSH 密码登录方式；尚未执行采集端操作。请核对机器人 SSH 登录设置") from None
            except paramiko.AuthenticationException:
                raise ValueError("SSH 登录认证失败：机器人未接受本次用户名或密码。请重新输入 SSH 密码后刷新状态；尚未执行采集端操作") from None
            if not transport.is_authenticated():
                raise ValueError("SSH认证失败")
            if changed:
                self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
                keys.save(str(self.known_hosts))
                self.known_hosts.chmod(0o600)
            transport.set_keepalive(10)
        except Exception:
            if self.transport is not None:
                self.transport.close()
                self.transport = None
            if connection is not None:
                connection.close()
            raise

    def _upload(self, bundle, robot_id):
        import paramiko

        path = Path(bundle)
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BUNDLE_BYTES:
            raise ValueError("请选择128MiB以内的采集端离线ZIP安装包")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        bundle_id = uuid.uuid4().hex
        with paramiko.SFTPClient.from_transport(self.transport) as sftp:
            home = PurePosixPath(sftp.normalize("."))
            if not home.is_absolute() or str(home) == "/":
                raise ValueError("无法确认机器人普通用户主目录")
            parent = home
            for name in (".cache", "rp1-collector-upload"):
                parent /= name
                try:
                    mode = sftp.lstat(str(parent)).st_mode
                except FileNotFoundError:
                    sftp.mkdir(str(parent), mode=0o700)
                    mode = sftp.lstat(str(parent)).st_mode
                if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                    raise ValueError("机器人上传缓存路径不是独立目录")
            marker = str(parent / ".rp1-collector-owner.json")
            entries = sftp.listdir(str(parent))
            if not entries:
                with sftp.file(marker, "wx") as output:
                    output.write(json.dumps({"schema": 1, "product": "rp1-monitor-robot",
                                             "robot_id": robot_id}).encode("utf-8"))
                sftp.chmod(marker, 0o600)
            elif ".rp1-collector-owner.json" in entries:
                if not stat.S_ISREG(sftp.lstat(marker).st_mode):
                    raise ValueError("机器人上传缓存标记不是普通文件")
                with sftp.file(marker, "rb") as source:
                    owner = json.loads(source.read(16384))
                if (owner.get("schema") != 1 or owner.get("product") != "rp1-monitor-robot"
                        or owner.get("robot_id") != robot_id):
                    raise ValueError("上传缓存绑定的机器人编号不一致，请刷新状态核对")
            remote = str(parent / (bundle_id + ".zip"))
            # Exclusive creation prevents accidental replacement of any remote file.
            with sftp.file(remote, "wx") as output, path.open("rb") as source:
                sftp.chmod(remote, 0o600)
                while data := source.read(1024 * 1024):
                    output.write(data)
        return {"bundle_id": bundle_id, "bundle_sha256": digest}

    def _request(self, request):
        channel = self.transport.open_session(timeout=10)
        raw_out, raw_err = bytearray(), bytearray()
        try:
            channel.exec_command(_runtime_command())
            channel.sendall(json.dumps(request).encode("utf-8"))
            channel.shutdown_write()
            deadline = time.monotonic() + (240 if request["action"] in {"install", "update"} else 45)
            while True:
                if channel.recv_ready():
                    raw_out.extend(channel.recv(65536))
                if channel.recv_stderr_ready():
                    raw_err.extend(channel.recv_stderr(65536))
                if len(raw_out) + len(raw_err) > 2 * 1024 * 1024:
                    raise ValueError("采集端管理响应超过上限，请检查机器人安装日志")
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() > deadline:
                    raise ValueError("SSH操作超时，机器人最终状态未知；请刷新状态后再操作")
                time.sleep(0.02)
            code = channel.recv_exit_status()
            if code:
                detail = raw_err.decode("utf-8", errors="replace").strip()[-1600:]
                raise ValueError("采集端操作未完成：" + (detail or f"远程退出码{code}"))
            try:
                result = json.loads(raw_out)
            except (ValueError, UnicodeDecodeError) as error:
                raise ValueError("机器人未返回有效管理结果，请刷新状态") from error
            if not isinstance(result, dict):
                raise ValueError("采集端管理响应格式错误")
            if result.get("ok") is False or result.get("error"):
                raise ValueError(str(result.get("error") or result.get("message") or "操作失败"))
            return result
        finally:
            channel.close()

    def run(self, action, bundle_path=None, robot_id="", confirm_delete=False):
        if action not in ACTIONS:
            raise ValueError("不支持的采集端操作")
        if action == "uninstall" and confirm_delete is not True:
            raise ValueError("卸载需确认删除机器人内本工具的全部数据")
        request = {"action": action}
        # Discovery must work even when the operator entered a different sample SN.
        if robot_id and action != "status":
            request["robot_id"] = robot_id
        if action == "uninstall":
            request["confirm_delete"] = True
        bundle = None
        if action in {"install", "update"}:
            bundle = Path(bundle_path) if bundle_path else default_bundle_path()
            if bundle is None or not bundle.is_file():
                raise ValueError("当前上位机缺少采集端离线安装包，请选择collector-bundle.zip")
            if not robot_id.strip():
                raise ValueError("安装或更新前请填写机器人编号")
        self._connect()
        if bundle is not None:
            request.update(self._upload(bundle, robot_id))
        return self._request(request)

    def close(self):
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.password = ""
