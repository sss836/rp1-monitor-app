"""Low-rate, display-only whole-machine observations, separate from the outbox."""

import json
import os
import socket
import tempfile
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import UUID, uuid4, uuid5

from .factory_bridge import (
    ROBOT_PATTERN,
    BridgeError,
    JsonHttpClient,
    base_url,
    private_file,
    timestamp,
)

INTERVAL_MS = 10_000
MAX_ROBOTS = 32
LOCAL_PATH = "/api/v1/station/display-presence"
CENTRAL_PATH = "/api/v1/whole-machine/presence/"


def text(value, limit):
    if isinstance(value, str) and value.strip() and len(value) <= limit and all(ord(ch) >= 32 and ord(ch) != 127 for ch in value):
        return value.strip()
    return None


def robot_identity(value):
    return isinstance(value, str) and ROBOT_PATTERN.fullmatch(value) is not None


def observation(frame, *, online):
    """Project validated preview metadata only. Unknown identity stays absent."""
    if not frame or frame.get("source_kind") != "ros2" or not robot_identity(frame.get("robot_id")):
        return None
    item = {"robot_id": frame["robot_id"], "online": bool(online)}
    return item


def active_run(session, robot_id):
    if not isinstance(session, dict) or session.get("robot_id") != robot_id or session.get("source_kind") != "ros2" or session.get("ended_at"):
        return None
    name = text(session.get("run_number"), 200)
    started = timestamp(session.get("started_at"))
    if name and started and started <= datetime.now(timezone.utc):
        # The local case_number/robot_number are free labels, not platform IDs.
        return {"name": name, "started_at": started.isoformat()}
    return None


def _safe_rows(value):
    if not isinstance(value, list) or len(value) > MAX_ROBOTS:
        return []
    result = {}
    for row in value:
        if not isinstance(row, dict) or not robot_identity(row.get("robot_id")) or type(row.get("online")) is not bool:
            continue
        robot_id = row["robot_id"]
        if robot_id in result:
            return []
        item = {"robot_id": robot_id, "online": row["online"]}
        if row.get("model") in ("24轴", "28轴"):
            item["model"] = row["model"]
        if item["online"] and isinstance(row.get("run"), dict):
            run = row["run"]
            if name := text(run.get("name"), 200):
                started = timestamp(run.get("started_at"))
                if started and started <= datetime.now(timezone.utc):
                    item["run"] = {"name": name, "started_at": started.isoformat()}
        result[robot_id] = item
    return list(result.values())


def read_station(url, token_file):
    base_url(url, local=True)
    token = private_file(token_file, 4096).decode("ascii").strip()
    if not token or any(not 33 <= ord(ch) <= 126 for ch in token):
        raise BridgeError("invalid_viewer_token")
    local = JsonHttpClient(url, timeout=2, response_bytes=64 * 1024)
    value = local.request("GET", LOCAL_PATH, headers={"Authorization": "Bearer " + token})
    return _safe_rows(value.get("robots"))


def installation_id(directory, origin, owner):
    """One random installation seed, with disjoint IDs for each account and site."""
    path = Path(directory) / "display-installation-id"
    if not os.path.lexists(path):
        with tempfile.TemporaryDirectory(prefix=".display-", dir=directory) as staging:
            temporary = Path(staging) / "id"
            with temporary.open("x", encoding="ascii") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(str(uuid4()))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
    return str(uuid5(UUID(private_file(path, 64).decode("ascii")), f"{origin}\n{owner}"))


def asset_bindings(raw, origin):
    """A maintainer opts in for one explicit platform origin, with stable robot IDs."""
    if not raw:
        return {}
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate display binding keys")
        return result

    if len(raw) > 16 * 1024:
        raise ValueError("display asset bindings exceed size limit")
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {"platform_url", "robots"}:
        raise ValueError("invalid display asset bindings")
    configured_origin = base_url(value["platform_url"])
    robots = value["robots"]
    if not isinstance(robots, dict) or len(robots) > MAX_ROBOTS or any(not robot_identity(key) for key in robots):
        raise ValueError("invalid display robot bindings")
    bindings = {key: str(UUID(asset)) for key, asset in robots.items()}
    if len(set(bindings.values())) != len(bindings):
        raise ValueError("duplicate display asset bindings")
    return bindings if configured_origin == origin else {}


class DisplaySession:
    def __init__(self, source, directory, *, ca_file, allow_http):
        self.owner = str(source.user["id"])
        self.generation = source.generation
        self.expires_at = timestamp(source.expires_at)
        self.transport = JsonHttpClient(source.base_url, ca_file=ca_file, allow_http=allow_http,
                                        timeout=4, response_bytes=64 * 1024)
        self.installation = installation_id(directory, source.base_url, self.owner)
        for cookie in source.cookies:
            if cookie.name in {"rp1_session", "rp1_csrf"}:
                self.transport.cookies.set_cookie(copy(cookie))
        self.csrf = source.csrf()
        self.cancelled = Event()
        self.known = {}
        self.notice = "等待本机实机状态"
        try:
            self.assets = asset_bindings(os.environ.get("RP1_PLATFORM_PRESENCE_ASSETS", ""), source.base_url)
            self.binding_warning = ""
        except (BridgeError, ValueError, TypeError, AttributeError):
            self.assets = {}
            self.binding_warning = "资产关联配置无效，仍可上报在线状态"
        self.station_name = text(os.environ.get("RP1_DISPLAY_STATION_NAME"), 128) or text(socket.gethostname(), 128) or "RP1 工位"

    def valid(self):
        return not self.cancelled.is_set() and self.expires_at and self.expires_at > datetime.now(timezone.utc)

    def stop(self):
        self.cancelled.set()
        self.transport.cancel_current_request()

    def send(self, rows):
        if not self.valid():
            return False
        self.transport.request("PUT", CENTRAL_PATH + self.installation,
            body=json.dumps({"station_name": self.station_name, "robots": rows}, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf}, cancel_event=self.cancelled)
        return True


class DisplayPublisher:
    """At most one bounded background job, with no automatic retry queue."""

    def __init__(self):
        self.session = None
        self.worker = None
        self._lock = Lock()

    def replace(self, session):
        with self._lock:
            if self.session:
                self.session.stop()
            self.session = session

    def stop(self):
        self.replace(None)

    def tick(self, *, station_url=None, token_file=None, current=None):
        with self._lock:
            session = self.session
            if not session or not session.valid() or (self.worker and self.worker.is_alive()):
                return
            self.worker = Thread(target=self._publish, args=(session, station_url, token_file, current),
                                 name="rp1-display-presence", daemon=True)
            self.worker.start()

    @staticmethod
    def _publish(session, station_url, token_file, current):
        try:
            rows = read_station(station_url, token_file) if station_url and token_file else []
        except (BridgeError, OSError, ValueError, TypeError):
            rows = []
        observed = {row["robot_id"]: row for row in rows}
        for row in _safe_rows([current]):
            # The direct preview proves connectivity only. Runs come from the
            # viewer-authenticated station, and asset links from explicit config.
            observed.setdefault(row["robot_id"], {"robot_id": row["robot_id"], "online": row["online"]})
        for robot_id, previous in session.known.items():
            if robot_id not in observed:
                observed[robot_id] = {key: previous[key] for key in ("robot_id", "model") if key in previous} | {"online": False}
        rows = sorted(observed.values(), key=lambda row: not row["online"])[:MAX_ROBOTS]
        session.known = {row["robot_id"]: row for row in rows}
        if not rows:
            return
        rows = [row | {"asset_id": session.assets[row["robot_id"]]}
                if row["robot_id"] in session.assets else row for row in rows]
        try:
            if session.send(rows):
                session.notice = session.binding_warning or "已上报实机展示状态"
        except BridgeError as exc:
            if exc.status == 403 and exc.code == "whole_machine_asset_unavailable" and any("asset_id" in row for row in rows):
                session.assets = {}
                session.binding_warning = "平台未接受资产关联，下一次仅上报实机状态"
                session.notice = session.binding_warning
            # Older sites and revoked/forbidden sessions quietly stop publication.
            elif exc.status in {401, 403, 404, 405, 409}:
                session.notice = "此平台暂不支持状态上报" if exc.status in {404, 405} else "平台未授权状态上报，已停止"
                session.stop()
            else:
                session.notice = "展示状态暂未送达，将随下一次观察重试"
        except (OSError, ValueError):
            session.notice = "展示状态暂未送达，将随下一次观察重试"
