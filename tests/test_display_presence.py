"""Isolated evidence for the native display publisher. No real robot or platform."""

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, RLock, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rp1_monitor.display_presence import (
    CENTRAL_PATH,
    LOCAL_PATH,
    DisplayPublisher,
    active_run,
    asset_bindings,
    installation_id,
    observation,
)
from rp1_monitor.fleet_recordings import FleetRecorder
from rp1_monitor.platform_client import PlatformSessionClient
from rp1_monitor.station import create_station_app


@pytest.fixture
def platform_wire():
    calls, statuses = [], []
    user = {"id": str(uuid4()), "kind": "HUMAN", "role": "TEST_EXECUTOR", "must_change_password": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/api/v1/auth/login"
            self.rfile.read(int(self.headers["Content-Length"]))
            self.reply(200, {"data": {"user": user, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}, login=True)

        def do_PUT(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Cookie"), self.headers.get("X-CSRF-Token"), body))
            reply = statuses.pop(0) if statuses else 200
            if isinstance(reply, tuple):
                status, code = reply
                self.reply(status, {"error": {"code": code}})
            else:
                self.reply(reply, {"data": {"accepted": len(body["robots"])}})

        def reply(self, status, data, *, login=False):
            raw = json.dumps(data).encode()
            self.send_response(status)
            if login:
                self.send_header("Set-Cookie", "rp1_session=synthetic-display-session; Path=/; HttpOnly")
                self.send_header("Set-Cookie", "rp1_csrf=synthetic-display-csrf; Path=/")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", user, calls, statuses
        finally:
            server.shutdown()
            thread.join(2)


def pulse(publisher, current=None):
    publisher.tick(current=current)
    if publisher.worker:
        publisher.worker.join(2)
        assert not publisher.worker.is_alive()


def test_real_cookie_csrf_online_then_offline_no_heartbeat_after_stop(tmp_path, platform_wire):
    url, _user, calls, _statuses = platform_wire
    client = PlatformSessionClient(url, allow_http=True)
    assert client.display_session(tmp_path) is None
    assert not list(tmp_path.iterdir())
    client.login("synthetic", "test-only")
    session = client.display_session(tmp_path)
    assert session.transport.cookies is not client.cookies
    publisher = DisplayPublisher()
    publisher.replace(session)
    pulse(publisher)  # no real observation
    assert not calls
    pulse(publisher, observation({"robot_id": "real-1", "source_kind": "ros2", "values": {"secret": 12}}, online=True))
    path, cookie, csrf, body = calls[0]
    assert path == CENTRAL_PATH + session.installation
    assert "rp1_session=synthetic-display-session" in cookie and csrf == "synthetic-display-csrf"
    assert body["robots"] == [{"robot_id": "real-1", "online": True}]
    pulse(publisher)
    assert calls[1][3]["robots"] == [{"robot_id": "real-1", "online": False}]
    pulse(publisher, {"robot_id": "real-1", "online": True, "values": {"raw": 9}, "asset_id": str(uuid4()),
                      "run": {"name": "unverified", "started_at": datetime.now(timezone.utc).isoformat()}})
    assert calls[2][3]["robots"] == [{"robot_id": "real-1", "online": True}]
    publisher.stop()
    pulse(publisher)
    assert len(calls) == 3 and client.session()["authenticated"]
    assert (tmp_path / "display-installation-id").stat().st_mode & 0o777 == 0o600
    assert "synthetic" not in (tmp_path / "display-installation-id").read_text()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_revoked_or_older_site_stops_only_publisher(tmp_path, platform_wire, status):
    url, _user, calls, statuses = platform_wire
    statuses.append(status)
    client = PlatformSessionClient(url, allow_http=True)
    client.login("synthetic", "test-only")
    publisher = DisplayPublisher()
    publisher.replace(client.display_session(tmp_path))
    pulse(publisher, {"robot_id": "real-1", "online": True})
    pulse(publisher, {"robot_id": "real-1", "online": True})
    assert len(calls) == 1 and client.session()["authenticated"]
    assert not publisher.session.valid()


def test_site_account_and_role_isolation(tmp_path, platform_wire):
    url, user, calls, _statuses = platform_wire
    client = PlatformSessionClient(url, allow_http=True)
    user["role"] = "VIEWER"
    client.login("synthetic", "test-only")
    assert client.display_session(tmp_path) is None
    user["role"] = "TEST_EXECUTOR"
    user["must_change_password"] = True
    client.login("synthetic", "test-only")
    assert client.display_session(tmp_path) is None
    user["must_change_password"] = False
    client.login("synthetic", "test-only")
    assert client.display_session(tmp_path) is not None
    assert not calls
    old = installation_id(tmp_path, url, user["id"])
    assert installation_id(tmp_path, url, user["id"]) == old
    assert installation_id(tmp_path, "https://other.invalid", user["id"]) != old
    assert installation_id(tmp_path, url, str(uuid4())) != old


def test_binding_only_matches_explicit_site_and_keeps_unbound_state_on_rejection(tmp_path, platform_wire, monkeypatch):
    url, _user, calls, statuses = platform_wire
    asset = str(uuid4())
    origin = "https://platform.invalid"
    raw = json.dumps({"platform_url": origin, "robots": {"real-1": asset}})
    assert asset_bindings(raw, origin) == {"real-1": asset}
    assert asset_bindings(raw, "https://elsewhere.invalid") == {}
    with pytest.raises(ValueError):
        asset_bindings('{"platform_url":"https://platform.invalid","robots":{"a":"bad","a":"bad"}}', origin)
    client = PlatformSessionClient(url, allow_http=True)
    client.login("synthetic", "test-only")
    monkeypatch.setenv("RP1_PLATFORM_PRESENCE_ASSETS", json.dumps({"platform_url": url, "robots": {"real-1": asset}}))
    session = client.display_session(tmp_path)
    publisher = DisplayPublisher()
    publisher.replace(session)
    statuses.append((403, "whole_machine_asset_unavailable"))
    pulse(publisher, {"robot_id": "real-1", "online": True})
    assert calls[0][3]["robots"][0]["asset_id"] == asset
    assert "未接受资产关联" in session.notice
    pulse(publisher, {"robot_id": "real-1", "online": True})
    assert calls[1][3]["robots"] == [{"robot_id": "real-1", "online": True}]


def test_station_endpoint_uses_real_local_relay_and_active_recording(tmp_path, monkeypatch):
    app = create_station_app(tmp_path / "station")
    token = (tmp_path / "station/viewer-token.txt").read_text().strip()
    start = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat()
    connections = [{"robot": {"robot_id": robot, "source_kind": kind}, "connected": True, "target": "private-target"}
                   for robot, kind in (("live", "ros2"), ("stale", "ros2"), ("sim", "simulated"))]
    monkeypatch.setattr(app.state.station, "status", lambda: {"connections": connections})
    monkeypatch.setattr(app.state.station, "display_connected", lambda robot: robot != "stale")
    run = {"robot_id": "live", "source_kind": "ros2", "run_number": "real-local-run", "started_at": start,
           "case_number": "unverified-label", "metrics": ["raw-sample"]}
    monkeypatch.setattr(app.state.recorder, "display_sessions", lambda _ids: {"live": run, "stale": run})
    try:
        client = TestClient(app, base_url="http://127.0.0.1:18980")
        assert client.get(LOCAL_PATH).status_code == 401
        response = client.get(LOCAL_PATH, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert response.json() == {"robots": [{"robot_id": "live", "online": True, "run": {"name": "real-local-run", "started_at": start}},
                                             {"robot_id": "stale", "online": False}]}
        monkeypatch.setattr(app.state.recorder, "display_sessions", lambda _ids: {})
        assert client.get(LOCAL_PATH, headers={"Authorization": "Bearer " + token}).json() == {
            "robots": [{"robot_id": "live", "online": True}, {"robot_id": "stale", "online": False}],
        }
        assert client.post(LOCAL_PATH, headers={"Authorization": "Bearer " + token}).status_code == 403
    finally:
        app.state.recorder.close()
    assert observation({"robot_id": "sim", "source_kind": "simulated"}, online=True) is None
    assert active_run({**run, "ended_at": start}, "live") is None


@pytest.mark.parametrize("contended", ["fleet", "recorder"])
def test_recording_display_read_skips_contended_locks(contended):
    session = {"robot_id": "real-1", "run_number": "active", "metrics": ["raw-data"]}
    recorder = SimpleNamespace(lock=RLock(), active={"session": session})
    fleet = SimpleNamespace(lock=RLock(), recorders={"real-1": recorder})
    lock = fleet.lock if contended == "fleet" else recorder.lock
    finished, results = Event(), []

    def read():
        results.append(FleetRecorder.display_sessions(fleet, ["real-1"]))
        finished.set()

    thread = Thread(target=read)
    try:
        with lock:
            thread.start()
            assert finished.wait(1), "display must not wait on a recording operation"
            assert results == [{}]
    finally:
        thread.join(2)
    copied = FleetRecorder.display_sessions(fleet, ["real-1"])["real-1"]
    session["run_number"] = "changed"
    assert copied["run_number"] == "active" and "metrics" not in copied


def test_pending_tick_is_not_reused_after_session_replaced(tmp_path, platform_wire, monkeypatch):
    url, user, calls, _statuses = platform_wire
    client = PlatformSessionClient(url, allow_http=True)
    client.login("synthetic", "test-only")
    publisher = DisplayPublisher()
    publisher.replace(client.display_session(tmp_path))
    entered, unblock = Event(), Event()

    def slow_read(*_args):
        entered.set()
        assert unblock.wait(2)
        return [{"robot_id": "old", "online": True}]

    monkeypatch.setattr("rp1_monitor.display_presence.read_station", slow_read)
    publisher.tick(station_url="http://127.0.0.1:1", token_file="unused")
    assert entered.wait(1)
    user["id"] = str(uuid4())
    client.login("synthetic", "test-only")
    new_session = client.display_session(tmp_path)
    publisher.replace(new_session)
    old_worker = publisher.worker
    publisher.tick(current={"robot_id": "new", "online": True})
    assert publisher.worker is old_worker
    unblock.set()
    old_worker.join(2)
    assert not calls
    pulse(publisher, {"robot_id": "new", "online": True})
    assert calls[0][0] == CENTRAL_PATH + new_session.installation
    assert calls[0][3]["robots"] == [{"robot_id": "new", "online": True}]
