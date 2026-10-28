"""Human management with private per-person PostgreSQL identities and append-only corrections."""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi import Path as PathParameter
from pydantic import Field, SecretStr, field_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from .models import MetricKey, MetricRollup, Model, RobotId, metric_spec
from .monitor_config import MonitoringConfiguration


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(password, encoded):
    scheme, salt, expected = encoded.split("$")
    if scheme != "scrypt":
        return False
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32
    )
    return hmac.compare_digest(actual.hex(), expected)


class AdminUser(Model):
    username: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    password_hash: SecretStr
    database_url: SecretStr

    @field_validator("password_hash")
    @classmethod
    def valid_hash(cls, value):
        import re

        if not re.fullmatch(r"scrypt\$[0-9a-f]{32}\$[0-9a-f]{64}", value.get_secret_value()):
            raise ValueError("管理员口令必须使用支持的scrypt摘要")
        return value


class AdminConfig(Model):
    users: list[AdminUser] = Field(min_length=1, max_length=100)
    device_key_directory: Path
    schema_name: str = Field(default="rp1_monitor", pattern=r"^[a-z][a-z0-9_]{0,62}$")


class Login(Model):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=12, max_length=256)


class RobotCreate(Model):
    robot_id: RobotId
    name: str = Field(min_length=1, max_length=128)
    source_kind: Literal["simulated", "ros2"]
    reason: str = Field(min_length=1, max_length=1000)
    configuration: MonitoringConfiguration | None = None


class ConfigurationSave(Model):
    expected_revision: int | None = Field(default=None, ge=1)
    configuration: MonitoringConfiguration
    reason: str = Field(min_length=1, max_length=1000)


class RobotUpdate(Model):
    expected_name: str = Field(min_length=1, max_length=128)
    expected_enabled: bool
    name: str = Field(min_length=1, max_length=128)
    enabled: bool
    reason: str = Field(min_length=1, max_length=1000)


class Correction(Model):
    metric: MetricKey
    expected_revision: int | None = Field(default=None, ge=1)
    operation: Literal["replace", "exclude", "restore"]
    replacement: MetricRollup | None = None
    reason: str = Field(min_length=1, max_length=1000)


class Administration:
    def __init__(self, path, store, demo_read=False):
        path = Path(path)
        if path.stat().st_mode & 0o077:
            raise ValueError("管理员配置必须为仅本人可读的0600文件")
        self.config = AdminConfig.model_validate_json(path.read_text())
        base = make_url(store.engine.url)
        if base.get_backend_name() != "postgresql":
            raise ValueError("管理员功能需要已执行管理员迁移的PostgreSQL")
        self.users = {}
        self.engines = {}
        database_users = set()
        for user in self.config.users:
            if user.username in self.users:
                raise ValueError("管理员用户名必须唯一")
            url = make_url(user.database_url.get_secret_value())
            if (url.get_backend_name(), url.host, url.port, url.database) != (
                "postgresql",
                base.host,
                base.port,
                base.database,
            ):
                raise ValueError("管理员必须连接同一中央PostgreSQL数据库")
            if not url.username or url.username == base.username or url.username in database_users:
                raise ValueError("每位管理员必须使用独立于API及其他管理员的数据库登录")
            database_users.add(url.username)
            self.users[user.username] = user
            self.engines[user.username] = create_engine(
                url, pool_pre_ping=True, hide_parameters=True
            )
        self.store = store
        self.demo_read = demo_read
        self.cookie_name = (
            "rp1_admin_"
            + hashlib.sha256(
                f"{base.host}:{base.port}/{base.database}/{self.config.schema_name}".encode()
            ).hexdigest()[:10]
        )
        self.sessions = {}
        self.attempts = {}
        self.lock = threading.RLock()
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        directory = self.config.device_key_directory
        if not directory.is_absolute():
            directory = path.parent / directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.stat().st_mode & 0o077:
            raise ValueError("设备部署凭据目录必须仅本人可访问（0700）")
        self.key_directory = directory

    def close(self):
        for engine in self.engines.values():
            engine.dispose()

    def statement(self, query):
        # Only the locally validated schema identifier is substituted; values remain bound.
        return text(query.replace("rp1_monitor.", self.config.schema_name + "."))

    def query(self, username, query, parameters=None, *, write=False):
        try:
            with self.engines[username].begin() as connection:
                return [
                    dict(row)
                    for row in connection.execute(
                        self.statement(query), parameters or {}
                    ).mappings()
                ]
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None)
            if state in {"40001", "23505"}:
                raise HTTPException(409, "记录已有变更或编号重复，请刷新核对后重新提交") from None
            if state == "22023":
                raise HTTPException(422, "更正内容不符合原始观测，或缺少有效原因") from None
            if state == "42501":
                raise HTTPException(403, "该管理员数据库身份没有此操作权限") from None
            raise HTTPException(503, "管理数据库暂不可用，请稍后核对记录再重试") from None

    @staticmethod
    def same_origin(request):
        if request.url.scheme != "https" and request.url.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise HTTPException(403, "管理员登录需要HTTPS或本机回环访问")
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "管理操作必须来自本站页面")

    def session(self, request):
        self.same_origin(request)
        raw = request.cookies.get(self.cookie_name, "")
        key = hashlib.sha256(raw.encode()).hexdigest()
        with self.lock:
            found = self.sessions.get(key)
            if not found or found["expires"] <= time.monotonic():
                self.sessions.pop(key, None)
                raise HTTPException(401, "请使用管理员账号登录")
            return {**found, "key": key}

    def router(self):
        router = APIRouter(prefix="/api/v1/admin", tags=["monitor-administration"])

        def authenticated(request: Request):
            return self.session(request)

        def writer(request: Request, x_csrf_token: Annotated[str | None, Header()] = None):
            self.same_origin(request)
            actor = self.session(request)
            if not x_csrf_token or not hmac.compare_digest(x_csrf_token, actor["csrf"]):
                raise HTTPException(403, "管理会话校验失败，请重新登录")
            return actor

        Human = Annotated[dict, Depends(authenticated)]
        Writer = Annotated[dict, Depends(writer)]

        @router.post("/login")
        def login(body: Login, request: Request, response: Response) -> dict:
            self.same_origin(request)
            now = time.monotonic()
            address = request.client.host if request.client else "local"
            with self.lock:
                self.attempts = {
                    key: value for key, value in self.attempts.items() if value[1] > now
                }
                attempts, until = self.attempts.get(address, (0, now + 60))
                if attempts >= 5 or len(self.attempts) >= 1024:
                    raise HTTPException(429, "登录尝试过多，请稍后再试")
                self.attempts[address] = (attempts + 1, until)
            user = self.users.get(body.username)
            valid = password_matches(
                body.password.get_secret_value(),
                user.password_hash.get_secret_value() if user else self.dummy_hash,
            )
            if not user or not valid:
                raise HTTPException(401, "用户名或密码不正确")
            # Check actual DB login and migration rights before establishing the human session.
            role = self.config.schema_name + "_admin"
            rights = self.query(
                body.username,
                """
                SELECT session_user AS login, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolbypassrls,
                  pg_catalog.pg_has_role(session_user, :role, 'MEMBER') AS is_admin,
                  (pg_catalog.pg_has_role(session_user, :executor, 'MEMBER')
                   OR pg_catalog.pg_has_role(session_user, :app, 'MEMBER')
                   OR pg_catalog.pg_has_role(session_user, :archiver, 'MEMBER')) AS unsafe_membership,
                  EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=:schema AND pg_catalog.pg_has_role(session_user,c.relowner,'MEMBER')) AS owns_objects,
                  pg_catalog.has_schema_privilege(session_user,:schema,'CREATE') AS can_create,
                  (pg_catalog.has_table_privilege(session_user,:raw,'INSERT,UPDATE,DELETE,TRUNCATE')
                   OR pg_catalog.has_table_privilege(session_user,:robots,'INSERT,UPDATE,DELETE,TRUNCATE')
                   OR pg_catalog.has_table_privilege(session_user,:corrections,'INSERT,UPDATE,DELETE,TRUNCATE')
                   OR pg_catalog.has_table_privilege(session_user,:audit,'INSERT,UPDATE,DELETE,TRUNCATE')
                   OR pg_catalog.has_any_column_privilege(session_user,:raw,'INSERT,UPDATE')
                   OR pg_catalog.has_any_column_privilege(session_user,:robots,'INSERT,UPDATE')
                   OR pg_catalog.has_any_column_privilege(session_user,:corrections,'INSERT,UPDATE')
                   OR pg_catalog.has_any_column_privilege(session_user,:audit,'INSERT,UPDATE')) AS direct_writes
                FROM pg_catalog.pg_roles r WHERE r.rolname=session_user
            """,
                {
                    "role": role,
                    "executor": role + "_executor",
                    "app": self.config.schema_name + "_app",
                    "archiver": self.config.schema_name + "_archiver",
                    "schema": self.config.schema_name,
                    "raw": self.config.schema_name + ".monitor_windows",
                    "robots": self.config.schema_name + ".monitor_robots",
                    "corrections": self.config.schema_name + ".monitor_metric_corrections",
                    "audit": self.config.schema_name + ".monitor_admin_audit",
                },
            )[0]
            if (
                rights["login"] != make_url(user.database_url.get_secret_value()).username
                or not rights["is_admin"]
                or any(
                    rights[key]
                    for key in (
                        "rolsuper",
                        "rolcreatedb",
                        "rolcreaterole",
                        "rolbypassrls",
                        "unsafe_membership",
                        "owns_objects",
                        "can_create",
                        "direct_writes",
                    )
                )
            ):
                raise HTTPException(403, "管理员数据库身份不符合最小权限要求")
            configuration_table = self.query(
                body.username,
                "SELECT pg_catalog.to_regclass(:table) IS NOT NULL AS present",
                {"table": self.config.schema_name + ".monitor_robot_config_versions"},
            )[0]["present"]
            if (
                configuration_table
                and self.query(
                    body.username,
                    """SELECT (pg_catalog.has_table_privilege(session_user,:table,'INSERT,UPDATE,DELETE,TRUNCATE')
                  OR pg_catalog.has_any_column_privilege(session_user,:table,'INSERT,UPDATE')) AS unsafe""",
                    {"table": self.config.schema_name + ".monitor_robot_config_versions"},
                )[0]["unsafe"]
            ):
                raise HTTPException(403, "管理员数据库身份不符合最小权限要求")
            self.query(body.username, "SELECT * FROM rp1_monitor.dashboard_robots LIMIT 0")
            raw = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            with self.lock:
                self.sessions = {
                    key: item for key, item in self.sessions.items() if item["expires"] > now
                }
                if len(self.sessions) >= 128:
                    raise HTTPException(429, "管理会话已满，请退出旧会话")
                old = request.cookies.get(self.cookie_name, "")
                self.sessions.pop(hashlib.sha256(old.encode()).hexdigest(), None)
                self.sessions[hashlib.sha256(raw.encode()).hexdigest()] = {
                    "username": user.username,
                    "csrf": csrf,
                    "expires": now + 8 * 3600,
                }
                self.attempts.pop(address, None)
            response.set_cookie(
                self.cookie_name,
                raw,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=8 * 3600,
                path=(request.scope.get("root_path", "") or "") + "/api/v1/admin",
            )
            response.headers["Cache-Control"] = "no-store"
            return {"username": user.username, "csrf": csrf}

        @router.get("/session")
        def session(actor: Human, response: Response) -> dict:
            response.headers["Cache-Control"] = "no-store"
            return {"username": actor["username"], "csrf": actor["csrf"]}

        @router.post("/logout")
        def logout(request: Request, response: Response, actor: Writer) -> dict:
            with self.lock:
                self.sessions.pop(actor["key"], None)
            response.delete_cookie(
                self.cookie_name, path=(request.scope.get("root_path", "") or "") + "/api/v1/admin"
            )
            return {"logged_out": True}

        @router.get("/robots")
        def robots(actor: Human) -> dict:
            return {
                "items": self.query(
                    actor["username"],
                    "SELECT * FROM rp1_monitor.dashboard_robots ORDER BY robot_id",
                )
            }

        @router.post("/robots", status_code=201)
        def create_robot(body: RobotCreate, actor: Writer) -> dict:
            if self.demo_read and body.source_kind != "simulated":
                raise HTTPException(422, "当前为模拟环境，只允许登记simulated机器人")
            if body.configuration is not None:
                try:
                    body.configuration.validate_source(body.source_kind)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
            key = secrets.token_urlsafe(32)
            reference = body.robot_id + "-" + secrets.token_hex(6) + ".token"
            descriptor = os.open(
                self.key_directory / reference, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(self.key_directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            # A failed/uncertain commit retains its private credential for operator reconciliation.
            query = (
                "SELECT rp1_monitor.admin_create_robot_with_configuration(:robot_id,:name,:source,:digest,:reason,CAST(:configuration AS jsonb)) AS revision"
                if body.configuration is not None
                else "SELECT rp1_monitor.admin_create_robot(:robot_id,:name,:source,:digest,:reason) AS robot_id"
            )
            parameters = {
                "robot_id": body.robot_id,
                "name": body.name,
                "source": body.source_kind,
                "digest": hashlib.sha256(key.encode()).hexdigest(),
                "reason": body.reason,
            }
            if body.configuration is not None:
                parameters["configuration"] = body.configuration.model_dump_json()
            result = self.query(
                actor["username"],
                query,
                parameters,
                write=True,
            )
            return {
                "robot_id": body.robot_id,
                "credential_reference": reference,
                "detail": "设备上传凭据已保存到服务器私有部署目录，页面不显示Key",
                "configuration_revision": result[0].get("revision"),
            }

        @router.get("/robots/{robot_id}/configuration")
        def configuration(robot_id: RobotId, actor: Human) -> dict:
            try:
                return self.store.effective_monitoring_configuration(robot_id)
            except KeyError:
                raise HTTPException(404, "机器人不存在") from None

        @router.get("/robots/{robot_id}/configuration/versions")
        def configuration_versions(
            robot_id: RobotId,
            actor: Human,
            before_id: Annotated[int | None, Query(ge=1)] = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 30,
        ) -> dict:
            rows = self.query(
                actor["username"],
                """
                SELECT id, previous_id, configuration, reason, actor_db_login, created_at
                FROM rp1_monitor.monitor_robot_config_versions WHERE robot_id=:robot_id
                  AND (CAST(:before_id AS bigint) IS NULL OR id<:before_id)
                ORDER BY id DESC LIMIT :limit
            """,
                {"robot_id": robot_id, "before_id": before_id, "limit": limit + 1},
            )
            return {
                "items": rows[:limit],
                "next_before_id": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

        @router.post("/robots/{robot_id}/configuration", status_code=201)
        def save_configuration(robot_id: RobotId, body: ConfigurationSave, actor: Writer) -> dict:
            registered = self.query(
                actor["username"],
                "SELECT source_kind FROM rp1_monitor.dashboard_robots WHERE robot_id=:robot_id",
                {"robot_id": robot_id},
            )
            if not registered:
                raise HTTPException(404, "机器人不存在")
            try:
                body.configuration.validate_source(registered[0]["source_kind"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            rows = self.query(
                actor["username"],
                """
                SELECT rp1_monitor.admin_save_robot_configuration(:robot_id,:expected,
                  CAST(:configuration AS jsonb),:reason) AS revision
            """,
                {
                    "robot_id": robot_id,
                    "expected": body.expected_revision,
                    "configuration": body.configuration.model_dump_json(),
                    "reason": body.reason,
                },
                write=True,
            )
            return {
                "robot_id": robot_id,
                "source_kind": registered[0]["source_kind"],
                "revision": rows[0]["revision"],
                "configuration": body.configuration.model_dump(),
                "source": "database",
            }

        @router.patch("/robots/{robot_id}")
        def edit_robot(robot_id: RobotId, body: RobotUpdate, actor: Writer) -> dict:
            self.query(
                actor["username"],
                """
                SELECT rp1_monitor.admin_update_robot(:robot_id,:old_name,:old_enabled,:name,:enabled,:reason)
            """,
                {
                    "robot_id": robot_id,
                    "old_name": body.expected_name,
                    "old_enabled": body.expected_enabled,
                    "name": body.name,
                    "enabled": body.enabled,
                    "reason": body.reason,
                },
                write=True,
            )
            return {"robot_id": robot_id, "updated": True}

        @router.get("/robots/{robot_id}/windows")
        def windows(
            robot_id: RobotId,
            metric: MetricKey,
            actor: Human,
            minutes: Annotated[int, Query(ge=1, le=44640)] = 10,
            before_id: Annotated[int | None, Query(ge=1)] = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 30,
        ) -> dict:
            rows = self.query(
                actor["username"],
                """
                SELECT w.id AS window_id, w.window_start, w.window_end, w.payload_hash,
                  w.payload -> 'metrics' -> :metric AS original, r.source_kind,
                  c.revision, c.operation, c.effective_metric, c.reason AS correction_reason
                FROM rp1_monitor.monitor_windows w
                JOIN rp1_monitor.dashboard_robots r ON r.robot_id=w.robot_id
                LEFT JOIN rp1_monitor.admin_current_metric_corrections c
                  ON c.window_id=w.id AND c.metric_key=:metric
                WHERE w.robot_id=:robot AND w.window_end>=:start
                  AND (CAST(:before_id AS bigint) IS NULL OR w.id<:before_id)
                  AND w.payload -> 'metrics' -> :metric IS NOT NULL
                ORDER BY w.id DESC LIMIT :limit
            """,
                {
                    "robot": robot_id,
                    "metric": metric,
                    "start": datetime.now(timezone.utc) - timedelta(minutes=minutes),
                    "before_id": before_id,
                    "limit": limit,
                },
            )
            for row in rows:
                row["effective"] = row["effective_metric"] if row["revision"] else row["original"]
                row.pop("effective_metric", None)
            return {
                "robot_id": robot_id,
                "metric": metric,
                "spec": metric_spec(metric),
                "items": rows,
                "next_before_id": rows[-1]["window_id"] if len(rows) == limit else None,
            }

        @router.get("/robots/{robot_id}/metrics")
        def metrics(robot_id: RobotId, actor: Human) -> dict:
            rows = self.query(
                actor["username"],
                """
                SELECT w.payload -> 'metrics' AS metrics FROM rp1_monitor.monitor_windows w
                WHERE w.robot_id=:robot ORDER BY w.id DESC LIMIT 1
            """,
                {"robot": robot_id},
            )
            return {
                "items": [metric_spec(key) for key in sorted(rows[0]["metrics"])] if rows else []
            }

        @router.post("/windows/{window_id}/corrections", status_code=201)
        def correct(
            window_id: Annotated[int, PathParameter(ge=1)], body: Correction, actor: Writer
        ) -> dict:
            replacement = body.replacement.model_dump() if body.replacement else None
            rows = self.query(
                actor["username"],
                """
                SELECT rp1_monitor.admin_correct_metric(:window,:metric,:expected,:operation,
                  CAST(:replacement AS jsonb),:reason) AS revision
            """,
                {
                    "window": window_id,
                    "metric": body.metric,
                    "expected": body.expected_revision,
                    "operation": body.operation,
                    "replacement": json.dumps(replacement) if replacement is not None else None,
                    "reason": body.reason,
                },
                write=True,
            )
            return {"revision": rows[0]["revision"], "original_unchanged": True}

        @router.get("/audit")
        def audit(
            actor: Human,
            robot_id: RobotId | None = None,
            before_id: Annotated[int | None, Query(ge=1)] = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 30,
        ) -> dict:
            rows = self.query(
                actor["username"],
                """
                SELECT * FROM rp1_monitor.monitor_admin_audit
                WHERE (CAST(:robot AS text) IS NULL OR robot_id=:robot)
                  AND (CAST(:before_id AS bigint) IS NULL OR id<:before_id)
                ORDER BY id DESC LIMIT :limit
            """,
                {"robot": robot_id, "before_id": before_id, "limit": limit},
            )
            return {"items": rows, "next_before_id": rows[-1]["id"] if len(rows) == limit else None}

        return router
