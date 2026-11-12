"""Native read-only live encoder angle monitor."""

import math
import threading
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .desktop_postzero import _binding, _label
from .postzero_transport import PostzeroCaptureError


class JointAngleWorker(QThread):
    received = Signal(dict)
    failed = Signal(str)

    def __init__(self, connection, password, dof, session_id, parent=None):
        super().__init__(parent)
        self.connection, self.password = dict(connection), password
        self.dof, self.session_id = dof, session_id
        self.cancel_event = threading.Event()

    def run(self):
        password, self.password = self.password, ""
        try:
            from .postzero_transport import capture_joint_angles

            capture_joint_angles(
                self.connection, password, self.dof, self.session_id,
                self.emit_received, self.cancel_event,
            )
        except PostzeroCaptureError as error:
            if not self.cancel_event.is_set():
                self.failed.emit(str(error))
        except Exception:
            if not self.cancel_event.is_set():
                self.failed.emit("读取未完成，请核对 SSH、sudo 权限和电机供电。")

    def emit_received(self, event):
        self.received.emit({**event, "pc_received_monotonic_ns": time.monotonic_ns()})


class JointAngleMonitor(QWidget):
    stopped = Signal()

    def __init__(self, calibration_context, connection_context, blocked, parent=None, *, fixed_dof=None):
        super().__init__(parent)
        self.calibration_context = calibration_context
        self.connection_context = connection_context
        self.blocked = blocked
        self.fixed_dof = fixed_dof
        self.worker = None
        self._session_id = None
        self._session_binding = None
        self._frame = None
        self._received_at = 0.0
        self._status = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(_label(
            "连续读取编码器位置，按机器人当前配置换算关节角度。"
            "请保持电机供电，退出运控、标零及其他电机工具。"
        ))
        form = QFormLayout()
        self.dof = QComboBox()
        choices = [("请选择机型", None), ("RP1 · 24 关节", 24), ("RP1 · 28 关节", 28)]
        for label, value in choices:
            if fixed_dof is not None and value != fixed_dof:
                continue
            self.dof.addItem(label, value)
        self.dof.setAccessibleName("编码器角度读取机型")
        form.addRow("机型", self.dof)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("本次 SSH 和 sudo 密码")
        self.password.setPlaceholderText("只用于本次认证，不保存")
        form.addRow("SSH / sudo 密码", self.password)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.start = QPushButton("开始读取")
        self.stop = QPushButton("停止读取")
        self.stop.setObjectName("secondary")
        for button in (self.start, self.stop):
            button.setMinimumHeight(40)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.status = _label()
        layout.addWidget(self.status)

        self.table = QTableWidget(0, 3)
        self.table.setAccessibleName("编码器关节角度和反馈年龄")
        self.table.setHorizontalHeaderLabels(["关节名称", "当前角度 / °", "反馈年龄 / s"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(300)
        layout.addWidget(self.table, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self.password.textChanged.connect(self.refresh)
        self.dof.currentIndexChanged.connect(self.refresh)
        self.start.clicked.connect(self.start_capture)
        self.stop.clicked.connect(self.stop_requested)
        self.refresh()

    @staticmethod
    def binding(status, connection):
        identity = tuple(status.get(key) for key in ("robot_id", "source_kind", "stream_id", "configuration_binding"))
        return identity, _binding(connection)

    def connection_problem(self, status, connection):
        if not status.get("robot_id"):
            return "请先在设备设置连接机器人。"
        if status.get("source_kind") != "ros2":
            return "编码器角度需要当前已连接的实机，模拟来源不可读取。"
        if (status.get("can_preflight") is not True or status.get("connected") is not True
                or connection.get("connected") is not True or status.get("robot_id") != connection.get("robot_id")):
            return "机器人连接或预览未就绪，请核对设备连接。"
        known_hosts = connection.get("known_hosts")
        if not connection.get("target") or not known_hosts or not Path(known_hosts).is_file():
            return "请先在设备设置核对并保存机器人 SSH 主机指纹。"
        dof = self.dof.currentData()
        if dof not in (24, 28):
            return "请选择 24 或 28 关节机型。"
        if any(status.get(key) not in (None, dof) for key in ("configured_dof", "health_dof")):
            return "所选机型与当前机器人配置不一致。"
        return ""

    def refresh(self, *_):
        status, connection = self.calibration_context(), self.connection_context()
        running = self.worker is not None
        configured = status.get("configured_dof") or status.get("health_dof")
        if self.fixed_dof is None and not running and configured in (24, 28) and self.dof.currentData() != configured:
            self.dof.setCurrentIndex(self.dof.findData(configured))
        problem = self.connection_problem(status, connection)
        if running and not self.worker.cancel_event.is_set():
            if problem or self.binding(status, connection) != self._session_binding:
                self.stop_capture("机器人或采集连接已改变，正在结束读取。")
        blocked = self.blocked() if not running else ""
        self.start.setEnabled(not running and not problem and not blocked and bool(self.password.text()))
        self.stop.setEnabled(running and not self.worker.cancel_event.is_set())
        self.password.setEnabled(not running)
        self.dof.setEnabled(self.fixed_dof is None and not running and configured not in (24, 28))
        if self._frame is None:
            message = self._status if running else problem or blocked or self._status
            self.status.setText(message or "输入本次密码后点击“开始读取”。")
            return
        self.render_frame()

    def start_capture(self):
        self.refresh()
        if not self.start.isEnabled():
            return
        status, connection = self.calibration_context(), self.connection_context()
        if self.connection_problem(status, connection) or self.blocked():
            self.refresh()
            return
        self._session_binding = self.binding(status, connection)
        self._session_id = str(uuid4())
        self.table.setRowCount(0)
        self._frame = None
        self._status = "正在认证并核对机器人配置…"
        self.worker = JointAngleWorker(connection, self.password.text(), self.dof.currentData(), self._session_id, self)
        self.worker.received.connect(self.on_event)
        self.worker.failed.connect(self.stop_capture)
        self.worker.finished.connect(self.capture_finished)
        self.worker.start()
        self.password.clear()
        self.refresh()

    def on_event(self, event):
        if self.worker is None or self.worker.cancel_event.is_set() or event.get("session_id") != self._session_id:
            return
        kind = event.get("type")
        if kind == "metadata":
            names = event["joint_names"]
            self.table.setRowCount(len(names))
            for index, name in enumerate(names):
                self.table.setItem(index, 0, QTableWidgetItem(name))
                for column, value in ((1, "未知"), (2, "—")):
                    item = QTableWidgetItem(value)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self.table.setItem(index, column, item)
            self._status = "配置已核对，正在等待首轮反馈…"
        elif kind == "frame":
            self._frame = event
            self._received_at = time.monotonic()
        elif kind == "error":
            self.stop_capture(event.get("message") or "编码器角度读取未完成。")
        elif kind == "complete":
            self.stop_capture(event.get("reason") or "本次读取已结束，可再次开始。")
        self.refresh()

    def render_frame(self):
        elapsed = max(0.0, time.monotonic() - self._received_at)
        sweep = self._frame["sweep_s"]
        # An early sample must remain readable until the following normal sweep.
        freshness = self.frame_freshness(sweep)
        frame_time = self._frame["frame_monotonic_ns"]
        fresh = 0
        for index, (angle, stamp) in enumerate(zip(self._frame["positions_deg"], self._frame["rx_monotonic_ns"])):
            age = max(0, frame_time - stamp) / 1e9 + elapsed if stamp is not None else None
            available = angle is not None and math.isfinite(angle) and age is not None and age <= freshness
            fresh += available
            value = f"{angle:+.2f}" if available else "未知"
            age_text = f"{age:.1f}" if available else "过期" if age is not None and angle is not None else "缺失"
            for column, text in ((1, value), (2, age_text)):
                item = self.table.item(index, column)
                if item is not None and item.text() != text:
                    item.setText(text)
        self.status.setText(
            f"有效角度 {fresh}/{self.table.rowCount()} · 最近一轮 {sweep:.1f} 秒"
            if fresh else "暂未收到新鲜角度反馈，读数为未知。"
        )

    @staticmethod
    def frame_freshness(sweep):
        return min(12.0, max(2.0, 2 * sweep + 0.25))

    def stop_requested(self):
        self.stop_capture("正在停止读取…")

    def stop_capture(self, reason="已停止读取。"):
        if self.worker is not None:
            self.worker.cancel_event.set()
        self.password.clear()
        self._frame = None
        self._status = reason
        for index in range(self.table.rowCount()):
            self.table.item(index, 1).setText("未知")
            self.table.item(index, 2).setText("—")
        self.refresh()

    def capture_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        if not worker.cancel_event.is_set() or self._status == "正在停止读取…":
            self.stop_capture()
        self.refresh()
        self.stopped.emit()

    def shutdown(self):
        self.stop_capture()
        return self.worker is None or not self.worker.isRunning()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event):
        self.timer.stop()
        self.stop_capture()
        super().hideEvent(event)
