"""Passive Qt detail contracts; synthetic archive fields, no HTTP/SQL/devices."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from rp1_monitor.desktop_research_archive_view import ResearchArchiveView


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def label(view, name):
    return view.findChild(QLabel, name).text()


def test_history_unknown_original_facts_are_not_filled_from_human_declarations(app):
    archive = {"origin": "HISTORY", "actual_object": "实际样品", "test_name": "实际测试",
               "facts": {"started_at": None, "ended_at": None, "active_seconds": None,
                         "completed_cycles": None, "exception_count": None, "machine_status": "unknown",
                         "automatic_verdict": "unconfigured", "data_quality": "MISSING", "clock_quality": "MISSING"},
               "current_verification": {"tester": "核实人员", "verdict": "failed",
                                        "declared_active_seconds": 0, "declared_completed_cycles": 0,
                                        "declared_exception_count": 0, "notes": "明确核实为0，不代表未知"}}
    view = ResearchArchiveView(archive)
    assert view.tabs.currentIndex() == 0
    assert label(view, "archive_actual_object") == "实际样品"
    assert label(view, "archive_test_name") == "实际测试"
    for field in ("active_seconds", "completed_cycles", "exception_count", "started_at", "ended_at"):
        assert "未知" in label(view, "fact_" + field)
    assert label(view, "declared_active_seconds") == "0 秒"
    assert label(view, "declared_completed_cycles") == "0 次"
    assert label(view, "declared_exception_count") == "0 次"
    assert label(view, "declared_verdict") == "不通过"
    assert label(view, "fact_machine_status") == "未知"
    assert label(view, "fact_automatic_verdict") == "未配置"
    assert not view.raw.toPlainText()
    view.deleteLater()


def test_large_explicit_count_is_not_rounded_or_rendered_as_unknown(app):
    view = ResearchArchiveView({"current_verification": {"declared_completed_cycles": 2147483647}})
    assert label(view, "declared_completed_cycles") == "2147483647 次"
    assert "未知" in label(view, "fact_completed_cycles")
    view.deleteLater()


def test_human_dates_convert_to_beijing_and_naive_original_dates_are_not_guessed(app):
    view = ResearchArchiveView({"facts": {"started_at": "2026-11-06T00:00:00"},
                                "current_verification": {"declared_started_at": "2026-11-06T00:00:00Z",
                                                         "declared_ended_at": "2026-11-06T09:30:00+08:00"}})
    assert label(view, "declared_started_at") == "2026-11-06 08:00:00 北京时间"
    assert label(view, "declared_ended_at") == "2026-11-06 09:30:00 北京时间"
    assert "未注明时区" in label(view, "fact_started_at")
    assert "未知" in label(view, "declared_verdict")
    view.deleteLater()


def test_notes_and_object_html_are_plain_original_text_and_identity_uses_flat_context(app):
    notes = '<b>人工备注</b> & <img src="https://invalid.example/no-network">\n' + "长备注" * 1000
    archive = {"actual_object": "<h1>实际对象</h1>", "sha256": "a" * 64,
               "classification": {"asset_id": "actual-asset-uuid", "station_id": "actual-station-uuid", "revision": 2},
               "current_verification": {"notes": notes, "declared_completed_cycles": 0}}
    view = ResearchArchiveView(archive)
    assert view.notes.isReadOnly() and view.notes.toPlainText() == notes
    object_label = view.findChild(QLabel, "archive_actual_object")
    assert object_label.text() == archive["actual_object"]
    assert object_label.textFormat() == Qt.TextFormat.PlainText
    assert not object_label.openExternalLinks()
    assert label(view, "identity_asset_id") == "actual-asset-uuid"
    assert label(view, "identity_sha256") == archive["sha256"]
    view.tabs.setCurrentIndex(1)
    assert '"declared_completed_cycles": 0' in view.raw.toPlainText()
    assert "actual-station-uuid" in view.raw.toPlainText()
    assert "<b>人工备注</b>" in view.raw.toPlainText()
    view.deleteLater()


def test_actual_manifest_quality_paths_and_zero_gap_remain_observation_only(app):
    archive = {"processing_state": "READY", "facts": {"active_seconds": 12.5}, "manifest": {
        "quality_status": "PARTIAL", "analysis": {"quality": {"row_count": 3, "valid_time_rows": 3,
        "invalid_time_rows": 0, "non_monotonic": 0, "max_gap_seconds": 0}},
        "monitor_validation": {"verified_sample_count": 3, "missing_sequence_count": 1,
                               "disconnected_rows": 1, "payload_hashes_verified": True}}}
    view = ResearchArchiveView(archive)
    assert label(view, "parse_state") == "解析就绪"
    assert label(view, "parse_quality") == "部分缺失"
    assert label(view, "parse_invalid_time_rows") == "0 行"
    assert label(view, "parse_max_gap_seconds") == "0 秒"
    assert label(view, "parse_missing_sequence_count") == "1 个"
    assert label(view, "parse_payload_hashes_verified") == "逐点匹配"
    assert label(view, "fact_active_seconds") == "12.5 秒"
    assert "未知" in label(view, "declared_active_seconds")
    assert any("不代表硬件合格" in item.text() for item in view.findChildren(QLabel))
    view.deleteLater()
