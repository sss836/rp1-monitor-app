"""Native zero-reference recheck using the read-only encoder stream."""

import math
import time
from datetime import datetime, timezone
from uuid import uuid4

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .desktop_joint_angle_monitor import JointAngleMonitor
from .desktop_manual_check import joint_title
from .desktop_postzero import _label
from .zero_reference_check import ZeroReferenceCheck

DOF = 24
STATUSES = {
    "pending": "未检查",
    "awaiting_confirmation": "待确认工装",
    "collecting": "采样中",
    "passed": "通过",
    "failed": "未通过",
    "skipped": "未完成",
    "incomplete": "未完成",
}


def _angle(value):
    if value is None:
        return "未知"
    return "0.00°" if round(value, 2) == 0 else f"{value:+.2f}°"


class _ZeroReferenceReader(JointAngleMonitor):
    capture_started = Signal()
    event_received = Signal(dict)
    end_requested = Signal()

    def start_capture(self):
        previous = self.worker
        super().start_capture()
        if self.worker is not None and self.worker is not previous:
            self.capture_started.emit()

    def on_event(self, event):
        if event.get("session_id") != self._session_id:
            return
        super().on_event(event)
        if self.worker is not None and not self.worker.cancel_event.is_set():
            self.event_received.emit(event)

    def stop_requested(self):
        self.end_requested.emit()


class ZeroReferenceCheckDialog(QDialog):
    def __init__(self, sn, save_report, calibration_context, connection_context, blocked, parent=None):
        super().__init__(parent)
        self.sn = sn
        self.save_report = save_report
        self.calibration_context = calibration_context
        self.blocked = blocked
        initial = calibration_context()
        self.identity = tuple(initial.get(key) for key in ("robot_id", "stream_id", "source_kind"))
        self.engine = None
        self.run_id = self.configuration_sha256 = None
        self._end_reason = None
        self._pending_save = None
        self._close_requested = False
        self._shown_index = None
        self._armed_at_ns = 0
        self._report = {}

        self.setWindowTitle("SYS-006 · 标零位复查")
        self.resize(960, 760)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        identity = _label(f"整机 SN {sn} · {self.identity[0] or '尚未连接机器人'} · RP1 24 关节")
        identity.setObjectName("sectionTitle")
        layout.addWidget(identity)
        layout.addWidget(_label(
            "数据来源为电机编码器。每项确认原标零工装姿态后，"
            "连续采集 3 次新反馈。原始轴角须在 0° ±3° 内，波动须不超过 0.5°。"
        ))
        self.site_ready = QCheckBox("机器人已固定，电机处于失能状态，运控和标零工具已退出")
        layout.addWidget(self.site_ready)
        self.reader = _ZeroReferenceReader(
            calibration_context, connection_context, self.read_problem, self, fixed_dof=DOF,
        )
        self.reader.table.hide()
        self.reader.start.setText("开始复查")
        self.reader.stop.setText("结束本轮")
        self.reader.capture_started.connect(self.capture_started)
        self.reader.event_received.connect(self.on_event)
        self.reader.end_requested.connect(lambda: self.end_run("人员结束复查"))
        self.reader.stopped.connect(self.capture_stopped)
        self.reader.timer.timeout.connect(self.update_detail)
        self.site_ready.toggled.connect(self.reader.refresh)
        layout.addWidget(self.reader)

        self.current = _label("等待读取配置")
        self.current.setObjectName("sectionTitle")
        layout.addWidget(self.current)
        self.detail = _label()
        layout.addWidget(self.detail)
        actions = QHBoxLayout()
        self.arm_button = QPushButton("确认工装姿态，开始本项")
        self.retest_button = QPushButton("重测选中关节")
        self.skip_button = QPushButton("记未完成并继续")
        self.arm_button.clicked.connect(self.arm)
        self.retest_button.clicked.connect(self.retest)
        self.skip_button.clicked.connect(self.skip)
        for button in (self.arm_button, self.retest_button, self.skip_button):
            button.setMinimumHeight(36)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

        self.table = QTableWidget(0, 6)
        self.table.setAccessibleName("标零位复查逐关节结果")
        self.table.setHorizontalHeaderLabels(["关节", "逻辑参考", "逻辑实测", "角度偏差", "原始电机角度", "结果"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.update_controls)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.archive_status = _label()
        footer.addWidget(self.archive_status, 1)
        self.retry_save = QPushButton("重试保存")
        self.retry_save.clicked.connect(self.archive)
        self.retry_save.hide()
        footer.addWidget(self.retry_save)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.update_controls()

    def read_problem(self):
        if self._pending_save is not None:
            return "请先保存本轮复查记录。"
        current = self.calibration_context()
        if not self.identity[0] or tuple(current.get(key) for key in ("robot_id", "stream_id", "source_kind")) != self.identity:
            return "请连接本 SN 对应机器人，再重新打开复查。"
        return self.blocked() or ("" if self.site_ready.isChecked() else "请先确认机器人已固定且电机失能。")

    def capture_started(self):
        self.engine = ZeroReferenceCheck(DOF)
        self.run_id = str(uuid4())
        self.configuration_sha256 = None
        self._end_reason = None
        self._shown_index = None
        self._armed_at_ns = 0
        self.archive_status.clear()
        self.render()

    def on_event(self, event):
        if event["type"] == "metadata":
            self.configuration_sha256 = event["configuration_sha256"]
        elif event["type"] == "frame":
            if self._armed_at_ns and event.get("pc_received_monotonic_ns", 0) <= self._armed_at_ns:
                return
            report = self.engine.feed(event)
            if report["phase"] == "complete":
                self.end_run("逐项复查结束")
        self.render()

    def update_controls(self):
        running = self.reader.worker is not None and not self.reader.worker.cancel_event.is_set()
        ready = running and self.configuration_sha256 is not None and self._report.get("current_index") is not None
        phase = self._report.get("phase")
        self.site_ready.setEnabled(self.reader.worker is None)
        self.arm_button.setEnabled(ready and phase == "awaiting_confirmation")
        self.retest_button.setEnabled(ready and phase != "collecting" and self.table.currentRow() >= 0)
        self.skip_button.setEnabled(ready)
        self.skip_button.setText("记录不通过并继续" if phase == "failed" else "记未完成并继续")

    def arm(self):
        if self.arm_button.isEnabled():
            self._armed_at_ns = time.monotonic_ns()
            self.engine.arm()
            self.render()

    def retest(self):
        if self.retest_button.isEnabled():
            self.engine.retest(self.table.currentRow())
            self.render()

    def skip(self):
        if self.skip_button.isEnabled():
            report = (self.engine.continue_failed() if self._report.get("phase") == "failed"
                      else self.engine.skip())
            if report["phase"] == "complete":
                self.end_run("逐项复查结束")
            self.render()

    @staticmethod
    def raw_angles(motors, angles):
        angles = angles or [None] * len(motors)
        return " · ".join(f"{motor['interface']}/{motor['motor_id']} {_angle(value)}"
                          for motor, value in zip(motors, angles))

    def render(self):
        self._report = report = self.engine.report()
        rows, index, current = report["results"], report["current_index"], report["current"]
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (joint_title(row["joint"]), _angle(row["logical_reference_deg"]),
                      _angle(row["logical_measured_deg"]), _angle(row["logical_delta_deg"]),
                      self.raw_angles(row["motors"], row["raw_angles_deg"]),
                      STATUSES.get(row["status"], row["status"]))
            for column, value in enumerate(values):
                item = self.table.item(row_index, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row_index, column, item)
                if item.text() != value:
                    item.setText(value)
                item.setToolTip(row["joint"] if column == 0 else row.get("reason") or "")
        if index != self._shown_index and index is not None:
            self.table.selectRow(index)
            self.table.scrollToItem(self.table.item(index, 0))
        self._shown_index = index
        done = sum(row["status"] in ("passed", "failed", "skipped", "incomplete") for row in rows)
        if current is not None:
            self.current.setText(f"{index + 1}/{len(rows)} · {joint_title(current['joint'])} · 已记录 {done} 项")
        else:
            self.current.setText(f"本轮已记录 {done}/{len(rows)} 项")
        self.update_detail()
        self.update_controls()

    def update_detail(self):
        row = self._report.get("current")
        if not row:
            if self._report:
                self.detail.setText("逐项结果见下表。未完成项保留在本轮记录中。")
            return
        frame = self.reader._frame
        measured, raw = None, None
        if frame is not None:
            elapsed = max(0.0, time.monotonic() - self.reader._received_at)
            freshness = self.reader.frame_freshness(frame["sweep_s"])
            frame_ns = frame["frame_monotonic_ns"]

            def fresh_value(value, stamp):
                if stamp is not None and (frame_ns - stamp) / 1e9 + elapsed <= freshness:
                    return value
                return None

            index = row["index"]
            measured = fresh_value(frame["positions_deg"][index], frame["rx_monotonic_ns"][index])
            raw = [fresh_value(frame["raw_angles_deg"][axis], frame["raw_rx_monotonic_ns"][axis])
                   for axis in row["motor_indices"]]
        reference = row["logical_reference_deg"]
        delta = math.remainder(measured - reference, 360.0) if measured is not None else None
        self.detail.setText(
            f"{row['reason']} · 本项新样本 {row['sample_count']}/3\n"
            f"逻辑参考 {_angle(reference)} · 当前 {_angle(measured)} · 角度偏差 {_angle(delta)}\n"
            f"原始轴角 {self.raw_angles(row['motors'], raw)}"
        )

    def end_run(self, reason):
        if self.reader.worker is None or self._end_reason is not None:
            return
        self._end_reason = reason
        self.engine.pause()
        self.reader.stop_capture("正在结束读取…")
        self.render()

    def capture_stopped(self):
        self._end_reason = self._end_reason or self.reader._status or "读取连接已结束"
        self.engine.pause()
        report = self.engine.report()
        if self.configuration_sha256 and any(row["attempts"] or row["samples"] for row in report["results"]):
            robot, stream, preview_source = self.identity
            self._pending_save = dict(
                kind="zero_reference_check", sn=self.sn, robot_id=robot, stream_id=stream,
                preview_source=preview_source, dof=DOF, configuration_sha256=self.configuration_sha256,
                run_id=self.run_id, saved_at=datetime.now(timezone.utc).isoformat(),
                report=report, end_reason=self._end_reason,
            )
            self.archive()
        else:
            self.archive_status.setText("本轮尚无可保存的复查结果。")
        self.render()
        if self._close_requested and self._pending_save is None:
            super().reject()

    def archive(self):
        if self._pending_save is None:
            return
        try:
            if self.save_report(self._pending_save) is not True:
                raise ValueError("上位机未确认保存")
        except (OSError, ValueError) as error:
            self.archive_status.setText(f"本轮保存失败：{error}")
            self.retry_save.show()
        else:
            self._pending_save = None
            self.retry_save.hide()
            self.archive_status.setText(f"本轮结果已保存到 SN {self.sn}。")
        self.reader.refresh()

    def invalidate(self):
        self.end_run("机器人连接已断开")

    def shutdown(self):
        self.reader.password.clear()
        self.end_run("人员关闭复查窗口")
        return self.reader.worker is None

    def reject(self):
        self._close_requested = True
        if self.shutdown() and self._pending_save is None:
            super().reject()

    def closeEvent(self, event):
        self.reject()
        event.ignore()
