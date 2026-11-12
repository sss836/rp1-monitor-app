"""Exercise motion recording in the native UI against an isolated relay."""

import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from rp1_monitor.desktop_factory import FactoryPanel
from rp1_monitor.factory_client import FactoryClient
from rp1_monitor.factory_models import FactorySegmentInfo

SN = "SYNTHETIC-MOTION"
URL = "http://127.0.0.1:19191"
SEGMENTS = "/api/v1/factory/segments"


class Relay:
    identity = {"robot_id": "SIM-MOTION", "source_kind": "simulated"}

    def __init__(self):
        self.calls = []
        self.segments = []
        self.active = None
        self.reachable = True
        self.start_outcomes = []
        self.start_ids = []

    def recording(self, test_type, sn=SN):
        self.active = str(uuid4())
        row = FactorySegmentInfo(
            segment_id=self.active, sn=sn, **self.identity, test_type=test_type,
            state="recording", started_at=datetime.now(timezone.utc) - timedelta(seconds=14),
            sample_counts={"motion": 100, "health": 200},
            valid_counts={"motion": 97, "health": 193},
        ).model_dump(mode="json")
        self.segments = [row]
        return row

    def respond(self, request):
        self.calls.append((request.method, request.url.path))
        if not self.reachable:
            raise httpx.ConnectError("isolated relay disconnected", request=request)
        if request.method == "GET" and request.url.path == "/api/v1/state":
            return httpx.Response(200, json=self.frame())
        if request.method == "GET" and request.url.path == SEGMENTS:
            return httpx.Response(200, json={"segments": self.segments, "active_segment_id": self.active})
        if request.method == "POST" and request.url.path == SEGMENTS and self.start_outcomes:
            body = json.loads(request.content)
            self.start_ids.append(body["segment_id"])
            if self.start_outcomes.pop(0) == "lost":
                raise httpx.ConnectError("isolated relay lost start response", request=request)
            row = FactorySegmentInfo(**body, state="recording", started_at=datetime.now(timezone.utc)).model_dump(mode="json")
            self.active = row["segment_id"]
            self.segments = [row]
            return httpx.Response(200, json=row)
        return httpx.Response(409)

    def frame(self):
        return {**self.identity, "upload": {"stage": "upper_computer", "collector_connected": self.reachable}}


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "factory panel did not reach the expected state"


@pytest.fixture
def panel_and_relay(app, tmp_path, monkeypatch):
    relay = Relay()
    monkeypatch.setattr(FactoryClient, "_client", lambda _: httpx.Client(transport=httpx.MockTransport(relay.respond)))
    panel = FactoryPanel(lambda: URL, directory=tmp_path / "factory")
    panel.store.set_model_dof(SN, 24)
    panel.activate_sn(SN)
    panel.workbench.select_case("SYS-009")
    panel.show()

    def offer_preview_frame():
        if relay.reachable:
            panel.set_frame(relay.frame())

    preview = QTimer(panel)
    preview.timeout.connect(offer_preview_frame)
    preview.start(500)
    yield panel, relay
    preview.stop()
    wait_for(app, panel.shutdown)
    panel.close()
    app.processEvents()


def test_entering_motion_view_loads_and_updates_live_counts_without_writes(app, panel_and_relay):
    panel, relay = panel_and_relay
    relay.recording("standing")
    panel.set_frame(relay.frame())
    wait_for(app, lambda: bool(panel.remote_rows))
    assert panel.remote_rows[0]["valid_counts"] == {"motion": 97, "health": 193}
    assert panel.motion_valid_points.text() == "97"
    assert panel.motion_health_points.text() == "193"
    assert not panel.start_button.isEnabled()
    assert panel.read_button.isEnabled()

    relay.segments[0]["valid_counts"]["motion"] = 99
    panel.set_frame(relay.frame())
    wait_for(app, lambda: panel.remote_rows[0]["valid_counts"]["motion"] == 99)
    assert ("GET", SEGMENTS) in relay.calls
    assert all(method == "GET" for method, _ in relay.calls)


@pytest.mark.parametrize("active_type,active_sn", [("walking", SN), ("running", SN), ("standing", "OTHER-SN")])
def test_standing_controls_cannot_start_or_seal_another_active_segment(app, panel_and_relay, active_type, active_sn):
    panel, relay = panel_and_relay
    relay.recording(active_type, active_sn)
    panel.set_frame(relay.frame())
    wait_for(app, lambda: bool(panel.remote_rows))
    assert not panel.start_button.isEnabled()
    assert not panel.read_button.isEnabled()

    panel.start_segment()
    wait_for(app, lambda: not panel.busy)
    panel.read_data()
    wait_for(app, lambda: not panel.busy)
    assert all(method == "GET" for method, _ in relay.calls)
    assert not list((panel.store.directory / "start-intents").glob("*.json"))


def test_start_checks_newly_created_active_segment_before_sending_post(app, panel_and_relay):
    panel, relay = panel_and_relay
    panel.set_frame(relay.frame())
    wait_for(app, panel.start_button.isEnabled)

    relay.recording("walking")
    panel.start_segment()
    wait_for(app, lambda: not panel.busy)
    assert all(method == "GET" for method, _ in relay.calls)


def test_finish_does_not_seal_replacement_segment_after_confirmation(app, panel_and_relay, monkeypatch):
    panel, relay = panel_and_relay
    original = relay.recording("standing")["segment_id"]
    panel.set_frame(relay.frame())
    wait_for(app, panel.read_button.isEnabled)

    def confirm(*_):
        relay.recording("standing")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    panel.read_button.click()
    wait_for(app, lambda: not panel.busy)
    assert relay.active != original
    assert all(method == "GET" for method, _ in relay.calls)


def test_pending_start_only_retries_on_click_and_reuses_segment_id(app, panel_and_relay):
    panel, relay = panel_and_relay
    relay.start_outcomes = ["lost", "accepted"]
    panel.set_frame(relay.frame())
    wait_for(app, panel.start_button.isEnabled)

    panel.start_button.click()
    wait_for(app, panel.motion_recovery.isVisible)
    assert len(relay.start_ids) == 1
    assert not panel.start_button.isEnabled()

    panel.motion_retry_button.click()
    wait_for(app, panel.read_button.isEnabled)
    assert relay.start_ids == [relay.active, relay.active]
    assert not panel.motion_recovery.isVisible()


def test_disconnect_freezes_last_confirmed_duration(app, panel_and_relay):
    panel, relay = panel_and_relay
    relay.recording("standing")
    panel.set_frame(relay.frame())
    wait_for(app, lambda: bool(panel.remote_rows))

    relay.reachable = False
    panel.mark_disconnected()
    panel.update_duration()
    assert not panel.start_button.isEnabled()
    assert not panel.read_button.isEnabled()
    assert "待确认" in panel.motion_state_label.text()
    assert panel.motion_valid_points.text() == "97"
    frozen = panel.duration_label.text()
    disconnected_at = time.monotonic()
    wait_for(app, lambda: time.monotonic() - disconnected_at >= 1.1)
    panel.update_duration()
    assert panel.duration_label.text() == frozen
    assert all(method == "GET" for method, _ in relay.calls)


def test_csv_action_exports_incomplete_report_while_recording(app, panel_and_relay, tmp_path):
    panel, relay = panel_and_relay
    relay.recording("standing")
    panel.set_frame(relay.frame())
    wait_for(app, lambda: bool(panel.remote_rows))

    directory = tmp_path / "exports"
    panel.result_summary.export_directory = directory
    button = next(button for button in panel.workbench.current_page.findChildren(QPushButton)
                  if button.text() == "导出报告 CSV")
    assert button.isEnabled()
    button.click()
    wait_for(app, lambda: not panel.result_summary.busy and bool(list(directory.glob("*.csv"))))
    [path] = directory.glob("*.csv")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert any(row["测试项目编号"] == "SYS-009" and row["是否通过"] == "未测试" for row in rows)
    assert all(method == "GET" for method, _ in relay.calls)
