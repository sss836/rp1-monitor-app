"""Exercise the encoder monitor UI with a synthetic worker and no SSH or CAN."""

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from rp1_monitor import desktop_joint_angle_monitor as monitor
from rp1_monitor.desktop_joints import JointInspectionPanel


class FakeWorker(QObject):
    received = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, connection, password, dof, session_id, parent):
        super().__init__(parent)
        self.connection = connection
        self.dof = dof
        self.session_id = session_id
        self.cancel_event = threading.Event()

    def start(self):
        pass

    def send(self, kind, **fields):
        self.received.emit(dict(type=kind, session_id=self.session_id, **fields))


@pytest.fixture
def setup(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    known_hosts = tmp_path / "known_hosts"
    known_hosts.touch()
    binding = dict(robot_id="RP1-A", target="robo@192.168.137.250", connected=True, known_hosts=known_hosts)
    status = dict(robot_id="RP1-A", stream_id="stream-A", source_kind="ros2", connected=True,
                  can_preflight=True, configured_dof=24)
    clock = SimpleNamespace(monotonic=lambda: 30.0)
    monkeypatch.setattr(monitor, "time", clock)
    monkeypatch.setattr(monitor, "JointAngleWorker", FakeWorker)
    panel = JointInspectionPanel(lambda: binding)
    panel.calibration_context = lambda: status
    panel.resize(820, 700)
    panel.show()
    panel.monitor_source.setCurrentIndex(panel.monitor_source.findData("encoder"))
    app.processEvents()
    try:
        yield panel, binding, status, clock
    finally:
        reader = panel.joint_angle_monitor
        if reader.worker:
            reader.worker.finished.emit()
        panel.close()
        panel.deleteLater()
        app.processEvents()


def start_reader(panel):
    reader = panel.joint_angle_monitor
    reader.password.setText("synthetic test password")
    reader.start.click()
    assert reader.worker is not None
    return reader


def test_encoder_reading_is_explicit_and_expires_missing_or_old_rows(setup):
    panel, _, _, clock = setup
    assert panel.monitor_source.count() == 3
    assert panel.joint_angle_monitor.worker is None
    reader = start_reader(panel)
    worker = reader.worker
    assert reader.password.text() == ""
    assert worker.dof == 24
    worker.send("metadata", dof=24, joint_names=[f"joint_{i}" for i in range(24)])
    worker.send("frame", positions_deg=[-12.5] + [None] * 23, rx_monotonic_ns=[9_000_000_000] + [None] * 23,
                frame_monotonic_ns=10_000_000_000, sweep_s=1.15)
    assert reader.table.item(0, 0).text() == "joint_0"
    assert reader.table.item(0, 1).text() == "-12.50"
    assert reader.table.item(1, 1).text() == "未知"
    assert reader.table.item(1, 2).text() == "缺失"
    clock.monotonic = lambda: 32.0
    reader.refresh()
    assert reader.table.item(0, 1).text() == "未知"
    assert reader.table.item(0, 2).text() == "过期"


@pytest.mark.parametrize("change", ["source", "stream", "binding", "disconnect", "hide"])
def test_encoder_stops_on_navigation_or_robot_change(setup, change):
    panel, binding, status, _ = setup
    reader = start_reader(panel)
    worker = reader.worker
    if change == "source":
        panel.monitor_source.setCurrentIndex(panel.monitor_source.findData("reported"))
    elif change == "stream":
        status["stream_id"] = "new-stream"
        reader.refresh()
    elif change == "binding":
        binding["generation"] = 2
        reader.refresh()
    elif change == "disconnect":
        panel.mark_disconnected()
    else:
        panel.hide()
    assert worker.cancel_event.is_set()
    worker.send("metadata", dof=24, joint_names=[f"joint_{i}" for i in range(24)])
    assert reader.table.rowCount() == 0


def test_raw_query_and_control_cannot_overlap_encoder_session(setup):
    panel, _, _, _ = setup
    reader = start_reader(panel)
    panel.open_motor_control()
    assert panel.motor_control_dialog is None
    panel.monitor_source.setCurrentIndex(panel.monitor_source.findData("motor"))
    assert reader.worker.cancel_event.is_set()
    assert not panel.postzero_dialog.isEnabled()
    reader.worker.finished.emit()
    assert panel.postzero_dialog.isEnabled()
