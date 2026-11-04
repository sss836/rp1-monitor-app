"""Editable current factory summary, using only the local factory store."""

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

VERDICTS = ("未测试", "通过", "不通过", "待复核", "中断", "不适用")
EDITABLE_FIELDS = (
    "test_name", "started_at", "ended_at", "operator", "actual_result", "criterion",
    "criterion_source", "verdict", "evidence", "notes",
)
_JOBS = set()


class _SummaryJob(QThread):
    completed = Signal(object)
    failed = Signal(object)

    def __init__(self, operation):
        # Unparented and retained until finished: closing a page never destroys a running job.
        super().__init__()
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation())
        except Exception as exc:
            self.failed.emit(exc)


def _label(text=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class ResultSummaryPanel(QWidget):
    """The current edited report is separate from immutable source files/history.

    ``set_sn`` and ``refresh`` only read local data. Unsubmitted edits are retained
    separately for each SN; refresh overlays them without replacing their source token.
    The host should disable its SN selector while ``busy_changed`` is true and retry
    closing after ``shutdown()`` returns false, allowing an in-flight save to finish.
    """

    saved = Signal()
    exported = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.sn = ""
        self.busy = False
        self.closing = False
        self._job = None
        self._loading = False
        self._rows = []
        self._visible_rows = []
        self._drafts = {}
        self._selected_key = None
        self.export_directory = Path(store.directory) / "reports"
        self.last_export = ""
        self._station_saved = str(store.report_settings().get("station_id", ""))

        layout = QVBoxLayout(self)
        layout.addWidget(_label("整机结果汇总 · 逐项核对规则与实际结果，编辑当前最终版后即可导出。原始记录与已有测试历史保留在原页面。"))
        station = QHBoxLayout()
        station.addWidget(QLabel("上位机编号"))
        self.station_edit = QComboBox()
        self.station_edit.setEditable(True)
        self.station_edit.addItems(["PC-01", "PC-02", "PC-03"])
        self.station_edit.setCurrentText(self._station_saved)
        self.station_edit.setMinimumWidth(100)
        self.station_edit.setAccessibleName("上位机编号")
        self.station_edit.currentTextChanged.connect(self._update_actions)
        station.addWidget(self.station_edit)
        self.station_save_button = QPushButton("保存编号")
        self.station_save_button.clicked.connect(self.save_station)
        station.addWidget(self.station_save_button)
        station.addStretch()
        self.sn_label = _label("当前SN：未选择")
        station.addWidget(self.sn_label)
        layout.addLayout(station)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("统一填写测试人员"))
        self.operator_edit = QLineEdit()
        self.operator_edit.setAccessibleName("统一填写测试人员")
        toolbar.addWidget(self.operator_edit, 1)
        self.apply_operator_button = QPushButton("应用到已测 / 已编辑项")
        self.apply_operator_button.clicked.connect(self.apply_operator)
        self.apply_operator_button.setToolTip("仅应用到已有测试结论或人工编辑的行；不为未测试项目补填人员。")
        toolbar.addWidget(self.apply_operator_button)
        self.refresh_button = QPushButton("刷新本地结果")
        self.refresh_button.setToolTip("重新读取本地结果，保留各SN尚未保存的编辑。")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        self.show_children = QCheckBox("显示子项")
        self.show_children.setToolTip("展开17项基础自检及行走 / 跑步原始子项；隐藏不会丢弃修改。")
        self.show_children.toggled.connect(self._render_rows)
        toolbar.addWidget(self.show_children)
        layout.addLayout(toolbar)
        self.source_notice = _label()
        self.source_notice.setAccessibleName("结果汇总数据来源提示")
        layout.addWidget(self.source_notice)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["编号 / 来源ID", "测试项目", "是否通过", "人工编辑", "数据来源"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.setAccessibleName("整机结果汇总项目")
        self.table.currentCellChanged.connect(self._selection_changed)
        splitter.addWidget(self.table)

        self.editor = QScrollArea()
        self.editor.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.identity_label = _label("请先打开整机SN，再选择项目。")
        form.addRow(self.identity_label)
        self.fields = {}
        for key, title in (
            ("test_name", "测试名称"), ("criterion", "测试规则 / 判据"),
            ("actual_result", "实际测试结果"), ("verdict", "是否通过"),
            ("operator", "测试人员"), ("started_at", "开始时间"),
            ("ended_at", "结束时间"), ("criterion_source", "标准来源"),
            ("evidence", "关联证据"), ("notes", "备注"),
        ):
            if key == "verdict":
                widget = QComboBox()
                widget.addItems(VERDICTS)
                widget.currentTextChanged.connect(lambda value, field=key: self._edit_field(field, value))
            elif key in {"criterion", "actual_result", "evidence", "notes"}:
                widget = QPlainTextEdit()
                widget.setMinimumHeight(68)
                widget.setMaximumHeight(130)
                widget.textChanged.connect(lambda field=key: self._edit_field(field, self.fields[field].toPlainText()))
            else:
                widget = QLineEdit()
                widget.textChanged.connect(lambda value, field=key: self._edit_field(field, value))
            widget.setAccessibleName(title)
            if key in {"started_at", "ended_at"}:
                widget.setPlaceholderText("保留原始时间格式和时区；缺失可留空")
            self.fields[key] = widget
            form.addRow(title, widget)
        self.discard_button = QPushButton("放弃本项未保存修改")
        self.discard_button.clicked.connect(self.discard_selected)
        form.addRow(self.discard_button)
        self.editor.setWidget(body)
        splitter.addWidget(self.editor)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 660])
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.save_button = QPushButton("保存当前最终版")
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        self.export_button = QPushButton("一键保存并导出CSV")
        self.export_button.clicked.connect(self.export)
        actions.addWidget(self.export_button)
        self.choose_directory_button = QPushButton("选择导出目录")
        self.choose_directory_button.clicked.connect(self.choose_export_directory)
        actions.addWidget(self.choose_directory_button)
        self.open_directory_button = QPushButton("打开导出目录")
        self.open_directory_button.clicked.connect(self.open_export_directory)
        actions.addWidget(self.open_directory_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.directory_label = _label(f"导出目录：{self.export_directory}")
        layout.addWidget(self.directory_label)
        self.notice = _label("打开整机SN后显示本地测试规则与已有结果；可逐项修改后导出。")
        self.notice.setAccessibleName("汇总保存与导出状态")
        layout.addWidget(self.notice)
        self._update_actions()

    def set_sn(self, sn):
        """Return false if an in-flight job prevents changing the selected SN."""
        if self.busy or self.closing:
            return False
        self.sn = str(sn or "").strip()
        self._selected_key = None
        self.sn_label.setText(f"当前SN：{self.sn or '未选择'}")
        self.refresh()
        return True

    def refresh(self):
        if self.busy or self.closing:
            return
        try:
            self._rows = self.store.report_rows(self.sn) if self.sn else []
        except Exception as exc:
            # Never show another SN's rows after a failed switch/read.
            self._rows = []
            self._render_rows()
            self.notice.setText(f"读取本地汇总失败：{exc}。未保存编辑已保留。")
            return
        self._render_rows()
        pending = len(self._drafts.get(self.sn, {}))
        self.notice.setText(f"已载入 {len(self._rows)} 项结果；{pending} 项未保存编辑已保留。" if pending else f"已载入 {len(self._rows)} 项结果，可逐项编辑。")

    def _row(self):
        return next((row for row in self._rows if row["row_key"] == self._selected_key), None)

    def _effective(self, row):
        return {**row, **self._drafts.get(self.sn, {}).get(row["row_key"], {}).get("fields", {})}

    def _render_rows(self):
        self._loading = True
        self._visible_rows = [row for row in self._rows if self.show_children.isChecked() or row.get("level") != "子项"]
        self.table.setRowCount(len(self._visible_rows))
        selected = 0
        for index, source in enumerate(self._visible_rows):
            row = self._effective(source)
            pending = row["row_key"] in self._drafts.get(self.sn, {})
            edited = "未保存" if pending else ("已编辑" if row.get("edited") else "来源结果")
            code = row.get("source_id") if row.get("level") == "子项" else row.get("case_code")
            name = ("    " if row.get("level") == "子项" else "") + str(row.get("test_name", ""))
            for column, value in enumerate((code, name, row.get("verdict"), edited, row.get("source_kind"))):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                self.table.setItem(index, column, item)
            if row["row_key"] == self._selected_key:
                selected = index
        simulated = any(row.get("source_kind") == "simulated" for row in self._rows)
        self.source_notice.setText("模拟数据 simulated · 本页包含模拟来源，不能视为实机验收。" if simulated else "本页仅汇总本地结果；来源为 ROS 2 也不等于完成实机验收。")
        font = self.source_notice.font()
        font.setBold(simulated)
        self.source_notice.setFont(font)
        if self._visible_rows:
            self.table.selectRow(selected)
            self._selected_key = self._visible_rows[selected]["row_key"]
        else:
            self._selected_key = None
        self._loading = False
        self._load_editor()
        self._update_actions()

    def _selection_changed(self, current_row, _column, _previous_row, _previous_column):
        if not self._loading and 0 <= current_row < len(self._visible_rows):
            self._selected_key = self._visible_rows[current_row]["row_key"]
            self._load_editor()

    @staticmethod
    def _not_applicable(row):
        return str(row.get("model", "")).strip() in {"28", "RP1 · 28轴", "28轴"} and row.get("case_code") == "SYS-011"

    def _load_editor(self):
        source = self._row()
        self._loading = True
        row = self._effective(source) if source else {}
        if source:
            simulation = "（模拟数据，不代表实机验收）" if row.get("source_kind") == "simulated" else ""
            self.identity_label.setText(
                f"SN：{self.sn}　机型：{row.get('model', '')}　分类：{row.get('category', '')}\n"
                f"本地编号：{row.get('case_code', '')}　层级：{row.get('level', '')}\n"
                f"原始来源ID：{row.get('source_id') or '—'}　记录ID：{row.get('record_id') or '—'}\n"
                f"数据来源：{row.get('source_kind') or '未知'} {simulation}"
            )
        else:
            self.identity_label.setText("请先打开整机SN，再选择项目。")
        verdict = self.fields["verdict"]
        verdict.clear()
        verdict.addItems(["不适用"] if self._not_applicable(row) else VERDICTS)
        for key, widget in self.fields.items():
            value = str(row.get(key) or "")
            if isinstance(widget, QPlainTextEdit):
                widget.setPlainText(value)
            elif isinstance(widget, QComboBox):
                widget.setCurrentText("不适用" if self._not_applicable(row) else value)
            else:
                widget.setText(value)
        self._loading = False
        self._update_actions()

    def _edit_field(self, key, value):
        row = self._row()
        if self._loading or self.busy or self.closing or not row:
            return
        if key == "verdict" and self._not_applicable(row):
            value = "不适用"
        drafts = self._drafts.setdefault(self.sn, {})
        draft = drafts.setdefault(row["row_key"], {"row_key": row["row_key"], "source_token": row.get("source_token", ""), "fields": {}})
        if value == str(row.get(key) or ""):
            draft["fields"].pop(key, None)
        else:
            draft["fields"][key] = value
        if not draft["fields"]:
            drafts.pop(row["row_key"], None)
        index = next((index for index, visible in enumerate(self._visible_rows) if visible["row_key"] == row["row_key"]), -1)
        effective = self._effective(row)
        if index >= 0:
            self.table.item(index, 1).setText(("    " if row.get("level") == "子项" else "") + str(effective.get("test_name", "")))
            self.table.item(index, 2).setText(str(effective.get("verdict", "")))
            self.table.item(index, 3).setText("未保存" if row["row_key"] in drafts else ("已编辑" if row.get("edited") else "来源结果"))
        self.notice.setText(f"当前SN有 {len(drafts)} 项未保存编辑；保存仅更新当前最终版。")
        self._update_actions()

    def apply_operator(self):
        name = self.operator_edit.text().strip()
        if not name or self.busy or self.closing:
            return
        selected = self._selected_key
        count = 0
        for row in self._rows:
            effective = self._effective(row)
            if effective.get("verdict") in {"未测试", "不适用"} and not row.get("edited") and row["row_key"] not in self._drafts.get(self.sn, {}):
                continue
            self._selected_key = row["row_key"]
            self._edit_field("operator", name)
            count += 1
        self._selected_key = selected
        self._render_rows()
        self.notice.setText(f"已为 {count} 个已测 / 已编辑项填写人员；点击保存或导出后生效。")

    def discard_selected(self):
        if self.busy or self.closing:
            return
        self._drafts.get(self.sn, {}).pop(self._selected_key, None)
        self._render_rows()
        self.notice.setText("已放弃本项尚未保存的修改；原始记录与已保存最终版未改动。")

    def save_station(self):
        self._start_job(export=False, station_only=True)

    def save(self):
        self._start_job(export=False)

    def export(self):
        self._start_job(export=True)

    def _start_job(self, *, export, station_only=False):
        if self.busy or self.closing or (not self.sn and not station_only):
            return
        station = self.station_edit.currentText().strip()
        sn = self.sn
        drafts = [] if station_only else deepcopy(list(self._drafts.get(sn, {}).values()))
        directory = self.export_directory

        def operation():
            self.store.save_report_edits(sn, drafts, station_id=station)
            return self.store.export_result_summary(sn, directory) if export else None

        job = _SummaryJob(operation)
        self._job = job
        self.busy = True
        self.busy_changed.emit(True)
        self.notice.setText("正在保存并导出CSV，请稍候…" if export else "正在保存当前最终版…")
        self._update_actions()
        job.completed.connect(lambda path: self._job_completed(path, station, station_only))
        job.failed.connect(self._job_failed)
        job.finished.connect(self._job_finished)
        job.finished.connect(lambda: _JOBS.discard(job))
        job.finished.connect(job.deleteLater)
        _JOBS.add(job)
        job.start()

    def _job_completed(self, path, station, station_only):
        self._station_saved = station
        if not station_only:
            self._drafts.pop(self.sn, None)
        self.saved.emit()
        if path:
            self.last_export = str(path)
            self.exported.emit(self.last_export)
        self._job_message = f"已导出：{path}" if path else ("上位机编号已保存。" if station_only else "当前最终版已保存。")

    def _job_failed(self, exc):
        self._job_message = f"保存 / 导出失败：{exc}。未保存编辑已保留，可核对后重试。"

    def _job_finished(self):
        self._job = None
        self.busy = False
        if not self.closing:
            self.refresh()
        self.notice.setText(getattr(self, "_job_message", "任务已结束。"))
        self._update_actions()
        self.busy_changed.emit(False)

    def _update_actions(self, *_args):
        idle = not self.busy and not self.closing
        has_sn = bool(self.sn)
        self.station_edit.setEnabled(idle)
        self.station_save_button.setEnabled(idle and self.station_edit.currentText().strip() != self._station_saved)
        self.operator_edit.setEnabled(idle and has_sn)
        self.apply_operator_button.setEnabled(idle and has_sn)
        self.refresh_button.setEnabled(idle and has_sn)
        self.show_children.setEnabled(idle and has_sn)
        self.table.setEnabled(idle and has_sn)
        self.editor.setEnabled(idle and self._row() is not None)
        self.discard_button.setEnabled(idle and self._selected_key in self._drafts.get(self.sn, {}))
        self.save_button.setEnabled(idle and has_sn)
        self.export_button.setEnabled(idle and has_sn)
        self.choose_directory_button.setEnabled(idle)
        self.open_directory_button.setEnabled(idle and self.export_directory.is_dir())

    def choose_export_directory(self):
        if self.busy or self.closing:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择结果汇总导出目录", str(self.export_directory))
        if directory:
            self.export_directory = Path(directory)
            self.directory_label.setText(f"导出目录：{self.export_directory}")
            self._update_actions()

    def open_export_directory(self):
        if not self.busy and not self.closing and self.export_directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.export_directory)))

    def shutdown(self):
        """An in-flight local write finishes naturally; never terminate its thread."""
        self.closing = True
        self._update_actions()
        return not self.busy

    def closeEvent(self, event):
        if self.shutdown():
            super().closeEvent(event)
        else:
            event.ignore()
