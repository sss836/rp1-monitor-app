"""Independent, explicit factory-platform bridge. Never starts the native monitor."""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import socket
import sqlite3
import ssl
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from getpass import getpass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID, uuid4

MAX_CSV_BYTES = 4 * 1024**2
MAX_CSV_ROWS = 1000
MAX_RESPONSE_BYTES = 2 * 1024**2
MAX_RESPONSE_BUDGET_BYTES = 64 * 1024**2
CSV_COLUMNS = (
    "记录编号", "上位机编号", "样品编号", "机型", "测试体系", "测试分类", "记录层级",
    "测试项目编号", "原始检查ID", "测试内容", "开始时间", "结束时间", "测试人员",
    "实际结果", "通过标准", "是否通过", "备注",
)
UPLOAD_FIELDS = {
    "source_id", "robot_id", "source_kind", "filename", "size_bytes", "sha256",
    "verified", "completed", "reason",
}
SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
ROBOT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class BridgeError(Exception):
    """Only fixed, non-secret codes cross the CLI/outbox boundary."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.code, self.status = code, status


def permanent_submission_error(error):
    """Only deterministic task errors are isolated; outages/auth remain retryable."""
    # archive_conflict also covers deadlocks/serialization failures on the server;
    # offset_conflict/incomplete_upload recover by rereading its stored progress.
    return error.status in {400, 413, 415, 422} or error.code in {
        "export_evidence_changed", "export_evidence_unavailable", "archive_processing_failed",
        "archive_context_conflict", "archive_verification_conflict", "archive_revision_conflict",
        "factory_result_storage_conflict", "factory_result_content_conflict",
        "archive_storage_conflict", "archive_storage_incomplete", "immutable_archive",
        "chunk_conflict", "archive_corrupt", "segmented_upload_required",
        "factory_trace_conflict", "factory_trace_state_conflict", "factory_trace_required", "factory_trace_frozen_hash_mismatch",
        "factory_trace_csv_mismatch", "invalid_factory_trace", "invalid_factory_trace_size", "payload_hash_mismatch",
    }


def clean_text(value, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BridgeError("invalid_identity")
    if any(ord(char) < 32 for char in value):
        raise BridgeError("invalid_identity")
    return value.strip()


def source_id(value):
    if not isinstance(value, str) or not SOURCE_PATTERN.fullmatch(value):
        raise BridgeError("invalid_source_id")
    return value


def base_url(value, *, allow_http=False, local=False):
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"} or not parts.hostname
        or parts.username or parts.password or parts.query or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise BridgeError("invalid_base_url")
    if local and (parts.scheme != "http" or parts.hostname != "127.0.0.1"):
        raise BridgeError("monitor_must_be_loopback")
    if parts.scheme == "http" and not allow_http and parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise BridgeError("platform_requires_https")
    try:
        parts.port
    except ValueError:
        raise BridgeError("invalid_base_url") from None
    return value.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("redirect_refused", code)


class RequestDeadline:
    """Interrupt owned sockets; no worker thread survives a timed-out request."""

    def __init__(self, seconds):
        self.until = time.monotonic() + seconds
        self.expired = threading.Event()
        self.lock = threading.Lock()
        self.sockets = []
        self.timer = threading.Timer(seconds, self.abort)
        self.timer.name = "rp1-bridge-request-deadline"
        self.timer.daemon = True

    def start(self):
        self.timer.start()

    @staticmethod
    def close_socket(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def track(self, sock):
        with self.lock:
            self.sockets.append(sock)
            expired = self.expired.is_set()
        if expired:
            self.close_socket(sock)
        self.check()

    def check(self):
        if self.expired.is_set() or time.monotonic() >= self.until:
            self.abort()
            raise BridgeError("request_deadline_exceeded")

    def abort(self):
        with self.lock:
            self.expired.set()
            sockets = tuple(self.sockets)
        for sock in sockets:
            self.close_socket(sock)

    def finish(self):
        self.timer.cancel()
        self.timer.join()
        with self.lock:
            sockets, self.sockets = self.sockets, []
        for sock in sockets:
            self.close_socket(sock)


def connect_with_deadline(address, timeout, source_address=None, *, deadline):
    """DNS is OS-owned; all subsequent TCP attempts share one remaining budget."""
    host, port = address
    candidates = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    last_error = None
    for family, kind, protocol, _, target in candidates:
        deadline.check()
        sock = socket.socket(family, kind, protocol)
        try:
            deadline.track(sock)
            remaining = max(0.001, deadline.until - time.monotonic())
            sock.settimeout(min(timeout, remaining) if timeout is not None else remaining)
            if source_address:
                sock.bind(source_address)
            sock.connect(target)
            deadline.check()
            return sock
        except OSError as exc:
            last_error = exc
            RequestDeadline.close_socket(sock)
            deadline.check()
        except BaseException:
            RequestDeadline.close_socket(sock)
            raise
    if last_error is not None:
        raise last_error
    raise OSError("no TCP address candidates")


class DeadlineHTTPConnection(HTTPConnection):
    def __init__(self, host, *, deadline, **kwargs):
        super().__init__(host, **kwargs)
        self.deadline = deadline

        def create_connection(address, timeout, source_address=None):
            return connect_with_deadline(address, timeout, source_address, deadline=deadline)

        self._create_connection = create_connection

    def connect(self):
        super().connect()
        self.deadline.track(self.sock)


class DeadlineHTTPSConnection(HTTPSConnection):
    def __init__(self, host, *, deadline, **kwargs):
        super().__init__(host, **kwargs)
        self.deadline = deadline

        def create_connection(address, timeout, source_address=None):
            return connect_with_deadline(address, timeout, source_address, deadline=deadline)

        self._create_connection = create_connection

    def connect(self):
        # Proxying/tunnelling is disabled. Register the TLS socket before handshake.
        HTTPConnection.connect(self)
        self.deadline.track(self.sock)
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self.host, do_handshake_on_connect=False,
        )
        self.deadline.track(self.sock)
        self.sock.do_handshake()
        self.deadline.check()


class DeadlineHTTPHandler(HTTPHandler):
    def __init__(self, current_deadline):
        super().__init__()
        self.current_deadline = current_deadline

    def http_open(self, request):
        def connection(host, **kwargs):
            return DeadlineHTTPConnection(host, deadline=self.current_deadline(), **kwargs)
        return self.do_open(connection, request)


class DeadlineHTTPSHandler(HTTPSHandler):
    def __init__(self, current_deadline, ca_file=None):
        super().__init__(context=ssl.create_default_context(cafile=ca_file))
        self.current_deadline = current_deadline

    def https_open(self, request):
        def connection(host, **kwargs):
            return DeadlineHTTPSConnection(host, deadline=self.current_deadline(), **kwargs)
        return self.do_open(connection, request, context=self._context)


class JsonHttpClient:
    def __init__(self, url, *, allow_http=False, timeout=15, ca_file=None, response_bytes=MAX_RESPONSE_BYTES):
        self.base_url = base_url(url, allow_http=allow_http)
        self.timeout = timeout
        if not math.isfinite(timeout) or timeout <= 0:
            raise BridgeError("invalid_request_timeout")
        if type(response_bytes) is not int or not 1 <= response_bytes <= MAX_RESPONSE_BUDGET_BYTES:
            raise BridgeError("invalid_response_budget")
        self.response_bytes = response_bytes
        self.cookies = CookieJar()
        self._request_lock = threading.Lock()
        self._deadline = None
        self.opener = build_opener(
            ProxyHandler({}), HTTPCookieProcessor(self.cookies), NoRedirect(),
            DeadlineHTTPHandler(lambda: self._deadline),
            DeadlineHTTPSHandler(lambda: self._deadline, ca_file),
        )

    def cancel_current_request(self):
        deadline = self._deadline
        if deadline is not None:
            deadline.abort()

    def request(self, method, path, *, body=None, headers=None, cancel_event=None):
        request = Request(self.base_url + path, data=body, method=method, headers=headers or {})
        with self._request_lock:
            deadline = RequestDeadline(self.timeout)
            self._deadline = deadline
            deadline.start()
            try:
                try:
                    if cancel_event is not None and cancel_event.is_set():
                        raise BridgeError("cancelled")
                    deadline.check()
                    response = self.opener.open(request, timeout=self.timeout)
                    with response:
                        status = getattr(response, "status", None)
                        raw = response.read(self.response_bytes + 1)
                        deadline.check()
                        if len(raw) > self.response_bytes:
                            raise BridgeError("response_too_large")
                except HTTPError as exc:
                    # Retain only a bounded, safe machine code, never response text.
                    code = f"http_{exc.code}"
                    try:
                        error_raw = exc.read(16385)
                        if len(error_raw) <= 16384:
                            error = json.loads(error_raw).get("error", {})
                            candidate = error.get("code") if isinstance(error, dict) else None
                            if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", candidate):
                                code = candidate
                    except (ValueError, TypeError, AttributeError, OSError):
                        pass
                    finally:
                        exc.close()
                    deadline.check()
                    raise BridgeError(code, exc.code) from None
                except (URLError, OSError, TimeoutError, HTTPException):
                    deadline.check()
                    raise BridgeError("network_error") from None
                except Exception:
                    # TLS close/read races may raise ValueError/AttributeError instead
                    # of OSError. Preserve non-timeout faults, normalize expired I/O.
                    deadline.check()
                    raise
            finally:
                deadline.finish()
                self._deadline = None
        if status == 204 and not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise BridgeError("invalid_json_response") from None
        if not isinstance(data, dict):
            raise BridgeError("invalid_json_response")
        return data


class PlatformClient(JsonHttpClient):
    def __init__(self, url, *, allow_http=False, timeout=15):
        super().__init__(url, allow_http=allow_http, timeout=timeout)
        self.user = None

    def login(self, username, password):
        self.cookies.clear()
        self.user = None
        result = self.request(
            "POST", "/api/v1/auth/login",
            body=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        data = result.get("data")
        user = data.get("user", {}) if isinstance(data, dict) else {}
        if not isinstance(user, dict):
            raise BridgeError("invalid_login_response")
        if not user.get("id") or user.get("must_change_password") or not self.csrf() or not any(
            cookie.name == "rp1_session" for cookie in self.cookies
        ):
            self.cookies.clear()
            raise BridgeError("login_requires_valid_human_session")
        if user.get("kind") != "HUMAN":
            self.cookies.clear()
            raise BridgeError("human_session_required")
        self.user = user
        return user

    def csrf(self):
        values = [cookie.value for cookie in self.cookies if cookie.name == "rp1_csrf"]
        return values[0] if len(values) == 1 else ""

    def post(self, path, body, content_type, *, idempotency_key=None):
        if self.user is None or not self.csrf():
            raise BridgeError("login_required")
        headers = {"Content-Type": content_type, "X-CSRF-Token": self.csrf()}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            result = self.request("POST", path, body=body, headers=headers)
        except BridgeError as exc:
            if exc.status == 401:
                self.user = None
                self.cookies.clear()
            raise
        if not isinstance(result.get("data"), dict):
            raise BridgeError("invalid_platform_envelope")
        return result["data"]

    def publish_presence(self, payload):
        receipt = self.post(
            "/api/v1/factory/robots/presence",
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(), "application/json",
        )
        if (
            receipt.get("source_id") != payload.get("source_id")
            or receipt.get("accepted") != len(payload.get("robots", []))
            or not timestamp(receipt.get("as_of_at"))
            or receipt.get("offline_after_seconds") != 45
        ):
            raise BridgeError("invalid_presence_receipt")
        return {key: receipt[key] for key in ("source_id", "accepted", "as_of_at", "offline_after_seconds")}

    def upload_result(self, metadata, raw_bytes, idempotency_key):
        receipt = self.post(
            "/api/v1/factory/robot-result-archives?" + urlencode(metadata),
            raw_bytes, "text/csv", idempotency_key=idempotency_key,
        )
        return validate_receipt(metadata, raw_bytes, receipt)

    def upload_trace(self, archive_id, raw, digest):
        from .factory_trace_upload import validate_trace_receipt

        archive_id = str(UUID(archive_id))
        receipt = self.post(
            f"/api/v1/factory/robot-result-archives/{archive_id}/trace", raw, "application/json",
            idempotency_key="factory-trace-" + digest,
        )
        return validate_trace_receipt(archive_id, digest, receipt)


def private_file(path, maximum):
    """Read only an explicitly named regular file; reject symlink/device inputs."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise BridgeError("invalid_input_file")
            result = stream.read(maximum + 1)
    except OSError:
        raise BridgeError("input_file_unavailable") from None
    if len(result) > maximum:
        raise BridgeError("input_file_too_large")
    return result


class MonitorClient(JsonHttpClient):
    def __init__(self, url, token_file, *, timeout=5):
        base_url(url, local=True)
        super().__init__(url, timeout=timeout)
        self.token_file = Path(token_file)

    def robots(self):
        try:
            token = private_file(self.token_file, 4096).decode("utf-8").strip()
        except UnicodeError:
            raise BridgeError("invalid_viewer_token") from None
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise BridgeError("invalid_viewer_token")
        result = self.request(
            "GET", "/api/v1/robot-monitors", headers={"Authorization": "Bearer " + token},
        )
        if not isinstance(result.get("items"), list) or len(result["items"]) > 256:
            raise BridgeError("invalid_monitor_response")
        return result


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def recent(value, now, age=10):
    parsed = timestamp(value)
    return parsed is not None and -5 <= (now - parsed).total_seconds() <= age


def presence_payload(source, kind, station_name, bindings, snapshot, *, now=None):
    """The monitor has no SN: only explicit operator bindings supply SN/model."""
    if kind not in {"real", "simulated"}:
        raise BridgeError("invalid_source_kind")
    now = now or datetime.now(timezone.utc)
    station_name = clean_text(station_name, 128)
    items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
    by_id = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("robot_id"), str):
            if item["robot_id"] in by_id:
                raise BridgeError("duplicate_monitor_identity")
            by_id[item["robot_id"]] = item
    robots, skipped, seen, serials = [], [], set(), set()
    if not isinstance(bindings, list) or len(bindings) > 100:
        raise BridgeError("invalid_bindings")
    for binding in bindings:
        if not isinstance(binding, dict):
            raise BridgeError("invalid_bindings")
        robot_id = binding.get("robot_id")
        if not isinstance(robot_id, str) or not ROBOT_PATTERN.fullmatch(robot_id) or robot_id in seen:
            raise BridgeError("invalid_robot_binding")
        seen.add(robot_id)
        serial = binding.get("serial_number")
        model = binding.get("model")
        if not isinstance(serial, str) or not serial.strip() or model not in {"24轴", "28轴"}:
            skipped.append(robot_id)
            continue
        serial = clean_text(serial, 200)
        if serial in serials:
            raise BridgeError("duplicate_serial_binding")
        serials.add(serial)
        item = by_id.get(robot_id, {})
        matching_source = item.get("source_kind") == ("ros2" if kind == "real" else "simulated")
        observed = item.get("received_at") if timestamp(item.get("received_at")) else None
        online = bool(
            item.get("enabled") is True and item.get("online") is True and matching_source
            and recent(snapshot.get("as_of_at"), now) and recent(observed, now)
        )
        robots.append({
            "robot_id": robot_id, "serial_number": serial, "model": model,
            "station_name": station_name, "online": online, "observed_at": observed,
            "data_fresh": item.get("data_fresh") is True if online else False,
        })
    return {
        "protocol": "reliability_v1", "source_id": source_id(source),
        "source_kind": kind, "robots": robots,
    }, skipped


def read_result_csv(path):
    """Read one explicit original file with bounded row count; no verification claim."""
    path = Path(path)
    if path.suffix.lower() != ".csv" or "\\" in path.name or len(path.name) > 200:
        raise BridgeError("csv_file_required")
    raw = private_file(path, MAX_CSV_BYTES)
    try:
        decoded = raw.decode("utf-8-sig")
        if "\x00" in decoded:
            raise BridgeError("invalid_17_column_csv")
        reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
        if tuple(next(reader, [])) != CSV_COLUMNS:
            raise BridgeError("invalid_17_column_csv")
        rows = []
        for row in reader:
            if len(rows) >= MAX_CSV_ROWS or len(row) != 17 or any(len(value) > 65536 for value in row):
                raise BridgeError("invalid_17_column_csv")
            rows.append(row)
    except (UnicodeError, csv.Error):
        raise BridgeError("invalid_utf8_csv") from None
    if not rows:
        raise BridgeError("invalid_17_column_csv")
    return path, raw, rows


def result_upload(path, *, source, robot_id, kind, verified, completed, submit, reason):
    if not verified or not completed or not submit:
        raise BridgeError("explicit_verified_completed_submit_required")
    if kind not in {"real", "simulated"}:
        raise BridgeError("invalid_source_kind")
    source = source_id(source)
    if not isinstance(robot_id, str) or not ROBOT_PATTERN.fullmatch(robot_id):
        raise BridgeError("invalid_robot_id")
    reason = clean_text(reason, 300)
    path, raw, rows = read_result_csv(path)
    if any(row[1] != source or not row[0].strip() for row in rows):
        raise BridgeError("csv_source_or_record_identity_mismatch")
    if len({row[2] for row in rows}) != 1 or not rows[0][2].strip():
        raise BridgeError("csv_single_sn_required")
    if len({row[3] for row in rows}) != 1 or rows[0][3] not in {"24轴", "28轴"}:
        raise BridgeError("csv_single_model_required")
    metadata = {
        "source_id": source, "robot_id": robot_id, "source_kind": kind,
        "filename": path.name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "verified": "true", "completed": "true", "reason": reason,
    }
    return metadata, raw


def validate_receipt(metadata, raw, receipt):
    try:
        UUID(receipt["id"])
        rows = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        next(rows)
        serial = next(rows)[2]
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration, IndexError):
        raise BridgeError("invalid_archive_receipt") from None
    if (
        receipt.get("sha256") != metadata["sha256"]
        or receipt.get("size_bytes") != len(raw)
        or receipt.get("source_id") != metadata["source_id"]
        or receipt.get("robot_id") != metadata["robot_id"]
        or receipt.get("source_kind") != metadata["source_kind"]
        or receipt.get("serial_number") != serial
        or receipt.get("status") not in {"PENDING", "APPROVED", "REJECTED"}
        or type(receipt.get("revision")) is not int or receipt["revision"] < 1
        or receipt.get("formal_statistics_eligible") is not False
    ):
        raise BridgeError("archive_receipt_identity_mismatch")
    return {key: receipt[key] for key in (
        "id", "sha256", "status", "revision", "formal_statistics_eligible",
        "source_id", "robot_id", "serial_number", "source_kind", "size_bytes",
    )}


class Outbox:
    """Frozen explicit submissions only. No directory scans and no credentials."""

    def __init__(self, path, *, max_pending_bytes=32 * 1024**2, max_pending_count=64):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_info = self.path.parent.stat()
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise BridgeError("invalid_state_path")
        if directory_info.st_uid != os.getuid() or directory_info.st_mode & 0o077:
            raise BridgeError("state_directory_must_be_private")
        self.max_pending_bytes, self.max_pending_count = max_pending_bytes, max_pending_count
        self.db = sqlite3.connect(self.path, timeout=5)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_size_limit=1048576")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_key TEXT PRIMARY KEY, platform_url TEXT NOT NULL, owner_id TEXT NOT NULL,
            metadata TEXT NOT NULL, payload BLOB, bytes INTEGER NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', receipt TEXT,
            envelope_hash TEXT
        )""")
        if "envelope_hash" not in {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN envelope_hash TEXT")
            self.db.execute(
                "UPDATE tasks SET envelope_hash=substr(task_key,16) WHERE envelope_hash IS NULL"
            )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        for name, kind in (("trace_payload", "BLOB"), ("trace_sha256", "TEXT"), ("trace_receipt", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
        self.db.commit()

    def close(self):
        self.db.close()

    def enqueue(self, platform_url, owner_id, metadata, raw, *, trace_raw=None):
        if not isinstance(metadata, dict) or set(metadata) - {"trace_sha256"} != UPLOAD_FIELDS:
            raise BridgeError("invalid_submission_metadata")
        if len(raw) > MAX_CSV_BYTES or len(raw) != metadata.get("size_bytes") or hashlib.sha256(raw).hexdigest() != metadata.get("sha256"):
            raise BridgeError("payload_hash_mismatch")
        if metadata.get("verified") != "true" or metadata.get("completed") != "true":
            raise BridgeError("unverified_submission_refused")
        trace_hash = None
        if trace_raw is not None:
            from .factory_trace_upload import validate_trace

            trace_hash = validate_trace(trace_raw, raw)
            if metadata.get("trace_sha256", trace_hash) != trace_hash:
                raise BridgeError("factory_trace_frozen_hash_mismatch")
            metadata = {**metadata, "trace_sha256": trace_hash}
        elif "trace_sha256" in metadata:
            raise BridgeError("factory_trace_payload_required")
        envelope = json.dumps({
            "platform_url": base_url(platform_url, allow_http=True),
            "owner_id": clean_text(owner_id), "metadata": metadata,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        frozen_bytes = len(raw) + len(trace_raw or b"")
        envelope_hash = hashlib.sha256(envelope.encode()).hexdigest()
        with self.db:
            # Multiple CLI processes must serialize the budget check and insertion.
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT task_key FROM tasks WHERE envelope_hash=? AND status!='sent' ORDER BY rowid LIMIT 1",
                (envelope_hash,),
            ).fetchone()
            if existing:
                return existing[0]
            count, size = self.db.execute(
                "SELECT count(*),coalesce(sum(bytes),0) FROM tasks WHERE status!='sent'"
            ).fetchone()
            if count >= self.max_pending_count or size + frozen_bytes > self.max_pending_bytes:
                raise BridgeError("outbox_full_existing_evidence_retained")
            # A new explicit submission after a completed transfer is a new attempt.
            # Pending retries retain their nonce/key; rejected archives can be resubmitted.
            key = "factory-result-" + envelope_hash + "-" + uuid4().hex
            self.db.execute(
                "INSERT INTO tasks(task_key,platform_url,owner_id,metadata,payload,bytes,status,envelope_hash,trace_payload,trace_sha256) VALUES(?,?,?,?,?,?,'pending',?,?,?)",
                (key, base_url(platform_url, allow_http=True), owner_id,
                 json.dumps(metadata, ensure_ascii=False, sort_keys=True), raw, frozen_bytes, envelope_hash, trace_raw, trace_hash),
            )
        return key

    def status(self):
        return [dict(zip(("task_key", "status", "attempts", "last_error"), row)) for row in
                self.db.execute("SELECT task_key,status,attempts,last_error FROM tasks ORDER BY rowid")]

    def drain(self, platform, *, limit=10, source=None, task_key=None):
        if platform.user is None:
            raise BridgeError("login_required")
        owner = str(platform.user["id"])
        if task_key is not None:
            rows = self.db.execute(
                "SELECT task_key,platform_url,owner_id,metadata,payload,status,receipt,trace_payload,trace_sha256 FROM tasks WHERE task_key=?",
                (task_key,),
            ).fetchall()
            if not rows:
                raise BridgeError("outbox_task_not_found")
            if rows[0][1] != platform.base_url or rows[0][2] != owner:
                raise BridgeError("outbox_account_or_destination_mismatch")
        else:
            # Filter destination/account before limiting. Old queues stay untouched.
            rows = self.db.execute(
                "SELECT task_key,platform_url,owner_id,metadata,payload,status,receipt,trace_payload,trace_sha256 FROM tasks "
                "WHERE status='pending' AND platform_url=? AND owner_id=? ORDER BY rowid",
                (platform.base_url, owner),
            ).fetchall()
        acknowledged = []
        attempted = 0
        for key, url, task_owner, metadata, raw, status, saved_receipt, trace_raw, trace_hash in rows:
            if status == "sent":
                continue
            metadata = json.loads(metadata)
            if source is not None and metadata["source_id"] != source:
                if task_key is not None:
                    raise BridgeError("outbox_source_mismatch")
                continue
            if attempted >= limit:
                break
            attempted += 1
            try:
                if raw is None or len(raw) != metadata["size_bytes"] or hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
                    raise BridgeError("payload_hash_mismatch")
                if (trace_raw is None) != (trace_hash is None):
                    raise BridgeError("factory_trace_frozen_hash_mismatch")
                if trace_raw is not None:
                    from .factory_trace_upload import validate_trace, validate_trace_receipt

                    if validate_trace(trace_raw, raw) != trace_hash or metadata.get("trace_sha256") != trace_hash:
                        raise BridgeError("factory_trace_frozen_hash_mismatch")
                if saved_receipt is None:
                    receipt = platform.upload_result(metadata, raw, key)
                    receipt = validate_receipt(metadata, raw, receipt)
                    safe = {name: receipt[name] for name in (
                        "id", "sha256", "status", "revision", "formal_statistics_eligible",
                    )}
                    if trace_raw is not None:
                        # CSV delivery and trace delivery are separate durable stages.
                        # A lost trace response must never create another CSV archive.
                        with self.db:
                            self.db.execute("UPDATE tasks SET receipt=? WHERE task_key=?", (json.dumps(safe), key))
                else:
                    safe = json.loads(saved_receipt)
                    if safe.get("sha256") != metadata["sha256"] or safe.get("formal_statistics_eligible") is not False:
                        raise BridgeError("archive_receipt_identity_mismatch")
                    try:
                        UUID(safe["id"])
                    except (ValueError, TypeError, KeyError):
                        raise BridgeError("invalid_archive_receipt") from None
                trace_receipt = None
                if trace_raw is not None:
                    trace_receipt = platform.upload_trace(safe["id"], trace_raw, trace_hash)
                    trace_receipt = validate_trace_receipt(safe["id"], trace_hash, trace_receipt)
            except BridgeError as exc:
                blocked = permanent_submission_error(exc)
                with self.db:
                    self.db.execute(
                        "UPDATE tasks SET attempts=attempts+1,last_error=?,status=? WHERE task_key=? AND status!='sent'",
                        (exc.code, "blocked" if blocked else "pending", key),
                    )
                if task_key is not None or not blocked:
                    raise
                continue
            # Persist only bounded receipt identity/status; never arbitrary remote payloads.
            with self.db:
                self.db.execute(
                    "UPDATE tasks SET status='sent',payload=NULL,trace_payload=NULL,trace_receipt=?,bytes=0,attempts=attempts+1,last_error='',receipt=? WHERE task_key=?",
                    (json.dumps(trace_receipt) if trace_receipt else None, json.dumps(safe), key),
                )
                self.db.execute("DELETE FROM tasks WHERE status='sent' AND rowid NOT IN (SELECT rowid FROM tasks WHERE status='sent' ORDER BY rowid DESC LIMIT 512)")
            acknowledged.append({"task_key": key, "receipt": safe})
        return acknowledged


def parser():
    result = argparse.ArgumentParser(description="独立工厂桥接；不启动Qt/机器人，不改变原记录与保存")
    result.add_argument("--platform-url", required=True)
    result.add_argument("--source-id", required=True)
    result.add_argument("--username")
    result.add_argument("--password-stdin", action="store_true")
    result.add_argument("--allow-http", action="store_true", help="显式允许非HTTPS平台，仅用于受控网络")
    result.add_argument("--state-dir", type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    presence = commands.add_parser("presence", help="只读监控列表并上报完整工厂来源快照")
    presence.add_argument("--monitor-url", default="http://127.0.0.1:18980")
    presence.add_argument("--viewer-token-file", type=Path, required=True)
    presence.add_argument("--bindings", type=Path, required=True)
    presence.add_argument("--station-name", required=True)
    presence.add_argument("--source-kind", choices=("real", "simulated"), default="real")
    presence.add_argument("--interval", type=float, default=10)
    presence.add_argument("--once", action="store_true")
    upload = commands.add_parser("submit-results", help="只提交明确指定、已结束且人工核实的17列CSV")
    upload.add_argument("file", type=Path)
    upload.add_argument("--robot-id", required=True)
    upload.add_argument("--source-kind", choices=("real", "simulated"), required=True)
    upload.add_argument("--verified", action="store_true")
    upload.add_argument("--completed", action="store_true")
    upload.add_argument("--submit", action="store_true")
    upload.add_argument("--reason", required=True)
    retry = commands.add_parser("retry", help="重试已显式提交的冻结任务，不扫描新文件")
    retry.add_argument("--task-key", help="只重试指定任务；允许人工重试已隔离任务")
    commands.add_parser("status", help="只看本桥接任务状态，不登录、不读设备")
    return result


def run(args):
    source = source_id(args.source_id)
    state = args.state_dir or Path.home() / ".local/share/rp1-monitor/factory-bridge" / source
    if args.command == "status":
        box = Outbox(state / "outbox.sqlite")
        try:
            print(json.dumps(box.status(), ensure_ascii=False))
        finally:
            box.close()
        return 0
    # Validate local submission before requesting a password or making any network request.
    upload = None
    trace_raw = None
    if args.command == "submit-results":
        upload = result_upload(
            args.file, source=source, robot_id=args.robot_id, kind=args.source_kind,
            verified=args.verified, completed=args.completed, submit=args.submit, reason=args.reason,
        )
        from .factory_trace_upload import read_trace

        trace_raw = read_trace(args.file, upload[1])
    if not args.username:
        raise BridgeError("username_required")
    platform = PlatformClient(args.platform_url, allow_http=args.allow_http)
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass("平台密码（不保存）：")
    try:
        platform.login(args.username, password)
    finally:
        password = None
    if args.command in {"submit-results", "retry"}:
        box = Outbox(state / "outbox.sqlite")
        try:
            if upload:
                metadata, raw = upload
                key = box.enqueue(platform.base_url, str(platform.user["id"]), metadata, raw, trace_raw=trace_raw)
                print(json.dumps({"queued": key, "domain": "FACTORY"}))
            print(json.dumps({"acknowledged": box.drain(
                platform, source=source, task_key=key if upload else args.task_key,
            )}, ensure_ascii=False))
        finally:
            box.close()
        return 0
    if not math.isfinite(args.interval) or not 5 <= args.interval <= 30:
        raise BridgeError("presence_interval_must_be_5_to_30_seconds")
    try:
        bindings = json.loads(private_file(args.bindings, 64 * 1024))["robots"]
    except (ValueError, KeyError, TypeError):
        raise BridgeError("invalid_bindings") from None
    monitor = MonitorClient(args.monitor_url, args.viewer_token_file)
    while True:
        try:
            snapshot = monitor.robots()
        except BridgeError:
            snapshot = {"items": [], "as_of_at": None}
        payload, skipped = presence_payload(
            source, args.source_kind, args.station_name, bindings, snapshot,
        )
        if skipped:
            print(json.dumps({"warning": "missing_sn_or_model_binding", "robot_ids": skipped}, ensure_ascii=False), file=sys.stderr)
        try:
            receipt = platform.publish_presence(payload)
            print(json.dumps({"presence": receipt}, ensure_ascii=False))
        except BridgeError as exc:
            if args.once or (exc.status is not None and 400 <= exc.status < 500 and exc.status != 429):
                raise
            print(json.dumps({"presence_error": exc.code, "state": "delivery_unconfirmed"}), file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


def main(argv=None):
    os.umask(0o077)
    try:
        return run(parser().parse_args(argv))
    except BridgeError as exc:
        print(json.dumps({"error": exc.code, "http_status": exc.status}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except (OSError, sqlite3.Error, ValueError):
        print('{"error":"local_state_or_configuration_error"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
