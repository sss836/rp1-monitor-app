"""PC SSH/relay and recording controls with PostgreSQL history; no robot control."""

import fcntl
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from .backend import BackendSettings, create_app
from .cli import private_write
from .models import DeviceRegistration, PreviewFrame
from .relay import RelaySettings, create_relay_app, read_json
from .ssh_transport import HostKeyRequired, SshTunnel, parse_ssh_target
from .storage import robots, token_digest

DEFAULT_PORT = 18980


def prepare_state(directory):
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_mode & 0o077 or directory.stat().st_uid != os.getuid():
        raise ValueError("快捷连接目录必须归当前用户所有且权限为0700")
    marker = directory / "station-owner.json"
    if not marker.exists():
        if any(directory.iterdir()):
            raise ValueError("快捷连接只使用新的专用目录，拒绝接管已有数据库或运行目录")
        private_write(marker, '{"kind":"rp1-local-station","schema":1}\n')
    if json.loads(marker.read_text()) != {"kind": "rp1-local-station", "schema": 1}:
        raise ValueError("快捷连接目录身份不匹配")
    for name, content in [("viewer-token.txt", secrets.token_urlsafe(32)),
                          ("station-token.txt", secrets.token_urlsafe(32)),
                          ("devices.json", "[]")]:
        path = directory / name
        if not path.exists():
            private_write(path, content + "\n")
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("快捷连接凭据文件必须为私有普通文件")
    return directory


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=4096)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    collector_port: int = Field(default=8891, ge=1, le=65535)
    confirmed_fingerprint: str = Field(default="", max_length=128)


class StationManager:
    def __init__(self, directory, central_url, store, tunnel_factory=None):
        self.directory, self.central_url, self.store = directory, central_url, store
        self.tunnel_factory = tunnel_factory or SshTunnel.connect
        self.lock = threading.Lock()
        self.tunnel = self.relay_server = self.relay_thread = None
        self.identity = None
        self.target = ""
        self.relay_url = ""

    def status(self):
        connected = bool(self.tunnel and self.tunnel.transport.is_active())
        return {"connected": connected, "robot": self.identity, "target": self.target,
                "relay_url": self.relay_url, "central_url": self.central_url,
                "mode": "local_station", "robot_modified": False}

    def connect(self, query):
        parse_ssh_target(query.target)
        if not self.lock.acquire(blocking=False):
            raise ValueError("已有连接任务，请等待完成后再操作")
        candidate = None
        try:
            candidate = self.tunnel_factory(
                query.target, query.password.get_secret_value(), self.directory / "known_hosts",
                ssh_port=query.ssh_port, collector_port=query.collector_port,
                confirmed_fingerprint=query.confirmed_fingerprint,
            )
            try:
                with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                    report = read_json(client, "GET", candidate.url + "/api/v1/doctor")
                    frame = PreviewFrame.model_validate(report["snapshot"])
                    identity = {"robot_id": frame.robot_id, "source_kind": frame.source_kind}
                    if frame.upload.get("stage") != "robot_buffer":
                        raise ValueError("目标端口不是本项目机器人采集端")
            except (httpx.HTTPError, ValueError, KeyError):
                raise ValueError("SSH已连接，但未读到正确采集端；请确认机器人监听程序已开启及8891端口") from None
            if self.store.engine.dialect.name != "postgresql" and identity["source_kind"] == "ros2":
                raise ValueError("真机数据应统一写入PostgreSQL，请先配置长期历史库；旧SQLite历史保留，不会迁移或删除")
            # Protect the already deployed local receiver from a second ACK consumer.
            with httpx.Client(timeout=0.5, trust_env=False, follow_redirects=False) as client:
                for port in (18972, 8892):
                    try:
                        other = read_json(client, "GET", f"http://127.0.0.1:{port}/api/v1/doctor")["snapshot"]
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        continue
                    if (other.get("robot_id"), other.get("source_kind"), other.get("upload", {}).get("stage")) == (
                        identity["robot_id"], identity["source_kind"], "upper_computer"
                    ):
                        raise ValueError(f"本机已有该机器人的relay（{port}），请连接已有relay；迁移前先保留并交完旧队列，避免两个接收器争用摘要")
            devices = json.loads((self.directory / "devices.json").read_text())
            device = next((d for d in devices if d["robot_id"] == identity["robot_id"]), None)
            if device and device["source_kind"] != identity["source_kind"]:
                raise ValueError("同一机器人编号的数据来源改变，拒绝混入已有历史")
            with self.store.engine.connect() as connection:
                registered = connection.execute(select(robots.c.token_digest, robots.c.source_kind).where(
                    robots.c.robot_id == identity["robot_id"])).first()
            if registered and (not device or registered.token_digest != token_digest(device["token"])):
                raise ValueError("PostgreSQL已有该机器人登记，但本机缺少匹配设备凭据；请复用既有relay或由管理员导入绑定凭据，不会自动覆盖")
            if device is None:
                registration = DeviceRegistration(
                    **identity, name=identity["robot_id"], token=secrets.token_urlsafe(32),
                )
                device = registration.model_dump(mode="json", exclude={"token"})
                device["token"] = registration.token.get_secret_value()
                devices.append(device)
                with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, prefix="devices-", suffix=".tmp", delete=False) as stream:
                    stream.write(json.dumps(devices, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                Path(stream.name).replace(self.directory / "devices.json")
            self.store.configure([device], authoritative=False)
            self._stop_connection()
            self.tunnel, candidate = candidate, None
            self.identity, self.target = identity, query.target.strip()
            self._start_relay(device)
            return {"status": "connected", **self.status()}
        except Exception:
            if self.tunnel is not None and self.relay_server is None:
                self._stop_connection()
            raise
        finally:
            if candidate:
                candidate.close()
            self.lock.release()

    def _start_relay(self, device):
        import uvicorn

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.relay_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_relay_app(RelaySettings(
            robot_id=device["robot_id"], source_kind=device["source_kind"],
            collector_url=self.tunnel.url, backend_url=self.central_url, token=device["token"],
            state_dir=self.directory / "relay" / device["robot_id"],
            cache_bytes=256 * 1024**2, reserve_bytes=512 * 1024**2,
        ))
        self.relay_server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
        self.relay_thread = threading.Thread(
            target=self.relay_server.run, kwargs={"sockets": [sock]}, daemon=True,
        )
        self.relay_thread.start()
        deadline = time.monotonic() + 8
        while not self.relay_server.started:
            if not self.relay_thread.is_alive() or time.monotonic() > deadline:
                raise ValueError("电脑relay启动失败，请查看快捷连接日志")
            time.sleep(0.05)

    def _stop_connection(self):
        if self.relay_server:
            self.relay_server.should_exit = True
            if self.relay_thread:
                self.relay_thread.join(timeout=20)
                if self.relay_thread.is_alive():
                    raise ValueError("旧relay仍在退出，请稍后重试；未启动第二个接收器")
            self.relay_server = self.relay_thread = None
        if self.tunnel:
            self.tunnel.close()
            self.tunnel = None
        self.identity = None
        self.target = self.relay_url = ""

    def disconnect(self):
        with self.lock:
            self._stop_connection()


def create_station_app(directory, port=DEFAULT_PORT, *, tunnel_factory=None):
    directory = prepare_state(directory)
    viewer = (directory / "viewer-token.txt").read_text().strip()
    control = (directory / "station-token.txt").read_text().strip()
    central_url = f"http://127.0.0.1:{port}"

    def reader(request: Request, authorization: Annotated[str | None, Header()] = None):
        supplied = (authorization or "").removeprefix("Bearer ")
        cookie = request.cookies.get("rp1_station_viewer", "")
        if not (hmac.compare_digest(supplied, viewer) or hmac.compare_digest(cookie, viewer)):
            raise HTTPException(401, "需要本机快捷连接凭据")

    database_file = directory / "postgres-url.txt"
    database_url = f"sqlite:///{directory / 'monitor.sqlite'}"
    if database_file.exists():
        if database_file.is_symlink() or database_file.stat().st_mode & 0o077:
            raise ValueError("PostgreSQL连接文件必须为0600私有普通文件")
        database_url = database_file.read_text().strip().replace("postgresql://", "postgresql+psycopg://", 1)
        if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("长期历史库只接受PostgreSQL连接")
    from .recording_api import register_recordings
    from .recordings import Recorder, relay_frame

    app = create_app(BackendSettings(
        database_url=database_url, initialize=not database_file.exists(),
        device_config=directory / "devices.json", viewer_token=viewer, database_managed=True,
    ), read_dependency=reader)
    manager = StationManager(directory, central_url, app.state.store, tunnel_factory)
    app.state.station = manager
    def recording_source():
        if manager.relay_server is not None:
            try:
                return manager.relay_server.config.app.state.runtime.snapshot().model_dump(mode="json")
            except (HTTPException, AttributeError):
                return None
        # Reuse an existing receiver by GET only, never take its transfer queue.
        for receiver_port in (18972, 8892):
            frame = relay_frame(f"http://127.0.0.1:{receiver_port}")
            if frame:
                return frame
        return None

    def record_control(request: Request):
        if not hmac.compare_digest(request.headers.get("x-rp1-station-token", ""), control):
            raise HTTPException(403, "需要本机监控记录操作凭据")

    recorder = Recorder(app.state.store, directory / "recording-outbox.sqlite", recording_source)
    app.state.recorder = recorder
    register_recordings(app, recorder, recording_source, reader, record_control)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            recorder.start_thread()
            try:
                yield
            finally:
                recorder.close()
                manager.disconnect()

    app.router.lifespan_context = lifespan

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(status_code=422, content={"detail": "参数无效，请核对地址、编号、时间范围、保存指标和频率"})

    @app.middleware("http")
    async def station_origin(request, call_next):
        if request.headers.get("host") != f"127.0.0.1:{port}":
            return JSONResponse(status_code=403, content={"detail": "只允许本机快捷连接地址"})
        if request.url.path.startswith("/api/v1/station"):
            origin = request.headers.get("origin")
            if origin not in (None, central_url):
                return JSONResponse(status_code=403, content={"detail": "只允许本机同源连接"})
            if request.method != "GET" and origin is not None:
                return JSONResponse(status_code=403, content={"detail": "网页仅展示数据，连接和采集操作请使用原生上位机"})
            if request.method != "GET" and not hmac.compare_digest(
                request.headers.get("x-rp1-station-token", ""), control
            ):
                return JSONResponse(status_code=403, content={"detail": "连接请求未通过本机校验"})
        response = await call_next(request)
        workspace_page = getattr(request.scope.get("route"), "name", "") == "robot_workspace"
        if (request.method == "GET" and response.status_code == 200
                and (request.url.path in ("/recordings", "/robots", "/") or workspace_page)):
            response.set_cookie("rp1_station_viewer", viewer, httponly=True, samesite="strict")
        if request.url.path.startswith(("/connect", "/recordings", "/api/v1/station")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/connect", include_in_schema=False)
    def connect_page():
        response = RedirectResponse("/robots", status_code=307)
        response.set_cookie("rp1_station_viewer", viewer, httponly=True, samesite="strict")
        return response

    @app.get("/api/v1/station", dependencies=[Depends(reader)])
    def state():
        return {**manager.status(), "recording": recorder.state()}

    @app.post("/api/v1/station/connect", dependencies=[Depends(reader)])
    def connect_robot(query: ConnectRequest):
        try:
            return manager.connect(query)
        except HostKeyRequired as exc:
            return {"status": "host_key_required", "host": exc.host, "fingerprint": exc.fingerprint}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(503, "连接暂不可用，请检查电脑后台状态后重试") from None

    @app.post("/api/v1/station/disconnect", dependencies=[Depends(reader)])
    def disconnect_robot():
        manager.disconnect()
        return manager.status()

    return app


def run_station(directory, port):
    import uvicorn

    directory = prepare_state(directory)
    with (directory / "station.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("该快捷连接后台已运行，请勿重复启动") from None
        uvicorn.run(create_station_app(directory, port), host="127.0.0.1", port=port, access_log=False)


def ensure_station(directory, port=DEFAULT_PORT):
    directory = prepare_state(directory)
    url = f"http://127.0.0.1:{port}"
    viewer = (directory / "viewer-token.txt").read_text().strip()

    def ready():
        try:
            with httpx.Client(timeout=0.5, trust_env=False) as client:
                response = client.get(url + "/api/v1/station", headers={"Authorization": f"Bearer {viewer}"})
                if response.status_code != 200:
                    raise ValueError("快捷连接端口被其他服务占用，请指定空闲端口")
                return response.json().get("mode") == "local_station"
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    if ready():
        return url, directory / "viewer-token.txt"
    with (directory / "station.log").open("ab") as log:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        process = subprocess.Popen(
            [sys.executable, "-m", "rp1_monitor", "station", "--state-dir", str(directory), "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, env=env,
            cwd=directory,
        )
    for _ in range(60):
        if ready():
            return url, directory / "viewer-token.txt"
        if process.poll() is not None:
            raise ValueError("快捷连接后台启动失败，请查看专用目录station.log")
        time.sleep(0.1)
    raise ValueError("快捷连接后台尚未就绪，请查看station.log后重试")
