"""Qt workflow tests using synthetic encoder replies and a fake reader."""

import threading
import time
from uuid import uuid4

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from rp1_monitor import desktop_joint_angle_monitor
from rp1_monitor.desktop_joints import JointInspectionPanel
from rp1_monitor.postzero import CONFIGURATION_SHA256


class FakeWorker(QObject):
    received = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, connection, password, dof, session_id, parent):
        super().__init__(parent)
        self.dof = dof
        self.session_id = session_id
        self.cancel_event = threading.Event()

    def start(self):
        pass

    def isRunning(self):
        return not self.cancel_event.is_set()

    def send(self, kind, **fields):
        self.received.emit(dict(type=kind, session_id=self.session_id, **fields))


@pytest.fixture
def dialog_setup(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    known_hosts = tmp_path / "known_hosts"
    known_hosts.touch()
    connection = dict(robot_id="RP1-A", target="robo@192.168.137.250", connected=True, known_hosts=known_hosts)
    context = dict(robot_id="RP1-A", stream_id=str(uuid4()), source_kind="ros2", connected=True,
                   can_preflight=True, configured_dof=24)
    monkeypatch.setattr(desktop_joint_angle_monitor, "JointAngleWorker", FakeWorker)
    archived = []

    def save(report):
        archived.append(report)
        return True

    panel = JointInspectionPanel(lambda: connection)
    panel.calibration_context = lambda: context
    panel.show()
    panel.open_zero_reference_check("sample-24", save)
    dialog = panel.zero_reference_dialog
    app.processEvents()
    try:
        yield panel, dialog, context, archived
    finally:
        if dialog.reader.worker:
            dialog.reject()
            dialog.reader.worker.finished.emit()
        elif panel.zero_reference_dialog:
            dialog.reject()
        panel.close()
        panel.deleteLater()
        app.processEvents()


def start(dialog):
    assert dialog.reader.worker is None
    dialog.reader.password.setText("test password")
    assert not dialog.reader.start.isEnabled()
    dialog.site_ready.setChecked(True)
    dialog.reader.start.click()
    worker = dialog.reader.worker
    assert worker.dof == 24
    assert dialog.reader.password.text() == ""
    rows = dialog.engine.report()["results"]
    worker.send("metadata", dof=24, joint_names=[row["joint"] for row in rows],
                configuration_sha256=CONFIGURATION_SHA256[24])
    return worker


def frame(worker, sequence, *, valid=True, received_ns=None):
    frame_ns = sequence * 1_000_000_000
    stamps = [frame_ns - (24 - index) * 1_000_000 for index in range(24)]
    worker.send(
        "frame", sequence=sequence, frame_monotonic_ns=frame_ns, sweep_s=1.0,
        positions_deg=[1.25 if valid else None] * 24,
        rx_monotonic_ns=stamps if valid else [None] * 24,
        raw_angles_deg=[0.0 if valid else None] * 24,
        raw_rx_monotonic_ns=stamps if valid else [None] * 24,
        raw_received_at=[f"2026-11-12T04:00:{sequence:02d}.000001000Z" if valid else None] * 24,
        pc_received_monotonic_ns=received_ns if received_ns is not None else time.monotonic_ns(),
    )


def test_confirmed_samples_advance_and_archive_to_original_sn(dialog_setup):
    _, dialog, context, archived = dialog_setup
    worker = start(dialog)
    frame(worker, 1)
    assert "当前 +1.25°" in dialog.detail.text()
    assert dialog.engine.report()["current"]["sample_count"] == 0
    queued_ns = time.monotonic_ns()
    dialog.arm_button.click()
    frame(worker, 2, received_ns=queued_ns)
    frame(worker, 3, received_ns=queued_ns)
    for sequence in range(4, 7):
        frame(worker, sequence)
    assert dialog.engine.report()["current"]["sample_count"] == 2
    frame(worker, 7)
    report = dialog.engine.report()
    assert report["results"][0]["status"] == "passed"
    assert report["results"][0]["sample_count"] == 3
    assert report["current_index"] == 1
    dialog.reader.stop.click()
    assert worker.cancel_event.is_set()
    worker.finished.emit()
    assert len(archived) == 1
    record = archived[0]
    assert record["sn"] == "sample-24" and record["stream_id"] == context["stream_id"]
    assert record["report"]["results"][0]["status"] == "passed"
    assert record["report"]["phase"] != "complete"


def test_ankle_pair_missing_is_unknown_and_confirmed_incomplete_is_archived(dialog_setup):
    _, dialog, _, archived = dialog_setup
    worker = start(dialog)
    rows = dialog.engine.report()["results"]
    ankle = next(row for row in rows if row["joint"] == "left_ankle_pitch_joint")
    dialog.table.selectRow(ankle["index"])
    dialog.retest_button.click()
    frame(worker, 1, valid=False)
    assert "左踝" in dialog.current.text()
    assert dialog.detail.text().count("未知") == 4
    for motor in ankle["motors"]:
        assert f"{motor['interface']}/{motor['motor_id']} 未知" in dialog.detail.text()
    dialog.arm_button.click()
    frame(worker, 2, valid=False)
    frame(worker, 3, valid=False)
    dialog.reader.stop.click()
    worker.finished.emit()
    assert len(archived) == 1
    saved = archived[0]["report"]["results"][ankle["index"]]
    assert saved["status"] != "passed"
    assert saved["attempts"][-1]["status"] == "interrupted"
    assert saved["attempts"][-1]["samples"] == []


def test_robot_change_stops_session_without_archiving_unstarted_check(dialog_setup):
    panel, dialog, context, archived = dialog_setup
    worker = start(dialog)
    panel.open_motor_control()
    assert panel.motor_control_dialog is None
    context["stream_id"] = str(uuid4())
    dialog.reader.refresh()
    assert worker.cancel_event.is_set()
    worker.finished.emit()
    assert not archived
    assert not dialog.arm_button.isEnabled()
