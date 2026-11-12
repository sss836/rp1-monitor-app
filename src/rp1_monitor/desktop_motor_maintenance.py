"""Inline native workstation tools for motor firmware and parameters."""

from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QHBoxLayout, QHeaderView, QLineEdit, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import motor_firmware
from .desktop_motor_parameters import MotorParameterDialog, ParameterWorker, text


class MotorMaintenancePanel(QWidget):
    report_requested = Signal(str, str)
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parameters = self.firmware = None
        self.binding = None
        self.section = "parameters"
        self.busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.identity = text("请在整机测试中选择 SN，并连接对应机器人。")
        layout.addWidget(self.identity)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("本次维护使用，离开页面后清除")
        form = QFormLayout()
        form.addRow("机器人 sudo 密码", self.password)
        layout.addLayout(form)
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self.section_changed)
        layout.addWidget(self.tabs, 1)
        self.tabs.hide()
        self.password.setEnabled(False)

    def configure(self, context, sn, dof, save_report, *, error="", save_firmware_report=None, session_key=None):
        if self.busy:
            return
        binding = (dict(context), sn, dof, session_key) if context and sn and not error else None
        if binding != self.binding:
            self.clear_credentials()
            self.binding = binding
            widgets = [self.tabs.widget(index) for index in range(self.tabs.count())]
            self.tabs.clear()
            for widget in widgets:
                widget.deleteLater()
            self.parameters = self.firmware = None
            if binding:
                self.parameters = MotorParameterDialog(context, sn, dof, save_report, self.tabs,
                                                       embedded=True, password_input=self.password)
                self.firmware = MotorFirmwarePanel(context, sn, dof, self.password, save_firmware_report, self.tabs)
                self.tabs.addTab(self.firmware, "固件检查与更新")
                self.tabs.addTab(self.parameters, "参数检查与更新")
                self.firmware.report_requested.connect(lambda target_sn: self.report_requested.emit(target_sn, "firmware"))
                self.parameters.report_requested.connect(lambda target_sn: self.report_requested.emit(target_sn, "parameters"))
                self.firmware.busy_changed.connect(self.set_busy)
                self.parameters.busy_changed.connect(self.set_busy)
                self.show_section(self.section)
        self.identity.setText(error or (f"SN {sn} · {context['robot_id']} · {dof} 轴 · {context['target']}"
                                       if binding else "请在整机测试中选择 SN，并连接对应机器人。"))
        self.tabs.setVisible(bool(binding))
        self.password.setEnabled(bool(binding))

    def show_section(self, section):
        self.section = section
        if self.tabs.count() and not self.busy:
            self.tabs.setCurrentIndex(0 if section == "firmware" else 1)

    def section_changed(self, index):
        if self.tabs.count() == 2:
            self.section = "firmware" if index == 0 else "parameters"
            self.parameters.cancel_review()
            self.firmware.cancel_review()

    def clear_credentials(self):
        self.password.clear()
        if self.parameters:
            self.parameters.confirm.setChecked(False)
            self.parameters.cancel_review()
        if self.firmware:
            self.firmware.confirm.setChecked(False)
            self.firmware.cancel_review()

    def set_busy(self, busy):
        self.busy = busy
        for index in range(self.tabs.count()):
            self.tabs.setTabEnabled(index, not busy or index == self.tabs.currentIndex())
        self.password.setEnabled(not busy)
        self.busy_changed.emit(busy)


class MotorFirmwarePanel(QWidget):
    report_requested = Signal(str)
    busy_changed = Signal(bool)
    STATUS = {
        "read": "已读取", "unreadable": "版本未读到", "verified": "刷写流程完成，版本匹配",
        "version_mismatch": "升级后版本不符", "version_unconfirmed": "升级后版本未确认",
        "flash_failed": "刷写失败", "not_attempted": "未执行",
    }

    def __init__(self, context, sn, dof, password, save_report, parent=None):
        super().__init__(parent)
        self.context, self.sn, self.dof = dict(context), sn, dof
        self.password, self.save_report = password, save_report
        self.worker = self.plan = self.pending_update = None
        self.report_available = False
        layout = QVBoxLayout(self)
        try:
            self.release = motor_firmware.firmware_release(verify=True)
            package = f"待刷固件 V{self.release['version']} · 构建 {self.release['build']}"
        except (OSError, ValueError) as error:
            self.release = None
            package = f"固件包不可用：{error}"
        layout.addWidget(text(package))
        controls = QHBoxLayout()
        self.check = QPushButton(f"检查全部 {dof} 台电机固件")
        self.check.clicked.connect(self.start_check)
        self.select_all = QPushButton("全选可更新电机")
        self.select_all.clicked.connect(self.toggle_select_all)
        controls.addWidget(self.check)
        controls.addWidget(self.select_all)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.status = text("先核对电机清单，再选择要刷写的电机。已是目标版本的电机也可重刷。")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["选择 / 电机 / 型号", "当前或更新前版本", "更新后版本", "结果"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self.update_enabled)
        layout.addWidget(self.table, 1)
        self.confirm = QCheckBox("机器人已固定、电机已失能，其他电机工具已退出，电机已按维护流程重新上电")
        self.confirm.toggled.connect(self.update_enabled)
        layout.addWidget(self.confirm)
        actions = QHBoxLayout()
        self.update = QPushButton("核对所选电机刷写清单…")
        self.update.clicked.connect(self.start_update)
        self.report = QPushButton("查看报告…")
        self.report.clicked.connect(lambda: self.report_requested.emit(self.sn))
        actions.addWidget(self.update, 1)
        actions.addWidget(self.report)
        layout.addLayout(actions)
        self.review = QWidget()
        review_layout = QVBoxLayout(self.review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        self.review_text = QPlainTextEdit(readOnly=True)
        self.review_text.setMaximumHeight(105)
        review_layout.addWidget(self.review_text)
        actions = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.cancel_review)
        confirm = QPushButton("开始刷写固件")
        confirm.clicked.connect(self.confirm_update)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(confirm)
        review_layout.addLayout(actions)
        layout.addWidget(self.review)
        self.update_enabled()

    def eligible_items(self):
        return [self.table.item(index, 0) for index in range(self.table.rowCount())
                if self.table.item(index, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable]

    def selected(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.eligible_items()
                if item.checkState() == Qt.CheckState.Checked]

    def can_update(self):
        return (not self.worker and self.release and self.plan and self.save_report
                and self.confirm.isChecked() and bool(self.selected()))

    def update_enabled(self, *_):
        self.cancel_review()
        self.update.setEnabled(bool(self.can_update()))
        items = self.eligible_items()
        self.select_all.setEnabled(bool(items) and self.plan is not None and not self.worker)
        checked = bool(items) and len(self.selected()) == len(items)
        self.select_all.setText("取消全选" if checked else "全选可更新电机")
        self.report.setEnabled(self.report_available and not self.worker)

    def toggle_select_all(self):
        items = self.eligible_items()
        checked = Qt.CheckState.Unchecked if len(self.selected()) == len(items) else Qt.CheckState.Checked
        self.table.blockSignals(True)
        for item in items:
            item.setCheckState(checked)
        self.table.blockSignals(False)
        self.update_enabled()

    def cancel_review(self):
        self.pending_update = None
        self.review.hide()

    def start_check(self):
        if not self.worker:
            self.begin("check")

    def start_update(self):
        if not self.can_update():
            return
        targets = self.selected()
        motors = "、".join(f"{target['interface']}/{target['motor_id']}" for target in targets)
        self.review_text.setPlainText(
            f"SN {self.sn} · {self.context['robot_id']} · {len(targets)} 台电机\n"
            f"{self.release['filename']}\n{motors}\n刷写期间保持电机供电和网线连接。"
        )
        self.pending_update = targets
        self.review.show()

    def confirm_update(self):
        if self.pending_update and self.can_update():
            self.begin("update", self.pending_update)

    def begin(self, operation, targets=None):
        password = self.password.text()
        if not password:
            self.status.setText("请输入本次机器人 sudo 密码。")
            return
        request = dict(operation=operation, request_id=str(uuid4()), robot_id=self.context["robot_id"], dof=self.dof)
        if operation == "update":
            request.update(targets=targets, maintenance_confirmed=True, firmware_sha256=self.release["sha256"],
                           plan_sha256=self.plan["plan_sha256"])
            try:
                self.save_report(self.sn, request, dict(state="requested", before=self.plan))
            except Exception as error:
                self.status.setText(f"刷写前记录未保存：{error}")
                return
        self.plan = None
        self.request = request
        self.status.setText("正在读取各电机版本…" if operation == "check" else "正在刷写固件，请保持供电和网线连接…")
        self.worker = ParameterWorker(self.context, password, request, self, performer=motor_firmware.perform)
        self.worker.completed.connect(self.complete)
        self.worker.finished.connect(self.finished_worker)
        for widget in (self.check, self.confirm, self.table):
            widget.setEnabled(False)
        self.update_enabled()
        self.busy_changed.emit(True)
        self.worker.start()

    def complete(self, result):
        is_check = self.request["operation"] == "check"
        if is_check and result.get("state") == "completed" and result.get("plan_sha256"):
            self.plan = result
        if "rows" in result:
            self.render(result["rows"])
        rows = result.get("rows", [])
        detail = result.get("detail")
        if result.get("state") == "completed" and is_check:
            count = sum(row["status"] == "read" for row in rows)
            detail = f"版本已读取 {count}/{len(rows)} 台。请按维护目标选择要刷写的电机。"
        elif result.get("state") == "completed":
            counts = [f"{label} {sum(row['status'] == state for row in rows)} 台"
                      for state, label in (("verified", "版本匹配"), ("version_unconfirmed", "版本待确认"),
                                           ("version_mismatch", "版本不符"), ("flash_failed", "失败"),
                                           ("not_attempted", "未执行"))]
            detail = " · ".join(counts)
        if not detail:
            detail = "未确认本次操作，请重新检查版本。"
        try:
            if self.save_report:
                self.save_report(self.sn, self.request, result)
                self.report_available = True
        except Exception as error:
            detail += f" 归档失败：{error}"
        self.status.setText(detail)

    def render(self, rows):
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            address = f"{row['interface']}/{row['motor_id']} · 型号 {row['model']}"
            item = QTableWidgetItem(address)
            if self.plan and row.get("eligible") and self.release:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, {key: row[key] for key in ("interface", "motor_id", "model")})
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.table.setItem(index, 0, item)
            status = self.STATUS.get(row.get("status"), "未知")
            for column, key in enumerate(("before", "after"), 1):
                version = row.get(key) or {}
                cell = QTableWidgetItem(version.get("version") or "—")
                if version.get("source") == "numeric":
                    cell.setToolTip("电机数字版本回执")
                self.table.setItem(index, column, cell)
            cell = QTableWidgetItem(status)
            cell.setToolTip(row.get("detail") or "")
            self.table.setItem(index, 3, cell)
        self.table.blockSignals(False)

    def finished_worker(self):
        self.worker.deleteLater()
        self.worker = None
        for widget in (self.check, self.confirm, self.table):
            widget.setEnabled(True)
        self.confirm.setChecked(False)
        self.update_enabled()
        self.busy_changed.emit(False)
