"""Exercise inline maintenance with synthetic responses and no hardware I/O."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rp1_monitor import motor_firmware, motor_parameters, motor_parameters_remote as remote
from rp1_monitor.desktop_motor_maintenance import MotorMaintenancePanel

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait(app, page):
    deadline = time.monotonic() + 5
    while page.worker and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert page.worker is None


def test_same_parameter_values_can_be_selected_and_rewritten_inline(app, monkeypatch):
    address = "can1/6"
    parameters = {
        key: dict(actual=remote.expected(1, key), expected=remote.expected(1, key), matches=True, unit=definition[-1])
        for key, definition in remote.PARAMS.items()
    }
    snapshot = dict(state="checked", snapshot_digest="a" * 64, parameter_standard=remote.PARAMETER_STANDARD,
                    parameter_write_allowed=True, rows=[dict(address=address, model=1, parameters=parameters,
                                                             status="match", errors=[])])
    calls, reports = [], []

    def perform(_, password, request):
        calls.append(request)
        assert password == "fake"
        if request["operation"] == "check":
            return snapshot
        assert reports[-1][2]["before"]["snapshot_digest"] == request["expected_digest"]
        return dict(state="updated")

    monkeypatch.setattr(motor_parameters, "perform", perform)
    panel = MotorMaintenancePanel()
    panel.configure(dict(robot_id="test-robot", target="robo@fake"), "SN-TEST", 24,
                    lambda *entry: reports.append(entry))
    panel.show()
    page = panel.parameters
    assert panel.tabs.currentWidget() is page and not page.isWindow()
    panel.password.setText("fake")
    page.start_check()
    wait(app, page)
    page.confirm.setChecked(True)
    assert not page.update.isEnabled()
    page.rewrite.setChecked(True)
    assert page.table.item(0, 0).checkState() == Qt.CheckState.Unchecked
    page.select_all.click()
    assert page.update.isEnabled()
    page.update.click()
    assert page.review.isVisible() and [call["operation"] for call in calls] == ["check"]
    page.confirm_update()
    wait(app, page)
    assert [call["operation"] for call in calls] == ["check", "rewrite"]
    assert calls[-1]["selected"] == [address]
    assert not panel.busy
    panel.clear_credentials()
    assert not panel.password.text()
    panel.close()


def test_firmware_checks_and_update_share_page_independent_of_current_version(app, monkeypatch):
    release = motor_firmware.firmware_release()
    targets = [dict(interface="can1", motor_id=number, model=1) for number in (6, 7)]
    rows = [dict(**targets[0], eligible=True, status="read", before={"version": "5.0.0.109", "source": "firmware_string"}),
            dict(**targets[1], eligible=True, status="unreadable", before=None),
            dict(interface="can1", motor_id=5, model=3, eligible=False, status="unreadable", before=None)]
    calls, reports = [], []

    def perform(_, password, request):
        calls.append(request)
        assert password == "fake"
        if request["operation"] == "check":
            return dict(state="completed", plan_sha256="a" * 64, rows=rows)
        assert reports[-1][2]["state"] == "requested"
        return dict(state="completed", rows=[dict(row, status="verified") for row in rows if row["eligible"]])

    monkeypatch.setattr(motor_firmware, "perform", perform)
    panel = MotorMaintenancePanel()
    panel.configure(dict(robot_id="test-robot", target="robo@fake"), "SN-TEST", 24, lambda *_: None,
                    save_firmware_report=lambda *entry: reports.append(entry))
    panel.show_section("firmware")
    panel.show()
    page = panel.firmware
    assert panel.tabs.currentWidget() is page and not page.isWindow()
    panel.password.setText("fake")
    page.start_check()
    wait(app, page)
    assert page.table.item(0, 1).text() == "5.0.0.109"
    assert page.table.item(1, 1).text() == "—"
    page.select_all.click()
    assert page.selected() == targets
    assert not page.update.isEnabled()
    page.confirm.setChecked(True)
    page.update.click()
    assert page.review.isVisible() and len(calls) == 1
    panel.show_section("parameters")
    assert not page.review.isVisible()
    page.confirm_update()
    assert len(calls) == 1
    panel.show_section("firmware")
    page.update.click()
    page.confirm_update()
    wait(app, page)
    assert calls[-1]["targets"] == targets and calls[-1]["firmware_sha256"] == release["sha256"]
    assert not page.update.isEnabled()
    assert "版本匹配 2 台" in page.status.text()
    panel.close()


def test_new_stream_replaces_maintenance_session(app):
    panel = MotorMaintenancePanel()
    context = dict(robot_id="test-robot", target="robo@fake")
    panel.configure(context, "SN-TEST", 24, lambda *_: None, session_key="stream-1")
    old_parameters = panel.parameters
    panel.password.setText("fake")
    panel.configure(context, "SN-TEST", 24, lambda *_: None, session_key="stream-2")
    assert panel.parameters is not old_parameters
    assert panel.parameters.snapshot is None and panel.firmware.plan is None
    assert not panel.password.text()
    panel.close()
