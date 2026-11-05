"""Archive presentation uses synthetic server responses, never network/devices."""

import json
import os
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHeaderView

from rp1_monitor.desktop_platform_details import FactoryArchiveView


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def synthetic_archive():
    rules = json.loads((Path(__file__).parents[1] / "src/rp1_monitor/data/factory-report-rules.json").read_text(encoding="utf-8"))
    columns = [column["title"] for column in rules["columns"]]
    fields = [column["field"] for column in rules["columns"]]
    specifications = [
        ("SYS-001", "项目", "BASIC_CHECKS", "未测试", "原父项名称"),
        ("SYS-001", "子项", "RP001", "不通过", "<b>人工改名</b>"),
        ("SYS-010", "子项", "walking", "通过", "行走原文"),
        ("SYS-011", "项目", "", "不适用", "原不适用项目"),
        ("SYS-009", "项目", "", "待复核", "原第一次父项"),
        ("SYS-009", "项目", "", "中断", "原第二次父项"),
    ]
    rows = []
    for index, (code, level, check, verdict, name) in enumerate(specifications):
        original = dict.fromkeys(fields, "")
        original.update(record_id=f"csv-record-{index}", station_id="PC-MOCK", sample_id="000023",
                        model="24轴", suite="SYS", level=level, case_code=code, source_id=check,
                        test_name=name, verdict=verdict, operator="原测试员", actual_result="000001.2300",
                        criterion="原通过标准", notes="simulated：合成资料，不能代表实机结果。")
        rows.append({"record_id": f"metadata-id-{index}", "version": index + 3,
                     "case_code": "WRONG-NAMED-METADATA", "test_name": "不得覆盖原文",
                     "values": [original[field] for field in fields]})
    return {"id": "mock-archive", "source_id": "PC-MOCK", "robot_id": "mock-robot",
            "status": "PENDING", "revision": 8, "source_kind": "simulated", "filename": "mock.csv",
            "sha256": "a" * 64, "columns": columns, "rows": rows}


@pytest.fixture
def view(app):
    widget = FactoryArchiveView(synthetic_archive())
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def project(view, code):
    return next(view.project_tree.topLevelItem(index) for index in range(view.project_tree.topLevelItemCount())
                if view.project_tree.topLevelItem(index).text(0) == code)


def test_default_is_sys12_structure_and_does_not_build_raw_table_or_mutate_source(app):
    archive = synthetic_archive()
    before = deepcopy(archive)
    widget = FactoryArchiveView(archive)
    assert widget.tabs.currentIndex() == 0
    assert widget.raw_table is None
    assert widget.project_tree.topLevelItemCount() == 12
    assert project(widget, "SYS-001").text(2) == "未测试"
    assert project(widget, "SYS-010").text(2) == "仅子项/父项缺失"
    assert project(widget, "SYS-011").text(2) == "不适用"
    assert project(widget, "SYS-002").text(2) == "本文件未包含"
    assert project(widget, "SYS-009").text(2) == "待复核 / 中断"
    assert project(widget, "SYS-001").childCount() == 2
    assert archive == before
    widget.close()


def test_selected_child_uses_original_cells_not_named_metadata(view):
    view.project_tree.setCurrentItem(project(view, "SYS-001").child(1))
    assert "<b>人工改名</b>" in view.row_caption.text()
    assert view.row_caption.textFormat() == Qt.TextFormat.PlainText
    assert view.project_title.textFormat() == Qt.TextFormat.PlainText
    assert view.result_text.toPlainText() == "000001.2300"
    assert view.criterion_text.toPlainText() == "原通过标准"
    assert view.notes_text.toPlainText() == "simulated：合成资料，不能代表实机结果。"
    record = view.record_text.toPlainText()
    assert "CSV记录编号：csv-record-1" in record
    assert "该行内容版本：4" in record
    assert "CSV样品SN：000023" in record
    assert "CSV原检查项ID：RP001" in record
    assert "metadata-id" not in record
    assert "不得覆盖原文" not in record
    assert "17列汇总未包含独立证据列" in record


def test_missing_project_clears_every_previous_row_detail(view):
    view.project_tree.setCurrentItem(project(view, "SYS-001").child(1))
    assert view.record_text.toPlainText()
    view.project_tree.setCurrentItem(project(view, "SYS-002"))
    assert view.row_table.rowCount() == 0
    assert "本文件未包含" in view.row_caption.text()
    for widget in (view.result_text, view.criterion_text, view.notes_text, view.record_text):
        assert not widget.toPlainText()


def test_mapping_uses_actual_columns_even_when_reordered(app):
    archive = synthetic_archive()
    archive["columns"] = list(reversed(archive["columns"]))
    for row in archive["rows"]:
        row["values"] = list(reversed(row["values"]))
    widget = FactoryArchiveView(archive)
    widget.project_tree.setCurrentItem(project(widget, "SYS-001").child(1))
    assert widget.result_text.toPlainText() == "000001.2300"
    assert "CSV原检查项ID：RP001" in widget.record_text.toPlainText()
    widget.close()


def test_raw_tab_is_lazy_and_selected_long_cell_preserves_full_value(app):
    archive = synthetic_archive()
    column = archive["columns"].index("实际结果")
    value = "<img src=x>" + "长" * 65000
    archive["rows"][0]["values"][column] = value
    widget = FactoryArchiveView(archive)
    assert widget.raw_table is None
    assert widget.result_text.toPlainText() == value
    widget.tabs.setCurrentIndex(1)
    assert widget.raw_table.rowCount() == len(archive["rows"])
    assert len(widget.raw_table.item(0, column).text()) <= 181
    widget.raw_table.setCurrentCell(0, column)
    assert widget.raw_text.toPlainText() == value
    assert widget.raw_table.horizontalHeader().sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
    assert widget.project_tree.header().sectionResizeMode(1) == QHeaderView.ResizeMode.Interactive
    prior = widget.raw_table
    widget.tabs.setCurrentIndex(0)
    widget.tabs.setCurrentIndex(1)
    assert widget.raw_table is prior
    widget.close()


@pytest.mark.parametrize("fault", ["duplicate-column", "missing-value", "non-string"])
def test_malformed_contract_never_guesses_structured_fields(app, fault):
    archive = synthetic_archive()
    if fault == "duplicate-column":
        archive["columns"][0] = archive["columns"][1]
    elif fault == "missing-value":
        archive["rows"][0]["values"].pop()
    else:
        archive["rows"][0]["values"][0] = 23
    widget = FactoryArchiveView(archive)
    assert widget.schema_error
    assert widget.project_tree.topLevelItemCount() == 0
    assert not widget.record_text.toPlainText()
    widget.tabs.setCurrentIndex(1)
    assert widget.raw_table is not None
    widget.close()


def test_empty_archive_has_no_synthetic_pass_or_source_identity(app):
    archive = synthetic_archive()
    archive["rows"] = []
    widget = FactoryArchiveView(archive)
    assert widget.project_tree.topLevelItemCount() == 12
    for index in range(12):
        assert widget.project_tree.topLevelItem(index).text(2) == "本文件未包含"
    assert not widget.record_text.toPlainText()
    widget.close()


def test_unlisted_case_is_preserved_in_explicit_unclassified_group(app):
    archive = synthetic_archive()
    column = archive["columns"].index("测试项目编号")
    archive["rows"][0]["values"][column] = "SYS-999"
    widget = FactoryArchiveView(archive)
    extra = project(widget, "目录外")
    assert extra.childCount() == 1
    widget.project_tree.setCurrentItem(extra.child(0))
    assert "CSV项目编号：SYS-999" in widget.record_text.toPlainText()
    widget.close()


def test_malformed_raw_fallback_has_bounded_column_and_row_allocation(app):
    archive = synthetic_archive()
    archive["columns"] = [f"bad-{index}" for index in range(100)]
    archive["rows"] = [{"values": ["x"] * 100}] * 1001
    widget = FactoryArchiveView(archive)
    assert widget.schema_error
    widget.tabs.setCurrentIndex(1)
    assert widget.raw_table.columnCount() == 17
    assert widget.raw_table.rowCount() == 1000
    widget.close()
