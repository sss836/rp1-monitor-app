"""Exercise the local hot-swap workflow using simulated preview frames."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from rp1_monitor.desktop_battery_swap import BatterySwapDialog
from rp1_monitor.desktop_result_summary import ResultSummaryPanel
from rp1_monitor.factory_store import FactoryStore


def frame(soc, *, stream, seconds=0, source="simulated"):
    return dict(robot_id="SIMULATED-ROBOT", stream_id=stream, source_kind=source,
                sampled_at=(datetime(2026, 11, 11, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat(),
                values={"battery.soc": soc}, ages_ms={"battery.soc": 100})


@pytest.fixture
def dialog(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = FactoryStore(tmp_path / "pc")
    dialog = BatterySwapDialog("SOC-SYNTHETIC", store)
    dialog.show()
    app.processEvents()
    try:
        yield dialog
    finally:
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()


def test_stage_buttons_wait_for_new_soc_and_require_confirmation(dialog, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("rp1_monitor.desktop_battery_swap.time.monotonic", lambda: clock[0])
    stream = str(uuid4())
    dialog.set_frame(frame(70, stream=stream, source="simulated"))
    dialog.start_button.click()
    first = dialog.stage_buttons["first"]
    first.click()
    assert not first.isEnabled()
    clock[0] = 11.0
    old = frame(70, stream=stream, seconds=1, source="simulated")
    old["ages_ms"]["battery.soc"] = 1100
    dialog.set_frame(old)
    assert not first.isEnabled()
    clock[0] = 12.0
    dialog.set_frame(frame(70, stream=stream, seconds=2, source="simulated"))
    assert first.isEnabled() and "70%" in first.text()
    assert len(dialog.observation_ids) == 1
    first.click()
    dialog.stage_buttons["backup"].click()
    assert "无SOC" in dialog.stage_buttons["backup"].text()
    assert dialog.last_sample["soc"] == 70
    clock[0] = 13.0
    dialog.set_frame(frame(None, stream=stream, seconds=3, source="simulated"))
    assert not dialog.interrupted
    clock[0] = 14.0
    dialog.set_frame(frame(98, stream=stream, seconds=4, source="simulated"))
    assert "无（小电池阶段）" in dialog.soc_label.text() and len(dialog.observation_ids) == 3
    second = dialog.stage_buttons["second"]
    second.click()
    clock[0] = 15.0
    retained = frame(98, stream=stream, seconds=5, source="simulated")
    retained["ages_ms"]["battery.soc"] = 1100
    dialog.set_frame(retained)
    assert not second.isEnabled()
    clock[0] = 16.0
    dialog.set_frame(frame(99, stream=stream, seconds=6, source="simulated"))
    assert second.isEnabled() and "99%" in second.text()
    second.click()
    dialog.finish_button.click()
    rows = dialog.store.soc_observations(dialog.sn)
    assert [(row["stage"], row["soc"]) for row in rows if row["event"] == "stage"] == [
        ("first", 70), ("backup", None), ("second", 99)]
    dialog.store.save_functional_check(dialog.sn, "battery_hot_swap", "incomplete", soc_observation_ids=dialog.observation_ids)
    report = next(row for row in dialog.store.report_rows(dialog.sn) if row["row_key"] == "SYS-008")
    assert "第一块电池 70% → 小电池 无SOC（已标记 " in report["soc_summary"]
    assert "→ 第二块电池 99%" in report["soc_summary"]
    assert "SIMULATED" in report["soc_summary"] and report["verdict"] == "待复核"
    summary_panel = ResultSummaryPanel(dialog.store)
    summary_panel.set_sn(dialog.sn)
    index = next(i for i, row in enumerate(summary_panel._visible_rows) if row["row_key"] == "SYS-008")
    summary_panel.table.setCurrentCell(index, 0)
    assert "小电池 无SOC" in summary_panel.soc_summary.text()
    summary_panel.close()
    summary_panel.deleteLater()


def test_backup_marker_keeps_disconnect_unknown_and_does_not_save_cached_soc(dialog):
    dialog.set_frame(frame(96, stream=str(uuid4()), source="ros2"))
    dialog.start_button.click()
    dialog.mark_disconnected()
    dialog.stage_buttons["backup"].click()
    dialog.finish_button.click()
    baseline, marker = dialog.store.soc_observations(dialog.sn)
    assert baseline["soc"] == 96
    assert marker["stage"] == "backup" and marker["soc"] is None
    assert marker["uptime_seconds"] is None and "连续性未知" in marker["continuity"]


@pytest.mark.parametrize("start_before_frame", [False, True])
def test_bound_sn_mismatch_stops_before_creating_screenshot(dialog, start_before_frame):
    dialog.store.save_board_snapshot(dialog.sn, "BOUND-ROBOT", "ros2", {})
    if start_before_frame:
        dialog.start_button.click()
        assert dialog.watching and dialog.finish_button.isEnabled()
    dialog.set_frame(frame(97, stream=str(uuid4()), source="ros2"))
    assert not dialog.watching and not dialog.start_button.isEnabled() and not dialog.finish_button.isEnabled()
    assert "BOUND-ROBOT" in dialog.notice.text() and "SIMULATED-ROBOT" in dialog.notice.text()
    assert all(not button.isEnabled() for button in dialog.stage_buttons.values())
    dialog.start()
    dialog.finish()
    assert not dialog.observation_ids and not dialog.store.soc_observations(dialog.sn)
    assert not (dialog.store.directory / "function-soc").exists()

    expected = {**frame(96, stream=str(uuid4()), source="ros2"), "robot_id": "BOUND-ROBOT"}
    dialog.set_frame(expected)
    assert not dialog.target_error and dialog.start_button.isEnabled()
    dialog.start_button.click()
    (baseline,) = dialog.store.soc_observations(dialog.sn)
    assert baseline["soc"] == 96 and baseline["robot_id"] == "BOUND-ROBOT"


def test_legacy_swap_readings_are_visible_without_guessing_stages(dialog, tmp_path):
    store, sn = dialog.store, dialog.sn
    stream = str(uuid4())
    dialog.set_frame(frame(0, stream=stream))
    dialog.start_button.click()
    dialog.set_frame(frame(99, stream=stream, seconds=1))
    dialog.finish_button.click()
    rows = store.soc_observations(sn)
    store.save_functional_check(sn, "battery_hot_swap", "pass", soc_observation_ids=dialog.observation_ids)
    observation = {**dialog.current_sample(), "session_id": str(uuid4()), "event": "baseline",
                   "soc": 47, "previous_soc": None}
    unrelated = store.save_soc_observation(sn, observation, store.attachments / rows[0]["attachment_id"])
    summary = next(row for row in store.report_rows(sn) if row["row_key"] == "SYS-008")
    assert "0% → 99% → 99%" in summary["soc_summary"] and "47%" not in summary["soc_summary"]
    assert "未标记电池阶段" in summary["soc_summary"] and summary["verdict"] == "通过"
    path = Path(store.export_result_summary(sn, tmp_path / "export"))
    assert "0% → 99% → 99%" in path.read_text(encoding="utf-8-sig")
    assert store.soc_observations(sn) == [*rows, unrelated]
