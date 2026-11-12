"""Native controls for reading and explicitly updating motor parameters."""

from uuid import uuid4

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import motor_parameters
from .motor_parameters_remote import (
    EXTENDED_STANDARD, PARAMETER_STANDARD, PARAMETER_WRITE_BLOCKED,
    parameter_differences, parameter_write_items,
)


class ParameterWorker(QThread):
    completed = Signal(object)

    def __init__(self, context, password, request, parent=None, *, performer=None):
        super().__init__(parent)
        self.context, self.password, self.request = dict(context), password, dict(request)
        self.performer = performer or motor_parameters.perform

    def run(self):
        try:
            result = self.performer(self.context, self.password, self.request)
        except Exception as error:
            result = dict(state="unknown" if self.request["operation"] != "check" else "error",
                          detail=str(error).replace(self.password, "[已隐藏]"))
        finally:
            self.password = ""
        self.completed.emit(result)


def text(value):
    label = QLabel(value)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def range_text(value):
    return " / ".join(f"{number:g}" for number in value)


def value_text(value):
    if value is None:
        return "未知"
    return range_text(value) if isinstance(value, list) else str(value)


class MotorParameterDialog(QDialog):
    report_requested = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, context, sn, dof, save_report, parent=None, *, embedded=False, password_input=None):
        super().__init__(parent, Qt.WindowType.Widget if embedded else Qt.WindowType.Dialog)
        self.context, self.sn, self.dof, self.save_report = dict(context), sn, dof, save_report
        self.embedded = embedded
        self.worker = None
        self.snapshot = None
        self.report_available = False
        self.pending_update = None
        self.setObjectName("motorParameterDialog")
        self.setWindowTitle("电机参数检查与更新")
        if not embedded:
            self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        palette = self.palette()
        for role, color in ((QPalette.ColorRole.Window, "#eef2fa"), (QPalette.ColorRole.Base, "#ffffff"),
                            (QPalette.ColorRole.WindowText, "#142441"), (QPalette.ColorRole.Text, "#142441")):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.setStyleSheet("""
            QDialog#motorParameterDialog { background:#eef2fa; color:#142441; }
            QDialog#motorParameterDialog QLabel { background:transparent; color:#142441; }
            QDialog#motorParameterDialog QTableWidget { background:#fff; color:#142441;
                gridline-color:#d4dfef; font-size:13px; }
            QDialog#motorParameterDialog QLineEdit { background:#fff; color:#142441; }
        """)
        self.resize(1060, 650)
        body = QVBoxLayout(self)
        if not embedded:
            body.addWidget(text(f"SN：{sn} · {context['robot_id']} · {dof}轴 · SSH：{context['target']}"))
        self.standard = text("等待读取本次 MIT 编码量程基准。")
        body.addWidget(self.standard)
        self.password = password_input if password_input is not None else QLineEdit()
        if password_input is None:
            form = QFormLayout()
            self.password.setEchoMode(QLineEdit.EchoMode.Password)
            self.password.setPlaceholderText("本次 sudo 密码，仅在本次维护中使用")
            form.addRow("sudo 密码", self.password)
            body.addLayout(form)
        self.check = QPushButton(f"检查全部{dof}台电机参数")
        self.check.clicked.connect(self.start_check)
        body.addWidget(self.check)
        self.status = text("准备就绪。请先按原流程失能电机、停止deploy并退出标零等电机工具。")
        body.addWidget(self.status)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(["更新 / 电机 / 型号", "固件（读取值）", "KP", "KD", "POS / rad", "SPD / rad/s", "TOR / N·m", "CAN超时 / ms", "结果"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(lambda _: self.update_enabled())
        tabs = QTabWidget()
        tabs.addTab(self.table, "MIT 量程与版本")
        extended = QVBoxLayout()
        self.extended_status = text("完成检查后可查看 Kt、CUR 和各型号的 Pn 回读。")
        extended.addWidget(self.extended_status)
        self.extended_table = QTableWidget(0, 5)
        self.extended_table.setHorizontalHeaderLabels(["电机 / 型号", "参数", "实际回读", "期望值", "结果"])
        self.extended_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.extended_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        extended.addWidget(self.extended_table)
        extended_page = QWidget()
        extended_page.setLayout(extended)
        tabs.addTab(extended_page, "扩展参数")
        body.addWidget(tabs, 1)
        self.rewrite = QCheckBox("重写所选电机已读出的可写参数（包含已一致的参数）")
        self.rewrite.toggled.connect(self.change_write_mode)
        self.select_all = QPushButton("全选可更新电机")
        self.select_all.setEnabled(False)
        self.select_all.clicked.connect(self.toggle_select_all)
        selection = QHBoxLayout()
        selection.addWidget(self.rewrite, 1)
        selection.addWidget(self.select_all)
        body.addLayout(selection)
        self.confirm = QCheckBox("已停止策略、失能电机并退出其他电机工具，现场允许参数维护")
        self.confirm.stateChanged.connect(lambda _: self.update_enabled())
        body.addWidget(self.confirm)
        buttons = QHBoxLayout()
        self.update = QPushButton("确认更新所选差异参数…")
        self.update.clicked.connect(self.start_update)
        self.update.setEnabled(False)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        self.report_button = QPushButton("查看报告 / 导出…")
        self.report_button.setToolTip("检查结果自动加入当前SN；进入报告页可编辑结果并导出CSV。")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.open_report)
        buttons.addWidget(self.update, 1)
        buttons.addWidget(self.report_button)
        buttons.addWidget(self.close_button)
        body.addLayout(buttons)
        self.review = QWidget()
        review_layout = QVBoxLayout(self.review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        self.review_text = QPlainTextEdit(readOnly=True)
        self.review_text.setMaximumHeight(120)
        review_layout.addWidget(self.review_text)
        actions = QHBoxLayout()
        approve = QPushButton("写入并逐项回读")
        approve.clicked.connect(self.confirm_update)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.cancel_review)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(approve)
        review_layout.addLayout(actions)
        body.addWidget(self.review)
        self.review.hide()
        self.close_button.setVisible(not embedded)

    def open_report(self):
        if self.report_available and self.worker is None:
            self.report_requested.emit(self.sn)

    def selected(self):
        return [self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                for row in range(self.table.rowCount())
                if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def can_update(self):
        return (not self.worker and bool(self.snapshot) and self.confirm.isChecked()
                and self.snapshot.get("parameter_standard") == PARAMETER_STANDARD
                and self.snapshot.get("parameter_write_allowed") is True and bool(self.selected()))

    def update_enabled(self):
        self.cancel_review()
        self.update.setEnabled(self.can_update())
        eligible = [self.table.item(row, 0) for row in range(self.table.rowCount())
                    if self.table.item(row, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable]
        self.select_all.setEnabled(not self.worker and bool(eligible) and bool(self.snapshot))
        all_selected = bool(eligible) and all(item.checkState() == Qt.CheckState.Checked for item in eligible)
        self.select_all.setText("取消全选" if all_selected else "全选可更新电机")

    def toggle_select_all(self):
        eligible = [self.table.item(row, 0) for row in range(self.table.rowCount())
                    if self.table.item(row, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable]
        checked = Qt.CheckState.Unchecked if all(item.checkState() == Qt.CheckState.Checked for item in eligible) else Qt.CheckState.Checked
        self.table.blockSignals(True)
        for item in eligible:
            item.setCheckState(checked)
        self.table.blockSignals(False)
        self.update_enabled()

    def change_write_mode(self, rewrite):
        self.update.setText("核对所选电机重写清单…" if rewrite else "确认更新所选差异参数…")
        if self.snapshot:
            self.render(self.snapshot)
        self.update_enabled()

    def cancel_review(self):
        self.pending_update = None
        self.review.hide()

    def confirm_update(self):
        if self.pending_update and self.can_update():
            operation, digest, selected = self.pending_update
            self.cancel_review()
            self.begin(operation, digest, selected)

    def start_check(self):
        if self.worker:
            return
        self.snapshot = None
        self.cancel_review()
        self.standard.setText("等待读取本次 MIT 编码量程基准。")
        self.standard.setToolTip("")
        self.extended_table.setRowCount(0)
        self.extended_status.setText("完成检查后可查看 Kt、CUR 和各型号的 Pn 回读。")
        self.begin("check", "", [])

    def start_update(self):
        if not self.can_update():
            return
        selected = self.selected()
        rewrite = self.rewrite.isChecked()
        details, keys = [], set()
        for row in self.snapshot["rows"]:
            if row["address"] in selected:
                for key, value in parameter_write_items(row, rewrite=rewrite):
                    keys.add(key)
                    unit = f" {value['unit']}" if value["unit"] else ""
                    details.append(f"{row['address']} {key}：{value_text(value['actual'])} → {value_text(value['expected'])}{unit}")
        notices = []
        if "CAN_TIMEOUT" in keys:
            notices.append("CAN_TIMEOUT 写入 0 ms 会关闭电机通信超时保护。")
        if "PnE71" in keys:
            notices.append("PnE71 会把电机过压检测阈值设为 75 V。")
        description = "可写参数（含一致项）" if rewrite else "差异参数"
        summary = f"SN：{self.sn}\n将写入 {len(selected)} 台电机的 {len(details)} 项{description}，保存原值并逐项读回。"
        operation = "rewrite" if rewrite else "update"
        if self.embedded:
            self.pending_update = (operation, self.snapshot["snapshot_digest"], selected)
            self.review_text.setPlainText("\n".join([summary, *notices, "", *details]))
            self.review.show()
            return
        box = QMessageBox(QMessageBox.Icon.Question, "更新已核对的差异参数",
                          "\n".join([summary, *notices, "", *details[:8]]),
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.setDetailedText("\n".join(details))
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.begin(operation, self.snapshot["snapshot_digest"], selected)

    def begin(self, operation, expected_digest, selected):
        password = self.password.text()
        if not password:
            self.status.setText("请输入本次sudo密码。")
            return
        request = dict(request_id=str(uuid4()), robot_id=self.context["robot_id"], dof=self.dof,
                       operation=operation, expected_digest=expected_digest, selected=selected)
        try:
            # Required before writing: the complete before image already lives on PC.
            self.save_report(self.sn, request, dict(state="requested", before=self.snapshot))
        except Exception as error:
            self.status.setText(f"本机备份未保存，未发送参数操作：{error}")
            return
        self.request = request
        self.check.setEnabled(False)
        self.update.setEnabled(False)
        self.password.setEnabled(False)
        self.confirm.setEnabled(False)
        self.rewrite.setEnabled(False)
        self.select_all.setEnabled(False)
        self.table.setEnabled(False)
        self.close_button.setEnabled(False)
        self.report_button.setEnabled(False)
        self.status.setText("正在逐台读取量程、固件和扩展参数…" if operation == "check" else "正在重新核对原值并写入所选参数，请保持连接…")
        self.worker = ParameterWorker(self.context, password, request, self)
        self.worker.completed.connect(self.complete)
        self.worker.finished.connect(self.finished_worker)
        self.busy_changed.emit(True)
        self.worker.start()

    def complete(self, result):
        try:
            self.save_report(self.sn, self.request, result)
        except Exception as error:
            self.snapshot = None
            self.report_available = False
            self.status.setText(f"操作回执在本次窗口，归档失败：{error}。请核对实际值，勿重复更新。")
            return
        self.report_available = True
        self.snapshot = result if result["state"] == "checked" else None
        if self.snapshot:
            self.render(result)
        else:
            self.status.setText({"updated": "所选参数已更新并读回。重新上电后请再次检查全部电机。",
                                 "partial": "部分参数可能已写入，后续已停止。请先重新检查；原值和过程已保存。",
                                 "unknown": "更新回执未确认，部分参数可能已写入。请先重新检查，勿重复写入。"}.get(
                                     result["state"], "参数操作未完成。") + " " + result.get("detail", ""))

    def render(self, result):
        standard = result.get("parameter_standard")
        write_allowed = standard == PARAMETER_STANDARD and result.get("parameter_write_allowed") is True
        if standard == PARAMETER_STANDARD:
            label = "MIT 编码量程基准 · roboparty_motors@" + standard.split("@", 1)[1][:7]
            if not write_allowed:
                label += "\n" + PARAMETER_WRITE_BLOCKED
            self.standard.setText(label)
            self.standard.setToolTip(standard)
        else:
            self.standard.setText("本次回执缺少当前官网基准。请更新电脑后台后重新检查；当前仅可读取。")
            self.standard.setToolTip("")
        selected = set(self.selected()) if self.table.rowCount() and all(
            self.table.item(row, 0) is not None for row in range(self.table.rowCount())) else set()
        rewrite = self.rewrite.isChecked()
        self.table.blockSignals(True)
        self.table.setRowCount(len(result["rows"]))
        counts = dict.fromkeys(("match", "different", "unreadable"), 0)
        for index, row in enumerate(result["rows"]):
            counts[row["status"]] += 1
            first = QTableWidgetItem(f"{row['address']} · 型号{row['model']}")
            first.setData(Qt.ItemDataRole.UserRole, row["address"])
            if parameter_write_items(row, rewrite=rewrite) and write_allowed:
                first.setFlags(first.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                default_selected = row["address"] in selected if rewrite else bool(parameter_differences(row))
                first.setCheckState(Qt.CheckState.Checked if default_selected else Qt.CheckState.Unchecked)
            else:
                first.setFlags(first.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.table.setItem(index, 0, first)
            cells = [row.get("firmware", "未知")]
            for key in ("KP", "KD", "POS", "SPD", "TOR"):
                value = row["parameters"].get(key)
                cells.append(f"{range_text(value['actual'])}\n期望 {range_text(value['expected'])}" if value else "未读取")
            row_status = {"match": "范围一致", "different": "有差异", "unreadable": "未完成"}[row["status"]]
            if row["status"] == "match" and parameter_differences(row):
                row_status = "扩展参数有差异"
            cells += [str(row.get("can_timeout_ms", "未知")), row_status]
            for column, value in enumerate(cells, 1):
                item = QTableWidgetItem(value)
                item.setToolTip("\n".join(row["errors"]))
                self.table.setItem(index, column, item)
        self.table.resizeRowsToContents()
        self.table.blockSignals(False)
        self.render_extended(result["rows"], result.get("extended_parameter_standard", ""))
        self.status.setText(f"MIT 量程一致{counts['match']}台 · 有差异{counts['different']}台 · 未完成{counts['unreadable']}台。已加入SN：{self.sn}的SYS-003 / SYS-004报告。")
        if write_allowed and not any(parameter_differences(row) for row in result["rows"]):
            self.status.setText(self.status.text() + " 如需主动重写，请勾选重写模式后选择电机。")
        self.update_enabled()

    def render_extended(self, rows, standard=EXTENDED_STANDARD):
        readings = [(motor, name, reading) for motor in rows
                    for name, reading in motor.get("extended_parameters", {}).items()]
        self.extended_table.setRowCount(len(readings))
        read_count = different = 0
        for index, (motor, name, reading) in enumerate(readings):
            value = reading["actual"]
            read_count += value is not None
            different += reading.get("matches") is False
            actual = value_text(value)
            expected = reading.get("expected")
            wanted = value_text(expected)
            unit = reading["unit"]
            if unit:
                if value is not None:
                    actual += " " + unit
                if expected is not None:
                    wanted += " " + unit
            status = "未知" if value is None else {True: "一致", False: "有差异", None: "已读取"}[reading.get("matches")]
            cells = (f"{motor['address']} · 型号{motor['model']}", name, actual,
                     wanted, reading.get("error", status))
            for column, cell in enumerate(cells):
                item = QTableWidgetItem(cell)
                item.setToolTip(reading.get("raw_reply", ""))
                self.extended_table.setItem(index, column, item)
        if readings:
            self.extended_status.setText(f"{standard} · 已读取 {read_count}/{len(readings)} 项 · 差异 {different} 项。")
        else:
            self.extended_status.setText("本次回执未包含扩展参数，请重新检查。")

    def finished_worker(self):
        self.worker.deleteLater()
        self.worker = None
        for widget in (self.check, self.password, self.confirm, self.rewrite, self.table, self.close_button):
            widget.setEnabled(True)
        self.report_button.setEnabled(self.report_available)
        self.update_enabled()
        self.busy_changed.emit(False)

    def reject(self):
        if self.worker:
            return
        self.password.clear()
        super().reject()

    def closeEvent(self, event):
        if self.worker:
            event.ignore()
        else:
            self.password.clear()
            super().closeEvent(event)
