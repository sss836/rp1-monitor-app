"""Password SSH forwarding on the PC. Never opens a shell or executes robot commands."""

import base64
import hashlib
import hmac
import re
import select
import shlex
import socket
import socketserver
import threading
from pathlib import Path


def parse_ssh_target(value):
    parts = shlex.split(value.strip())
    if len(parts) == 2 and parts[0] == "ssh":
        parts = parts[1:]
    if len(parts) != 1 or not re.fullmatch(
        r"[a-zA-Z_][a-zA-Z0-9_.-]{0,63}@[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", parts[0]
    ):
        raise ValueError("SSH地址应为 ssh 用户名@IP 或 用户名@IP，不包含其他命令或参数")
    return parts[0].split("@", 1)


def fingerprint(key):
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class HostKeyRequired(ValueError):
    def __init__(self, host, value):
        super().__init__("首次连接，请核对机器人主机指纹后确认")
        self.host, self.fingerprint = host, value


def check_host_key(host_keys, host, key, confirmed):
    observed = fingerprint(key)
    known = host_keys.lookup(host)
    if known:
        if not host_keys.check(host, key):
            raise ValueError("机器人主机指纹与已保存记录不同，已拒绝连接；请先核实设备身份")
        return False
    if not confirmed or not hmac.compare_digest(confirmed, observed):
        raise HostKeyRequired(host, observed)
    host_keys.add(host, key.get_name(), key)
    return True


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # Expected disconnects must not print request data or authentication context.
        pass


class SshTunnel:
    def __init__(self, transport, remote_port):
        self.transport = transport
        self.remote_port = remote_port
        tunnel = self

        class Forward(socketserver.BaseRequestHandler):
            def handle(self):
                channel = None
                try:
                    channel = tunnel.transport.open_channel(
                        "direct-tcpip", ("127.0.0.1", tunnel.remote_port), self.client_address,
                        timeout=5,
                    )
                    if channel is None:
                        return
                    self.request.settimeout(5)
                    channel.settimeout(5)
                    while tunnel.transport.is_active():
                        ready, _, _ = select.select([self.request, channel], [], [], 1)
                        if self.request in ready:
                            data = self.request.recv(65536)
                            if not data:
                                break
                            channel.sendall(data)
                        if channel in ready:
                            data = channel.recv(65536)
                            if not data:
                                break
                            self.request.sendall(data)
                except (OSError, EOFError):
                    pass
                finally:
                    if channel is not None:
                        channel.close()

        self.server = _ForwardServer(("127.0.0.1", 0), Forward)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @classmethod
    def connect(cls, target, password, known_hosts: Path, *, ssh_port=22,
                collector_port=8891, confirmed_fingerprint=""):
        import paramiko

        username, host = parse_ssh_target(target)
        address = host if ssh_port == 22 else f"[{host}]:{ssh_port}"
        transport = None
        sock = None
        try:
            sock = socket.create_connection((host, ssh_port), timeout=5)
            transport = paramiko.Transport(sock)
            transport.auth_timeout = 10
            transport.banner_timeout = 5
            transport.start_client(timeout=8)
            key = transport.get_remote_server_key()
            keys = paramiko.HostKeys()
            if known_hosts.exists():
                keys.load(str(known_hosts))
            changed = check_host_key(keys, address, key, confirmed_fingerprint)
            # Password authentication is attempted only after host verification.
            transport.auth_password(username, password, fallback=True)
            if not transport.is_authenticated():
                raise ValueError("SSH认证未完成")
            if changed:
                keys.save(str(known_hosts))
                known_hosts.chmod(0o600)
            transport.set_keepalive(15)
            return cls(transport, collector_port)
        except (HostKeyRequired, ValueError):
            if transport:
                transport.close()
            if sock:
                sock.close()
            raise
        except paramiko.AuthenticationException:
            if transport:
                transport.close()
            if sock:
                sock.close()
            raise ValueError("SSH用户名或密码不正确，或机器人未允许密码登录") from None
        except (OSError, EOFError, paramiko.SSHException):
            if transport:
                transport.close()
            if sock:
                sock.close()
            raise ValueError("SSH连接失败，请检查网线、机器人IP、SSH端口和服务") from None

    def close(self):
        self.transport.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
