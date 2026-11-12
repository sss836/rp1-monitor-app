"""Synthetic evidence only: the current policy is not a hardware verdict."""

import copy
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rp1_monitor import factory_result_summary, factory_store
from rp1_monitor.desktop_factory import FactoryPanel
from rp1_monitor.factory_codec import encode_payload
from rp1_monitor.factory_duration_policy import duration_requirement, duration_status
from rp1_monitor.factory_store import FactoryStore


@pytest.mark.parametrize("duration,met", [(7199.9, False), (7200, True), (7200.1, True), (None, False)])
def test_standing_has_inclusive_two_hour_minimum(duration, met):
    result = duration_requirement("standing", duration)
    assert result == {"duration_required": True, "minimum_test_seconds": 7200, "duration_satisfied": met}


@pytest.mark.parametrize("kind", ["walking", "running", "stunt"])
@pytest.mark.parametrize("duration", [0, 1, 7199, None])
def test_other_motion_has_no_two_hour_requirement(kind, duration):
    result = duration_requirement(kind, duration)
    assert result == {"duration_required": False, "minimum_test_seconds": None, "duration_satisfied": None}
    assert duration_status({**result, "duration_seconds": duration}) == "无两小时时长要求"


def test_segment_interpretation_preserves_robot_original_summary(tmp_path, monkeypatch):
    store = FactoryStore(tmp_path / "pc")
    db_path = tmp_path / "evidence.sqlite"
    original = {"schema_version": "rp1.factory.key-data.v1", "duration_seconds": 7200,
                "minimum_test_seconds": 7200, "duration_satisfied": False, "metrics": {}}
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE metadata (key TEXT, value BLOB)")
        db.execute("CREATE TABLE samples (elapsed_seconds REAL)")
        db.executemany("INSERT INTO metadata VALUES(?, ?)", [
            ("recording_duration_seconds", json.dumps(7200)),
            ("key_data_summary", encode_payload(original)),
        ])
    before = db_path.read_bytes()
    monkeypatch.setattr(store, "_segment", lambda _: {"segment_id": str(uuid4()), "test_type": "standing"})

    @contextmanager
    def evidence(_):
        with sqlite3.connect(db_path) as db:
            yield db

    monkeypatch.setattr(factory_store, "_open_evidence", evidence)
    summary = store.segment_summary(str(uuid4()))
    assert summary["duration_satisfied"] is True
    assert summary["source_duration_satisfied"] is False
    assert summary["duration_seconds"] == 7200
    assert db_path.read_bytes() == before
    store.close()


def test_legacy_group_uses_latest_standing_and_retains_short_attempts(tmp_path, monkeypatch):
    store = FactoryStore(tmp_path / "pc")
    records = [
        {"test_type": "standing", "started_at": "2026-11-11T01:00:00+00:00", "segment_id": "a", "state": "sealed"},
        {"test_type": "walking", "started_at": "2026-11-11T02:00:00+00:00", "segment_id": "b", "state": "sealed"},
        {"test_type": "standing", "started_at": "2026-11-11T03:00:00+00:00", "segment_id": "c", "state": "sealed"},
        {"test_type": "running", "started_at": "2026-11-11T04:00:00+00:00", "segment_id": "d", "state": "sealed"},
    ]
    monkeypatch.setattr(store, "list_segments", lambda _: records)
    monkeypatch.setattr(store, "segment_summary", lambda key: {"duration_satisfied": key == "c"})
    assert store.item_duration_satisfied("SN", 4)
    records[2]["state"] = "interrupted"
    assert not store.item_duration_satisfied("SN", 4)
    assert store.item_duration_satisfied("SN", 5)
    assert len(records) == 4
    store.close()


def test_prior_manual_result_survives_rule_change_only_with_same_evidence(tmp_path, monkeypatch):
    new_rules = copy.deepcopy(factory_result_summary._rules())
    old_rules = copy.deepcopy(new_rules)
    for row in old_rules["rows"]:
        key = row["case_code"] + ("/" + row["source_id"] if row["level"] == "子项" else "")
        if key in factory_result_summary._LEGACY_MOTION_RULES:
            row["criterion"], row["criterion_source"] = factory_result_summary._LEGACY_MOTION_RULES[key]
    monkeypatch.setattr(factory_result_summary, "_rules", lambda: old_rules)
    store = FactoryStore(tmp_path / "pc")
    row = next(x for x in store.report_rows("SN") if x["row_key"] == "SYS-010/walking")
    store.save_report_edits("SN", [{"row_key": row["row_key"], "source_token": row["source_token"],
                                    "fields": {"verdict": "通过", "notes": "现场人工观察"}}])
    monkeypatch.setattr(factory_result_summary, "_rules", lambda: new_rules)
    migrated = next(x for x in store.report_rows("SN") if x["row_key"] == row["row_key"])
    assert migrated["verdict"] == "通过" and migrated["notes"] == "现场人工观察"
    assert "无两小时时长要求" in migrated["criterion"]
    store.set_model_dof("SN", 28)
    changed = next(x for x in store.report_rows("SN") if x["row_key"] == row["row_key"])
    assert changed["verdict"] != "通过"
    store.close()


def test_new_standing_pass_requires_two_hour_evidence_but_other_actions_do_not(tmp_path, monkeypatch):
    store = FactoryStore(tmp_path / "pc")
    rows = {row["row_key"]: row for row in store.report_rows("SN")}

    def edit(key, verdict):
        return {"row_key": key, "source_token": rows[key]["source_token"], "fields": {"verdict": verdict}}

    with pytest.raises(ValueError, match="站立记录尚未达到2小时"):
        store.save_report_edits("SN", [edit("SYS-009", "通过")])
    for key in ("SYS-010/walking", "SYS-010/running", "SYS-011"):
        rows = {row["row_key"]: row for row in store.report_rows("SN")}
        store.save_report_edits("SN", [edit(key, "通过")])
    rows = {row["row_key"]: row for row in store.report_rows("SN")}
    assert all(rows[key]["verdict"] == "通过" for key in ("SYS-010/walking", "SYS-010/running", "SYS-011"))
    assert all(rows[key]["source_kind"] == "human" for key in ("SYS-010/walking", "SYS-010/running", "SYS-011"))
    monkeypatch.setattr(store, "item_duration_satisfied", lambda sn, item: True)
    store.save_report_edits("SN", [edit("SYS-009", "通过")])
    assert next(row for row in store.report_rows("SN") if row["row_key"] == "SYS-009")["verdict"] == "通过"
    store.close()


def test_28_axis_stunt_remains_not_applicable(tmp_path):
    store = FactoryStore(tmp_path / "pc")
    store.set_model_dof("SN", 28)
    row = next(row for row in store.report_rows("SN") if row["row_key"] == "SYS-011")
    assert row["verdict"] == "不适用"
    with pytest.raises(ValueError, match="28轴"):
        store.save_report_edits("SN", [{"row_key": "SYS-011", "source_token": row["source_token"],
                                        "fields": {"verdict": "通过"}}])
    store.close()


def test_factory_screen_displays_current_requirements(tmp_path):
    app = QApplication.instance() or QApplication([])
    panel = FactoryPanel(lambda: None, directory=tmp_path / "pc")
    panel.workbench.select_case("SYS-009")
    panel.update_duration()
    assert any(value in panel.motion_duration_caption.text() for value in ("2小时", "02:00:00"))
    panel.workbench.select_case("SYS-011")
    panel.update_duration()
    assert "无两小时时长要求" in panel.motion_duration_caption.text()
    assert "超过2小时" not in panel.manual_deploy_guide.text()
    panel.shutdown()
    panel.close()
    app.processEvents()


def test_short_stunt_manual_result_not_rejected_for_duration():
    row = {"actual_result": "", "evidence": "", "verdict": "未测试", "_inputs": []}
    segment = {"segment_id": "segment", "state": "sealed", "source_kind": "simulated",
               "started_at": "2026-11-11T00:00:00Z", "ended_at": "2026-11-11T00:01:00Z"}
    summaries = {"segment": {"duration_seconds": 60, "key_data_summary": {}, "error": ""}}
    conclusion = {"segment_ids": ["segment"], "status": "pass"}

    class Store:
        packages = Path("/tmp/simulated")

    factory_result_summary._motion_row(Store(), row, [segment], summaries, conclusion, stunt=True)
    assert row["verdict"] == "通过"  # This comes from the recorded human conclusion.
