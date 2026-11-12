import asyncio
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from zipfile import ZipFile

import pytest

from rp1_monitor import motor_firmware as client
from rp1_monitor import motor_firmware_remote as remote
from rp1_monitor.station import MotorFirmwareRequest, StationConnection, create_station_app

TARGET = {"interface": "can0", "motor_id": 2, "model": 1}
NEXT = {"interface": "can0", "motor_id": 3, "model": 1}
CHECK = {"operation": "check", "request_id": str(uuid4()), "robot_id": "fixture-robot", "dof": 24}
PAYLOAD = client.load_bundle()[0]


def update_request(plan, targets=None):
    return {**CHECK, "operation": "update", "request_id": str(uuid4()),
            "plan_sha256": plan, "targets": targets or [TARGET], "maintenance_confirmed": True,
            "firmware_sha256": remote.FIRMWARE["sha256"]}


def test_bundled_release_rejects_missing_corrupt_and_tampered_zip(tmp_path):
    assert client.firmware_release(verify=True)["build"] == "202610281537"
    path = tmp_path / "firmware.zip"
    with pytest.raises(ValueError, match="缺失或损坏"):
        client.load_bundle(path)
    path.write_bytes(b"not-a-zip")
    with pytest.raises(ValueError, match="缺失或损坏"):
        client.load_bundle(path)
    with ZipFile(path, "w") as archive:
        archive.writestr(remote.FIRMWARE["filename"], bytes(len(PAYLOAD)))
    with pytest.raises(ValueError, match="SHA-256"):
        client.load_bundle(path)


class FakeOta:
    class FlashError(Exception):
        pass

    def __init__(self):
        self.opened = []
        self.written = []

    @contextmanager
    def LinuxFlasher(self, interface):
        self.opened.append(interface)
        yield self

    def flash(self, path, ota_id):
        assert Path(path).read_bytes() == PAYLOAD
        self.written.append(ota_id)


@pytest.fixture
def fake_board(monkeypatch):
    monkeypatch.setattr(remote, "maintenance_window", lambda _: nullcontext())
    monkeypatch.setattr(remote, "configuration", lambda _: [TARGET, NEXT])
    monkeypatch.setattr(remote, "guard_processes", lambda: None)
    monkeypatch.setattr(remote, "read_version", lambda target: None)
    monkeypatch.setattr(remote, "check_can_bus", lambda interface: None)
    return FakeOta()


def test_unreadable_before_remains_eligible_and_selected_ota_is_verified(monkeypatch, fake_board):
    check = remote.run_operation(CHECK)
    assert check["state"] == "completed"
    assert check["rows"][0]["eligible"] and check["rows"][0]["before"] is None
    assert check["rows"][0]["status"] == "unreadable"
    assert fake_board.written == []
    after = {"version": "5.0.0.109", "source": "firmware_string"}
    monkeypatch.setattr(remote, "wait_for_version", lambda _: after)
    result = remote.run_operation(update_request(check["plan_sha256"]),
                                  receive_payload=lambda: PAYLOAD, ota=fake_board)
    assert fake_board.written == [0x1003]
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["before"] is None and row["after"] == after
    assert row["status"] == "verified" and row["ota_completed"]


def test_old_readback_never_reports_verified_and_pauses_same_bus(monkeypatch, fake_board):
    old = {"version": "5.0.0.108", "source": "firmware_string"}
    monkeypatch.setattr(remote, "read_version", lambda _: old)
    monkeypatch.setattr(remote, "wait_for_version", lambda _: old)
    plan = remote.run_operation(CHECK)["plan_sha256"]
    result = remote.run_operation(update_request(plan, [TARGET, NEXT]),
                                  receive_payload=lambda: PAYLOAD, ota=fake_board)
    assert fake_board.written == [0x1003]
    assert [row["status"] for row in result["rows"]] == ["version_mismatch", "not_attempted"]
    assert result["rows"][0]["before"] == result["rows"][0]["after"] == old


def test_same_109_version_is_not_an_ota_gate(monkeypatch, fake_board):
    version = {"version": "5.0.0.109", "source": "firmware_string"}
    monkeypatch.setattr(remote, "read_version", lambda _: version)
    monkeypatch.setattr(remote, "wait_for_version", lambda _: version)
    plan = remote.run_operation(CHECK)["plan_sha256"]
    result = remote.run_operation(update_request(plan), receive_payload=lambda: PAYLOAD, ota=fake_board)
    assert fake_board.written == [0x1003]
    assert result["rows"][0]["status"] == "verified"


def test_partial_payload_or_unverified_plan_cannot_open_ota(fake_board):
    plan = remote.run_operation(CHECK)["plan_sha256"]
    result = remote.run_operation(update_request(plan), receive_payload=lambda: PAYLOAD[:100], ota=fake_board)
    assert result["state"] == "blocked"
    assert "载荷" in result["detail"]
    assert fake_board.opened == []
    result = remote.run_operation(update_request("0" * 64), receive_payload=pytest.fail, ota=fake_board)
    assert result["state"] == "blocked"
    assert "检查结果不同" in result["detail"]
    result = remote.run_operation(update_request(plan, [{**TARGET, "model": 2}]),
                                  receive_payload=pytest.fail, ota=fake_board)
    assert result["state"] == "blocked"
    assert "所选电机" in result["detail"]
    assert fake_board.opened == []


def test_three_part_numeric_reply_cannot_prove_1028_build():
    assert remote.version_status({"version": "0.1.9", "source": "numeric"})[0] == "version_unconfirmed"


@pytest.mark.parametrize("ask_for_password", [True, False])
def test_ssh_binary_sent_only_after_ready_without_password_prefix(monkeypatch, ask_for_password):
    request = update_request("a" * 64)
    data_to_send = b"fixture"
    received = []
    monkeypatch.setattr(client, "load_bundle", lambda: (data_to_send, b""))
    result = {"request_id": request["request_id"], "state": "completed"}

    class Channel:
        challenge = ask_for_password
        sent_payload = False
        sent_result = False
        ready_sent = False

        def settimeout(self, timeout):
            pass

        def exec_command(self, command):
            assert "fixed-sudo-password" not in command

        def sendall(self, data):
            received.append(data)
            if data == data_to_send:
                self.sent_payload = True

        def shutdown_write(self):
            pass

        def recv_ready(self):
            return not self.challenge and not self.sent_result

        def recv(self, _):
            if not self.ready_sent:
                self.ready_sent = True
                return remote.PAYLOAD_READY
            assert self.sent_payload
            self.sent_result = True
            return b'{"state":"completed","request_id":"' + request["request_id"].encode() + b'"}'

        def recv_stderr_ready(self):
            return self.challenge

        def recv_stderr(self, _):
            self.challenge = False
            return client.SUDO_PROMPT

        def exit_status_ready(self):
            return self.sent_result

        def close(self):
            pass

    tunnel = SimpleNamespace(transport=SimpleNamespace(open_session=lambda **_: Channel()))
    assert client._remote(tunnel, "fixed-sudo-password", request) == result
    assert received == ([b"fixed-sudo-password\n"] if ask_for_password else []) + [data_to_send]


def test_station_requires_native_credentials_matching_identity_and_maintenance_lock(tmp_path, monkeypatch):
    directory = tmp_path / "station"
    app = create_station_app(directory)
    robot_id = CHECK["robot_id"]
    tunnel = Mock()
    tunnel.transport.is_active.return_value = True
    connection = StationConnection(
        tunnel, {"robot_id": robot_id, "source_kind": "ros2"},
        "robo@robot.test", ("robot.test", 22, 8891), relay_url="http://127.0.0.1:9124",
    )
    app.state.station.connections[robot_id] = connection
    perform = Mock(return_value={"state": "completed"})
    monkeypatch.setattr(client, "perform", perform)
    dispatch = app.user_middleware[0].kwargs["dispatch"]
    path = SimpleNamespace(path="/api/v1/station/motor-firmware")
    base_headers = {"host": "127.0.0.1:18980"}
    native_token = (directory / "station-token.txt").read_text().strip()
    for extra in ({}, {"origin": "http://127.0.0.1:18980", "x-rp1-station-token": native_token}):
        request = SimpleNamespace(url=path, method="POST", headers={**base_headers, **extra})
        assert asyncio.run(dispatch(request, pytest.fail)).status_code == 403
    manager = app.state.station
    payload = MotorFirmwareRequest(robot_id=robot_id, relay_url=connection.relay_url, request=CHECK,
                                   sudo_password="fixture")
    mismatch = payload.model_copy(update={"request": dict(CHECK, robot_id="other-robot")})
    with pytest.raises(ValueError, match="身份不匹配"):
        manager.motor_firmware(mismatch)
    with connection.deploy_lock, pytest.raises(ValueError, match="已有电机维护任务"):
        manager.motor_firmware(payload)
    perform.assert_not_called()
    assert manager.motor_firmware(payload) == {"state": "completed"}
    perform.assert_called_once_with({"robot_id": robot_id, "target": connection.target, "tunnel": tunnel},
                                    "fixture", CHECK)
