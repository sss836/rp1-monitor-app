"""Synthetic native recovery receipts; no SSH command is sent to a robot."""

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from rp1_monitor import desktop_operations
from rp1_monitor.factory_basic_checks import FactoryCheckReport
from rp1_monitor.station import StationConnection, create_station_app


def report():
    return FactoryCheckReport(report_id=uuid4(), robot_id="RP1-TEST", source_kind="ros2",
                              created_at=datetime.now(timezone.utc), check_id="BMS_RECOVERY",
                              validation_scope="software_observation_not_hardware_acceptance",
                              result="pass", message="合成新实例数据")


def test_station_checks_origin_target_and_passes_secret_only_on_stdin(tmp_path, monkeypatch):
    app = create_station_app(tmp_path)
    channel = Mock()
    channel.recv_ready.side_effect = [True, False]
    channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = 0
    channel.recv.return_value = report().model_dump_json().encode()
    tunnel = Mock(url="http://127.0.0.1:9123")
    tunnel.transport.is_active.return_value = True
    tunnel.transport.open_session.return_value = channel
    identity = {"robot_id": "RP1-TEST", "source_kind": "ros2"}
    connection = StationConnection(tunnel, identity, "robo@robot.test", ("robot.test", 22, 8891),
                                   relay_url="http://127.0.0.1:9124")
    app.state.station.connections["RP1-TEST"] = connection
    monkeypatch.setattr("rp1_monitor.bms_recovery.FactoryClient.probe", lambda _: identity)
    viewer = {"Authorization": "Bearer " + (tmp_path / "viewer-token.txt").read_text().strip()}
    headers = {**viewer, "X-RP1-Station-Token": (tmp_path / "station-token.txt").read_text().strip()}
    path = "/api/v1/station/bms-recovery"
    body = {"robot_id": "RP1-TEST", "relay_url": connection.relay_url, "sudo_password": "SYNTHETIC_SECRET"}
    with TestClient(app, base_url="http://127.0.0.1:18980") as client:
        assert client.post(path, json=body, headers=viewer).status_code == 403
        assert client.post(path, json=body, headers={**headers, "Origin": "http://127.0.0.1:18980"}).status_code == 403
        assert client.post(path, json={**body, "relay_url": "http://127.0.0.1:9125"}, headers=headers).status_code == 409
        tunnel.transport.open_session.assert_not_called()
        receipt = client.post(path, json=body, headers=headers)
        assert receipt.status_code == 200 and receipt.json()["result"] == "pass"
        assert "SYNTHETIC_SECRET" not in receipt.text and "SYNTHETIC_SECRET" not in str(channel.exec_command.call_args)
        channel.sendall.assert_called_once_with(b"SYNTHETIC_SECRET\n")
        channel.get_pty.assert_not_called()
        channel.close.assert_called_once()


def test_board_prompts_and_saves_native_receipt_without_old_collector_overwrite(monkeypatch):
    app = QApplication.instance() or QApplication([])
    panel = desktop_operations.BoardDiagnosticsPanel(lambda: "http://127.0.0.1:9124")
    workers, saved = [], []

    class Worker(QObject):
        done = Signal(object)
        failed = Signal(str)
        finished = Signal()

        def __init__(self, url, action, body):
            super().__init__()
            self.action, self.body = action, body
            workers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(desktop_operations, "OperationWorker", Worker)
    panel.set_robot("RP1-TEST", source_kind="ros2")
    panel.native_context = lambda: dict(connected=True, robot_id="RP1-TEST", relay_url="http://127.0.0.1:9124",
                                        station_url="http://127.0.0.1:18980", station_token_file="unused")
    panel.apply_status(dict(robot_id="RP1-TEST", source_kind="ros2", enabled=True, factory_basic_version=2))
    panel.select.setCurrentIndex(panel.select.findData("BMS_RECOVERY"))
    panel.report_ready.connect(saved.append)
    monkeypatch.setattr(desktop_operations.QInputDialog, "getText", lambda *args: ("", False))
    panel.request("board_run")
    assert not workers and not saved
    monkeypatch.setattr(desktop_operations.QInputDialog, "getText", lambda *args: ("TEST", True))
    panel.request("board_run")
    assert len(workers) == 1 and workers[0].action == "bms_recovery"
    new_report = report()
    workers[0].done.emit(new_report)
    workers[0].finished.emit()
    assert saved[0]["reports"]["BMS_RECOVERY"]["report_id"] == str(new_report.report_id)
    old = {**report().head(), "result": "error"}
    panel._merge_reports({"checks": [{"id": "BMS_RECOVERY", "latest": old}]})
    assert panel.reports["BMS_RECOVERY"]["report_id"] == str(new_report.report_id)
    panel.close()
    app.processEvents()
