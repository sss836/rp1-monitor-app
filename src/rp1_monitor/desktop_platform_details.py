"""Read-only structured presentation of an already uploaded factory archive."""

import json
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


@lru_cache(maxsize=1)
def _rules():
    return json.loads((Path(__file__).parent / "data/factory-report-rules.json").read_text(encoding="utf-8"))


def _label(value=""):
    widget = QLabel()
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setText(value)
    return widget


def _plain(name):
    widget = QPlainTextEdit()
    widget.setReadOnly(True)
    widget.setAccessibleName(name)
    return widget


def _table(columns, name):
    widget = QTableWidget(0, len(columns))
    widget.setAccessibleName(name)
    widget.setHorizontalHeaderLabels(columns)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setWordWrap(False)
    widget.verticalHeader().setVisible(False)
    widget.verticalHeader().setDefaultSectionSize(28)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def _short(value, limit=120):
    return value if len(value) <= limit else value[:limit] + "…"


class FactoryArchiveView(QWidget):
    """Archive content only; authentication and review remain in the caller."""

    def __init__(self, archive, parent=None):
        super().__init__(parent)
        self.archive = archive
        self.columns = archive.get("columns") or []
        self.original_rows = archive.get("rows") or []
        self.rows = []
        self.project_rows = []
        self.current_rows = []
        self.raw_table = None
        self.raw_text = None
        self._raw_built = False
        rules = _rules()
        expected = {column["title"]: column["field"] for column in rules["columns"]}
        self.schema_error = ""
        if (not isinstance(self.columns, list) or len(self.columns) != len(expected)
                or not all(isinstance(column, str) for column in self.columns)
                or set(self.columns) != set(expected)):
            self.schema_error = "平台列定义与原17列契约不一致，不能推断项目字段；请查看原始列并核对归档。"
        elif not isinstance(self.original_rows, list) or len(self.original_rows) > 1000:
            self.schema_error = "平台行数据不符合结果归档契约，请重新读取归档。"
        else:
            fields = [expected[column] for column in self.columns]
            for row in self.original_rows:
                values = row.get("values") if isinstance(row, dict) else None
                if (not isinstance(values, list) or len(values) != len(fields)
                        or not all(isinstance(value, str) for value in values)):
                    self.schema_error = "平台原始行缺失或列数不一致，不能推断项目字段；请查看原始列并核对归档。"
                    self.rows = []
                    break
                self.rows.append({"fields": dict(zip(fields, values)), "original": row})

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("平台工厂汇总查看方式")
        layout.addWidget(self.tabs, 1)
        self.structured = QWidget()
        structured_layout = QVBoxLayout(self.structured)
        structured_layout.setContentsMargins(0, 0, 0, 0)
        notice = self.schema_error or "父项结论与子项原文分别保留；未测试、不适用和缺项不计为通过。17列汇总没有独立证据列或原始采样曲线。"
        structured_layout.addWidget(_label(notice))
        splitter = QSplitter()
        self.project_tree = QTreeWidget()
        self.project_tree.setAccessibleName("已上传SYS12项目与父子记录")
        self.project_tree.setHeaderLabels(["项目编号", "目录项目 / 原记录", "父项结论"])
        self.project_tree.setRootIsDecorated(True)
        self.project_tree.setUniformRowHeights(True)
        self.project_tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for index, width in enumerate((92, 200, 118)):
            self.project_tree.setColumnWidth(index, width)
        self.project_tree.setMinimumWidth(310)
        self.project_tree.setMaximumWidth(450)
        self.project_tree.currentItemChanged.connect(self._tree_selected)
        splitter.addWidget(self.project_tree)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(8, 0, 0, 0)
        self.project_title = _label("选择测试项目")
        self.project_title.setObjectName("cardTitle")
        self.row_caption = _label("")
        detail_layout.addWidget(self.project_title)
        self.row_table = _table(["层级", "原检查项ID", "CSV测试内容", "原结论"], "当前项目原始父子记录")
        for index, width in enumerate((58, 112, 230, 90)):
            self.row_table.setColumnWidth(index, width)
        self.row_table.setMinimumHeight(78)
        self.row_table.setMaximumHeight(132)
        self.row_table.currentCellChanged.connect(lambda row, *_: self._row_selected(row))
        detail_layout.addWidget(self.row_table)
        detail_layout.addWidget(self.row_caption)
        self.detail_tabs = QTabWidget()
        self.result_text = _plain("平台原记录实际结果全文")
        self.criterion_text = _plain("平台原记录通过标准全文")
        self.notes_text = _plain("平台原记录备注全文")
        self.record_text = _plain("平台原记录身份与内容版本")
        for title, widget in (("实测结果", self.result_text), ("通过标准", self.criterion_text),
                              ("备注", self.notes_text), ("记录信息", self.record_text)):
            self.detail_tabs.addTab(widget, title)
        detail_layout.addWidget(self.detail_tabs, 1)
        splitter.addWidget(detail)
        splitter.setSizes([370, 620])
        structured_layout.addWidget(splitter, 1)
        self.tabs.addTab(self.structured, "按测试项目")
        self.raw_page = QWidget()
        self.raw_layout = QVBoxLayout(self.raw_page)
        self.raw_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs.addTab(self.raw_page, "原始列")
        self.tabs.currentChanged.connect(self._tab_selected)
        if not self.schema_error:
            self._populate_projects(rules)

    def _populate_projects(self, rules):
        definitions = [row for row in rules["rows"] if row["level"] == "项目"]
        first = None
        for index, definition in enumerate(definitions):
            selected = [row for row in self.rows if row["fields"]["case_code"] == definition["case_code"]]
            self.project_rows.append(selected)
            parents = [row["fields"]["verdict"] for row in selected if row["fields"]["level"] == "项目"]
            if not selected:
                status = "本文件未包含"
            elif not parents:
                status = "仅子项/父项缺失"
            else:
                status = " / ".join(dict.fromkeys(parents))
            project = QTreeWidgetItem([definition["case_code"], definition["test_name"], status])
            project.setData(0, Qt.ItemDataRole.UserRole, (index, None))
            self.project_tree.addTopLevelItem(project)
            for row_index, row in enumerate(selected):
                fields = row["fields"]
                child = QTreeWidgetItem([fields["level"], _short(fields["test_name"], 80), fields["verdict"]])
                child.setData(0, Qt.ItemDataRole.UserRole, (index, row_index))
                project.addChild(child)
            if selected and first is None:
                first = project
        known = {definition["case_code"] for definition in definitions}
        extra = [row for row in self.rows if row["fields"]["case_code"] not in known]
        if extra:
            index = len(self.project_rows)
            self.project_rows.append(extra)
            project = QTreeWidgetItem(["目录外", "保留原始记录，不自动归入SYS项目", "未作判定"])
            project.setData(0, Qt.ItemDataRole.UserRole, (index, None))
            self.project_tree.addTopLevelItem(project)
            for row_index, row in enumerate(extra):
                fields = row["fields"]
                child = QTreeWidgetItem([fields["case_code"], _short(fields["test_name"], 80), fields["verdict"]])
                child.setData(0, Qt.ItemDataRole.UserRole, (index, row_index))
                project.addChild(child)
            first = first or project
        if self.project_tree.topLevelItemCount():
            self.project_tree.setCurrentItem(first or self.project_tree.topLevelItem(0))

    def _tree_selected(self, item, _previous=None):
        if item is None:
            return
        project_index, row_index = item.data(0, Qt.ItemDataRole.UserRole)
        self.current_rows = self.project_rows[project_index]
        project = item.parent() or item
        self.project_title.setText(f"{project.text(0)} · {project.text(1)} · {project.text(2)}")
        self.row_table.setRowCount(0)
        self.row_table.setRowCount(len(self.current_rows))
        for index, row in enumerate(self.current_rows):
            fields = row["fields"]
            for column, value in enumerate((fields["level"], fields["source_id"], fields["test_name"], fields["verdict"])):
                self.row_table.setItem(index, column, QTableWidgetItem(_short(value)))
        if self.current_rows:
            selected = row_index if row_index is not None else 0
            self.row_table.setCurrentCell(selected, 0)
            self._row_selected(selected)
        else:
            self.row_caption.setText("本文件未包含该项目记录；不合成未测试或通过结论。")
            for widget in (self.result_text, self.criterion_text, self.notes_text, self.record_text):
                widget.clear()

    def _row_selected(self, index):
        if not 0 <= index < len(self.current_rows):
            return
        row = self.current_rows[index]
        fields, original = row["fields"], row["original"]
        self.row_caption.setText(f"{fields['level']} · CSV原文：{_short(fields['test_name'])} · {fields['verdict']}")
        self.result_text.setPlainText(fields["actual_result"])
        self.criterion_text.setPlainText(fields["criterion"])
        self.notes_text.setPlainText(fields["notes"])
        self.record_text.setPlainText("\n".join((
            f"CSV记录编号：{fields['record_id']}", f"该行内容版本：{original.get('version', '未提供')}",
            f"CSV上位机编号：{fields['station_id']}", f"CSV样品SN：{fields['sample_id']}",
            f"CSV机型：{fields['model']}", f"CSV测试人员：{fields['operator']}",
            f"CSV项目编号：{fields['case_code']}", f"CSV层级：{fields['level']}",
            f"CSV测试内容：{fields['test_name']}",
            f"CSV原检查项ID：{fields['source_id']}", f"CSV开始时间：{fields['started_at']}",
            f"CSV结束时间：{fields['ended_at']}",
            "", "以下为平台归档映射，未由SN或文件内容推断：",
            f"平台来源编号：{self.archive.get('source_id', '未提供')}",
            f"平台采集机器人ID：{self.archive.get('robot_id', '未提供')}",
            "17列汇总未包含独立证据列；此视图不生成或上传证据。",
        )))

    def _tab_selected(self, index):
        if index != 1 or self._raw_built:
            return
        self._raw_built = True
        hint = "原列顺序与字符串保持不变。表内长文本只显示预览，点选单元格查看下方完整原文。"
        if ((isinstance(self.columns, list) and len(self.columns) > 17)
                or (isinstance(self.original_rows, list) and len(self.original_rows) > 1000)):
            hint += "响应超出归档契约，仅展示前17列、1000行，请重新读取归档核对。"
        self.raw_layout.addWidget(_label(hint))
        columns = self.columns[:17] if isinstance(self.columns, list) and all(isinstance(value, str) for value in self.columns) else []
        self.raw_table = _table(columns, "平台保存的原始17列")
        self.raw_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.raw_table.horizontalHeader().setStretchLastSection(False)
        source = self.original_rows if isinstance(self.original_rows, list) else []
        source = source[:1000]
        self.raw_table.setRowCount(len(source))
        for row_index, row in enumerate(source):
            values = row.get("values", []) if isinstance(row, dict) else []
            for column, value in enumerate(values[:len(columns)] if isinstance(values, list) else []):
                if isinstance(value, str):
                    self.raw_table.setItem(row_index, column, QTableWidgetItem(_short(value, 180)))
        for column in range(len(columns)):
            self.raw_table.setColumnWidth(column, 145)
        self.raw_layout.addWidget(self.raw_table, 2)
        self.raw_text = _plain("原始单元格完整原文")
        self.raw_layout.addWidget(self.raw_text, 1)
        self.raw_table.currentCellChanged.connect(self._raw_selected)
        if source and columns:
            self.raw_table.setCurrentCell(0, 0)

    def _raw_selected(self, row, column, *_previous):
        if self.raw_text is None:
            return
        source = self.original_rows
        values = source[row].get("values", []) if isinstance(source, list) and 0 <= row < len(source) and isinstance(source[row], dict) else []
        value = values[column] if isinstance(values, list) and 0 <= column < len(values) else ""
        self.raw_text.setPlainText(value if isinstance(value, str) else "")
