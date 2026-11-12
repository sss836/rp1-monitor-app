"""Workstation repository checks using an isolated simulated SQLite fixture."""

import importlib.util
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from rp1_monitor import pc_cli
from rp1_monitor.backend import BackendSettings, create_app
from rp1_monitor.station import StationConnection, create_station_app


@pytest.mark.parametrize("command", ["agent", "demo"])
def test_computer_entry_rejects_robot_commands(monkeypatch, command):
    monkeypatch.setattr(sys, "argv", ["rp1-monitor", command])
    with pytest.raises(SystemExit) as error:
        pc_cli.main()
    assert error.value.code == 2


def test_computer_package_has_no_robot_collectors():
    for module in ("agent", "sources", "health", "host_cpu", "motor_health", "rp_server_source"):
        assert importlib.util.find_spec(f"rp1_monitor.{module}") is None


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.navigation = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "a" and values.get("data-nav") == "true":
            self.navigation.append(values.get("data-page"))


@pytest.mark.parametrize("path", ["/robots", "/robots/SIM-PC/battery", "/robots/SIM-PC/recordings"])
def test_pages_share_workspace_and_browser_cannot_connect_robot(tmp_path, path):
    app = create_station_app(tmp_path / "station", 18980)
    with TestClient(app, base_url="http://127.0.0.1:18980") as client:
        assert client.get("/api/v1/station").status_code == 401
        response = client.get(path)
        assert response.status_code == 200
        assert client.get("/api/v1/station").status_code == 200
        assert client.post("/api/v1/station/disconnect", headers={"Origin": "http://127.0.0.1:18980"}).status_code == 403
        markup = Markup()
        markup.feed(response.text)
        assert len(markup.ids) == len(set(markup.ids))
        assert markup.navigation == ["overview", "battery", "joints", "host", "recordings", "storage", "diagnostics"]


def test_native_light_test_reuses_verified_ssh_and_requires_successful_exit(tmp_path, monkeypatch):
    directory = tmp_path / "station"
    app = create_station_app(directory)
    channel = Mock()
    channel.recv_ready.return_value = False
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = 0
    tunnel = Mock(url="http://127.0.0.1:9123")
    tunnel.transport.is_active.return_value = True
    tunnel.transport.open_session.return_value = channel
    connection = StationConnection(
        tunnel, {"robot_id": "RP1-TEST", "source_kind": "ros2"},
        "robo@robot.test", ("robot.test", 22, 8891), relay_url="http://127.0.0.1:9124",
    )
    app.state.station.connections["RP1-TEST"] = connection
    identity = SimpleNamespace(robot_id="RP1-TEST", source_kind="ros2")
    monkeypatch.setattr("rp1_monitor.factory_client.FactoryClient.probe", lambda _: vars(identity))
    reader = {"Authorization": "Bearer " + (directory / "viewer-token.txt").read_text().strip()}
    native = {**reader, "X-RP1-Station-Token": (directory / "station-token.txt").read_text().strip()}
    path = "/api/v1/station/light-test"
    payload = {"robot_id": "RP1-TEST", "relay_url": connection.relay_url}
    with TestClient(app, base_url="http://127.0.0.1:18980") as client:
        assert client.post(path, json=payload, headers=reader).status_code == 403
        assert client.post(path, json=payload, headers={**native, "Origin": "http://127.0.0.1:18980"}).status_code == 403
        assert client.post(path, json={**payload, "command": "other"}, headers=native).status_code == 422
        assert client.post(path, json={**payload, "relay_url": "http://127.0.0.1:9125"}, headers=native).status_code == 409
        identity.source_kind = "simulated"
        assert client.post(path, json=payload, headers=native).status_code == 409
        tunnel.transport.open_session.assert_not_called()

        identity.source_kind = "ros2"
        result = client.post(path, json=payload, headers=native)
        assert result.status_code == 200
        assert result.json() == {"robot_id": "RP1-TEST", "status": "command_sent"}
        channel.exec_command.assert_called_once_with("LC_ALL=C sudo -n -- /usr/bin/robopi-ws2812 solid 80 80 80")
        channel.get_pty.assert_not_called()
        channel.close.assert_called_once()

        channel.recv_exit_status.return_value = 1
        channel.recv_ready.side_effect = [True, False]
        channel.recv.return_value = b"sudo: a password is required\n"
        assert client.post(path, json=payload, headers=native).json() == {
            "robot_id": "RP1-TEST", "status": "password_required",
        }
        channel.recv_ready.side_effect = None
        channel.recv_exit_status.return_value = 0
        assert client.post(path, json={**payload, "sudo_password": "test-only"}, headers=native).json()["status"] == "command_sent"
        channel.exec_command.assert_called_with("LC_ALL=C sudo -S -p RP1_LIGHT_SUDO: -- /usr/bin/robopi-ws2812 solid 80 80 80")
        channel.sendall.assert_called_once_with(b"test-only\n")
        channel.recv_exit_status.return_value = 127
        assert client.post(path, json=payload, headers=native).status_code == 409


def test_model_directory_requires_auth_and_exposes_no_keys(tmp_path):
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps([]))
    app = create_app(BackendSettings(
        f"sqlite:///{tmp_path / 'isolated.sqlite'}", devices, "test-viewer-" + "x" * 32,
        database_managed=True,
    ))
    with TestClient(app) as client:
        assert client.get("/api/v1/assistant/models").status_code == 401
        result = client.get("/api/v1/assistant/models", headers={"Authorization": "Bearer " + "test-viewer-" + "x" * 32})
        assert result.status_code == 200
        assert {"deepseek", "qwen", "openai"} <= {item["id"] for item in result.json()["items"]}
        assert all("api_key" not in item for item in result.json()["items"])


def test_reviewed_schema_includes_long_term_recording_migration():
    migrations = Path(__file__).parents[1] / "deploy/postgres"
    for number in ("001", "002", "004", "005", "006"):
        assert list(migrations.glob(number + "_*.review.sql"))
    sql = (migrations / "006_monitor_recordings.review.sql").read_text()
    assert "monitor_recordings" in sql and "monitor_recording_samples" in sql
