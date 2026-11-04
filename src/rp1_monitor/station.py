"""PC SSH/relay, recordings, and native motor parameter maintenance."""

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
from dataclasses import dataclass, field
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
from .models import DeviceRegistration, PreviewFrame, RobotId
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


class DisconnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    robot_id: RobotId | None = None


class MotorParameterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    robot_id: RobotId
    relay_url: str = Field(min_length=10, max_length=256)
    request: dict = Field(max_length=12)
    sudo_password: SecretStr = Field(min_length=1, max_length=4096)


@dataclass
class StationConnection:
    tunnel: object
    identity: dict
    target: str
    collector: tuple
    relay_server: object = None
    relay_thread: object = None
    relay_url: str = ""
    deploy_lock: object = field(default_factory=threading.Lock)


class StationManager:
    def __init__(self, directory, central_url, store, tunnel_factory=None):
        self.directory, self.central_url, self.store = directory, central_url, store
        self.tunnel_factory = tunnel_factory or SshTunnel.connect
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.connections = {}
        self.selected_robot_id = None

    def _selected_connection(self):
        with self.state_lock:
            return self.connections.get(self.selected_robot_id)

    # Keep the original single-connection attributes as the latest selected projection.
    @property
    def tunnel(self):
        connection = self._selected_connection()
        return connection.tunnel if connection else None

    @property
    def relay_server(self):
        connection = self._selected_connection()
        return connection.relay_server if connection else None

    @property
    def relay_thread(self):
        connection = self._selected_connection()
        return connection.relay_thread if connection else None

    @property
    def identity(self):
        connection = self._selected_connection()
        return connection.identity if connection else None

    @property
    def target(self):
        connection = self._selected_connection()
        return connection.target if connection else ""

    @property
    def relay_url(self):
        connection = self._selected_connection()
        return connection.relay_url if connection else ""

    def _projection(self, connection):
        connected = bool(connection and connection.tunnel and connection.tunnel.transport.is_active())
        return {"connected": connected, "robot": dict(connection.identity) if connection else None,
                "target": connection.target if connection else "",
                "ssh_port": connection.collector[1] if connection else None,
                "relay_url": connection.relay_url if connection else "",
                "central_url": self.central_url, "mode": "local_station", "robot_modified": False}

    def status(self, robot_id=None):
        with self.state_lock:
            connection = self.connections.get(robot_id if robot_id is not None else self.selected_robot_id)
            return {**self._projection(connection),
                    "connections": [self._projection(item) for item in self.connections.values()]}

    def connect(self, query):
        _, host = parse_ssh_target(query.target)
        collector = (host.lower(), query.ssh_port, query.collector_port)
        if not self.lock.acquire(blocking=False):
            raise ValueError("已有连接任务，请等待完成后再操作")
        candidate = connection = None
        try:
            with self.state_lock:
                if any(item.collector == collector for item in self.connections.values()):
                    raise ValueError("该采集端已有连接，请先断开对应机器人，避免两个接收器争用摘要")
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
            with self.state_lock:
                if identity["robot_id"] in self.connections:
                    raise ValueError("该机器人已有连接，请先断开原连接，避免两个接收器争用摘要")
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
            with self.store.engine.connect() as database:
                registered = database.execute(select(robots.c.token_digest, robots.c.source_kind).where(
                    robots.c.robot_id == identity["robot_id"])).first()
            if registered and registered.source_kind != identity["source_kind"]:
                raise ValueError("同一机器人编号的数据来源改变，拒绝混入已有历史")
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
            connection = StationConnection(candidate, identity, query.target.strip(), collector)
            candidate = None
            try:
                self._start_relay(connection, device)
            except Exception:
                try:
                    self._stop_connection(connection)
                except Exception:
                    # A receiver still exiting remains reserved; never start another ACK consumer.
                    with self.state_lock:
                        self.connections[identity["robot_id"]] = connection
                    raise
                raise
            with self.state_lock:
                self.connections[identity["robot_id"]] = connection
                self.selected_robot_id = identity["robot_id"]
            return {"status": "connected", **self.status(identity["robot_id"])}
        finally:
            if candidate:
                candidate.close()
            self.lock.release()

    def _start_relay(self, connection, device):
        import uvicorn

        cache_root = self.directory / "relay"
        state_dir = cache_root / device["robot_id"]
        if (cache_root.resolve() != self.directory.resolve() / "relay"
                or state_dir.resolve().parent != cache_root.resolve()):
            raise ValueError("relay缓存目录必须位于本机专用目录内")
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            connection.relay_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = create_relay_app(RelaySettings(
                robot_id=device["robot_id"], source_kind=device["source_kind"],
                collector_url=connection.tunnel.url, backend_url=self.central_url, token=device["token"],
                state_dir=state_dir,
                cache_bytes=256 * 1024**2, reserve_bytes=512 * 1024**2,
            ))
            connection.relay_server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
            connection.relay_thread = threading.Thread(
                target=connection.relay_server.run, kwargs={"sockets": [sock]}, daemon=True,
            )
            connection.relay_thread.start()
            deadline = time.monotonic() + 8
            while not connection.relay_server.started:
                if not connection.relay_thread.is_alive() or time.monotonic() > deadline:
                    raise ValueError("电脑relay启动失败，请查看快捷连接日志")
                time.sleep(0.05)
        except Exception:
            sock.close()
            raise

    def _stop_connection(self, connection=None):
        connection = connection or self._selected_connection()
        if connection is None:
            return
        if not connection.deploy_lock.acquire(blocking=False):
            raise ValueError("电机维护尚未结束，不能中断其SSH连接；请等待操作回执")
        try:
            self._close_connection(connection)
        finally:
            connection.deploy_lock.release()

    def _close_connection(self, connection):
        if connection.relay_server:
            connection.relay_server.should_exit = True
            if connection.relay_thread:
                connection.relay_thread.join(timeout=20)
                if connection.relay_thread.is_alive():
                    raise ValueError("旧relay仍在退出，请稍后重试；未启动第二个接收器")
            connection.relay_server = connection.relay_thread = None
        if connection.tunnel:
            connection.tunnel.close()
            connection.tunnel = None
        robot_id = connection.identity["robot_id"]
        with self.state_lock:
            if self.connections.get(robot_id) is connection:
                del self.connections[robot_id]
            if self.selected_robot_id == robot_id:
                self.selected_robot_id = next(reversed(self.connections), None)

    def _deploy_connection(self, robot_id, relay_url):
        connection = self.connections.get(robot_id)
        if (not connection or connection.relay_url != relay_url or connection.identity.get("source_kind") != "ros2"
                or not connection.tunnel or not connection.tunnel.transport.is_active()
                or parse_ssh_target(connection.target)[0] != "robo"):
            raise ValueError("所选机器人没有对应的已认证SSH连接，未发送启动命令")
        return connection

    def deploy_connection(self, robot_id, relay_url):
        from .factory_client import FactoryClient

        with self.state_lock:
            connection = self._deploy_connection(robot_id, relay_url)
            tunnel, target = connection.tunnel, connection.target
        identity = FactoryClient(tunnel.url).probe()
        if identity.get("robot_id") != robot_id or identity.get("source_kind") != "ros2":
            raise ValueError("已有SSH连接的采集身份已变化，未发送启动命令")
        channel = tunnel.transport.open_session(timeout=5)
        try:
            channel.get_pty(term="xterm", width=120, height=32)
        finally:
            channel.close()
        return dict(robot_id=robot_id, source_kind="ros2", target=target, ssh_reused=True)

    def motor_parameters(self, query):
        from .motor_parameters import perform
        from .motor_parameters_remote import validate_request

        request = validate_request(query.request)
        if request["robot_id"] != query.robot_id:
            raise ValueError("参数请求机器人身份不匹配")
        with self.state_lock:
            connection = self._deploy_connection(query.robot_id, query.relay_url)
            if not connection.deploy_lock.acquire(blocking=False):
                raise ValueError("已有电机维护任务进行中，请等待结果")
        try:
            context = dict(robot_id=query.robot_id, target=connection.target, tunnel=connection.tunnel)
            return perform(context, query.sudo_password.get_secret_value(), request)
        finally:
            connection.deploy_lock.release()

    def disconnect(self, robot_id=None):
        with self.lock:
            with self.state_lock:
                if robot_id is None:
                    if len(self.connections) > 1:
                        raise ValueError("存在多个机器人连接，请指定要断开的robot_id")
                    connection = next(iter(self.connections.values()), None)
                else:
                    connection = self.connections.get(robot_id)
                    if connection is None:
                        raise ValueError("指定机器人没有本机连接")
            self._stop_connection(connection)

    def close(self):
        """Shutdown explicitly closes every managed receiver, even if another cannot exit."""
        with self.lock:
            with self.state_lock:
                connections = list(self.connections.values())
            errors = []
            for connection in connections:
                try:
                    self._stop_connection(connection)
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise errors[0]

    def recording_source(self, robot_id=None):
        with self.state_lock:
            managed = bool(self.connections)
            if robot_id is None:
                connection = next(iter(self.connections.values())) if len(self.connections) == 1 else None
            else:
                connection = self.connections.get(robot_id)
        if managed and (robot_id is None or connection is not None):
            if connection is None or connection.relay_server is None:
                return None
            try:
                frame = connection.relay_server.config.app.state.runtime.snapshot().model_dump(mode="json")
                if (frame.get("robot_id"), frame.get("source_kind"), frame.get("upload", {}).get("stage")) == (
                    connection.identity["robot_id"], connection.identity["source_kind"], "upper_computer"
                ):
                    return frame
            except (HTTPException, AttributeError, ValueError):
                pass
            return None
        # A specific legacy robot may still use its existing receiver alongside managed peers.
        from .recordings import relay_frame

        frames = []
        for receiver_port in (18972, 8892):
            frame = relay_frame(f"http://127.0.0.1:{receiver_port}")
            if frame and (robot_id is None or frame.get("robot_id") == robot_id):
                if robot_id is not None:
                    return frame
                frames.append(frame)
        return frames[0] if len(frames) == 1 else None


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
    from .fleet_recordings import FleetRecorder
    from .recording_api import register_recordings

    app = create_app(BackendSettings(
        database_url=database_url, initialize=not database_file.exists(),
        device_config=directory / "devices.json", viewer_token=viewer, database_managed=True,
    ), read_dependency=reader)
    manager = StationManager(directory, central_url, app.state.store, tunnel_factory)
    app.state.station = manager
    recording_source = manager.recording_source

    def record_control(request: Request):
        if not hmac.compare_digest(request.headers.get("x-rp1-station-token", ""), control):
            raise HTTPException(403, "需要本机监控记录操作凭据")

    def recording_roster():
        return [{**row["robot"], "connected": row["connected"]}
                for row in manager.status()["connections"]]

    recorder = FleetRecorder(app.state.store, directory, recording_source, recording_roster)
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
                try:
                    recorder.close()
                finally:
                    manager.close()

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
    def state(robot_id: RobotId | None = None):
        return {**manager.status(robot_id), "recording": recorder.state(robot_id)}

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
    def disconnect_robot(query: DisconnectRequest | None = None):
        robot_id = query.robot_id if query else None
        try:
            manager.disconnect(robot_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return manager.status(robot_id)

    @app.get("/api/v1/station/deploy-connection", dependencies=[Depends(reader)])
    def deploy_connection(robot_id: RobotId, relay_url: str):
        try:
            return manager.deploy_connection(robot_id, relay_url)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/api/v1/station/motor-parameters", dependencies=[Depends(reader)])
    def motor_parameters(query: MotorParameterRequest):
        try:
            return manager.motor_parameters(query)
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from None
        except Exception:
            raise HTTPException(503, "参数操作回执未确认，请重新读取实际值；不会自动重复写入") from None

    @app.post("/api/v1/station/deploy-launches", dependencies=[Depends(reader)])
    def deploy_launch_disabled():
        raise HTTPException(410, "上位机策略启动已移除，请到机器人端手动启动deploy；本页仅记录和读取测试数据")

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
