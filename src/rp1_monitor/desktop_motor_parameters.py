"""One native dialog for reading and explicitly updating MIT encoding ranges."""

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
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import motor_parameters


class ParameterWorker(QThread):
    completed = Signal(object)

    def __init__(self, context, password, request, parent=None):
        super().__init__(parent)
        self.context, self.password, self.request = dict(context), password, dict(request)

    def run(self):
        try:
            result = motor_parameters.perform(self.context, self.password, self.request)
        except Exception as error:
            result = dict(state="unknown" if self.request["operation"] == "update" else "error",
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


class MotorParameterDialog(QDialog):
    def __init__(self, context, sn, dof, save_report, parent=None):
        super().__init__(parent)
        self.context, self.sn, self.dof, self.save_report = dict(context), sn, dof, save_report
        self.worker = None
        self.snapshot = None
        self.setObjectName("motorParameterDialog")
        self.setWindowTitle("电机参数检查与更新")
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
        body.addWidget(text(f"SN：{sn} · {context['robot_id']} · {dof}轴 · SSH：{context['target']}"))
        body.addWidget(text("先检查全部电机，再选择差异项更新。仅核对MIT编码范围；范围是协议量程，不是运行Kp/Kd或允许测试的速度、力矩。固件版本只记录，未定义允许版本，不自动判定固件合规。"))
        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("本次sudo密码；复用已连接SSH，不写入磁盘")
        form.addRow("sudo密码", self.password)
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
        body.addWidget(self.table, 1)
        body.addWidget(text("单元格显示“实际 / 期望”。只更新所选电机有差异的KP、KD、POS、SPD、TOR量程；保留ID、零位、CAN超时和其他Pn参数，不调用使能、标零或动作。原值与逐项读回结果保存到当前SN。"))
        self.confirm = QCheckBox("已停止策略、失能电机并退出其他电机工具，现场允许参数维护")
        self.confirm.stateChanged.connect(lambda _: self.update_enabled())
        body.addWidget(self.confirm)
        buttons = QHBoxLayout()
        self.update = QPushButton("确认更新所选差异参数…")
        self.update.clicked.connect(self.start_update)
        self.update.setEnabled(False)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.update, 1)
        buttons.addWidget(self.close_button)
        body.addLayout(buttons)

    def selected(self):
        return [self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                for row in range(self.table.rowCount())
                if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def update_enabled(self):
        self.update.setEnabled(not self.worker and bool(self.snapshot) and self.confirm.isChecked()
                               and bool(self.selected()))

    def start_check(self):
        if self.worker:
            return
        self.snapshot = None
        self.begin("check", "", [])

    def start_update(self):
        if self.worker or not self.snapshot or not self.confirm.isChecked() or not self.selected():
            return
        selected = self.selected()
        details = []
        for row in self.snapshot["rows"]:
            if row["address"] in selected:
                details.extend(f"{row['address']} {key}：{range_text(value['actual'])} → {range_text(value['expected'])}"
                               for key, value in row["parameters"].items() if not value["matches"])
        box = QMessageBox(QMessageBox.Icon.Question, "更新已核对的差异参数",
                          f"SN：{self.sn}\n将更新{len(selected)}台电机的{len(details)}项MIT编码范围，原值先保存，逐项读回。\n\n" + "\n".join(details[:8]),
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.setDetailedText("\n".join(details))
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.begin("update", self.snapshot["snapshot_digest"], selected)

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
        self.table.setEnabled(False)
        self.close_button.setEnabled(False)
        self.status.setText("正在逐台读取参数…" if operation == "check" else "正在重新核对原值并更新所选差异；请勿关闭或拔线…")
        self.worker = ParameterWorker(self.context, password, request, self)
        self.worker.completed.connect(self.complete)
        self.worker.finished.connect(self.finished_worker)
        self.worker.start()

    def complete(self, result):
        try:
            self.save_report(self.sn, self.request, result)
        except Exception as error:
            self.snapshot = None
            self.status.setText(f"操作回执在本次窗口，归档失败：{error}。请核对实际值，勿重复更新。")
            return
        self.snapshot = result if result["state"] == "checked" else None
        if self.snapshot:
            self.render(result)
        else:
            self.status.setText({"updated": "所选量程已更新并读回；请再次检查全部电机。未验证掉电保持。",
                                 "partial": "部分参数可能已写入，后续已停止。请先重新检查；原值和过程已保存。",
                                 "unknown": "更新回执未确认，部分参数可能已写入。请先重新检查，勿重复写入。"}.get(
                                     result["state"], "参数操作未完成。") + " " + result.get("detail", ""))

    def render(self, result):
        self.table.blockSignals(True)
        self.table.setRowCount(len(result["rows"]))
        counts = dict.fromkeys(("match", "different", "unreadable"), 0)
        for index, row in enumerate(result["rows"]):
            counts[row["status"]] += 1
            first = QTableWidgetItem(f"{row['address']} · 型号{row['model']}")
            first.setData(Qt.ItemDataRole.UserRole, row["address"])
            if row["status"] == "different":
                first.setFlags(first.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                first.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(index, 0, first)
            cells = [row.get("firmware", "未知")]
            for key in ("KP", "KD", "POS", "SPD", "TOR"):
                value = row["parameters"].get(key)
                cells.append(f"{range_text(value['actual'])}\n期望 {range_text(value['expected'])}" if value else "未读取")
            cells += [str(row.get("can_timeout_ms", "未知")), {"match": "范围一致", "different": "有差异", "unreadable": "未完成"}[row["status"]]]
            for column, value in enumerate(cells, 1):
                item = QTableWidgetItem(value)
                item.setToolTip("\n".join(row["errors"]))
                self.table.setItem(index, column, item)
        self.table.resizeRowsToContents()
        self.table.blockSignals(False)
        self.status.setText(f"范围一致{counts['match']}台 · 有差异{counts['different']}台 · 未完成{counts['unreadable']}台。检查已归档到SN：{self.sn}；固件版本及其他Pn参数未判定合规。")

    def finished_worker(self):
        self.worker.deleteLater()
        self.worker = None
        for widget in (self.check, self.password, self.confirm, self.table, self.close_button):
            widget.setEnabled(True)
        self.update_enabled()

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
