"""Native joint inspection; calibration is an explicit, separate SSH terminal."""

import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .calibration_context import extract_calibration_context
from .desktop_joint_angle_monitor import JointAngleMonitor
from .desktop_manual_check import ManualCalibrationDialog
from .desktop_postzero import PostzeroDialog
from .joint_inspection import (
    MAX_REFERENCE_BYTES,
    compare_joint_pose,
    extract_joint_pose,
    format_angle,
    load_reference,
    save_reference,
)

CALIBRATION_STYLE = """
QDialog#calibrationDialog { background:#eef2fa; color:#263451; }
QWidget { background:transparent; color:#263451; font-size:14px; }
QWidget#calibrationBody, QWidget#calibrationViewport,
QScrollArea#calibrationScroll { background:#eef2fa; border:0; }
QFrame#calibrationHeader, QFrame#calibrationFooter { background:#ffffff; border:0; }
QLabel#calibrationEyebrow { color:#4165a0; font-size:12px; font-weight:600; }
QLabel#calibrationTitle { color:#263451; font-size:24px; font-weight:700; }
QLabel#calibrationTarget { color:#263451; font-size:17px; font-weight:600; }
QLabel#calibrationCaption { color:#596a83; font-size:13px; }
QLabel#calibrationConnection { color:#4165a0; font-family:monospace; font-size:13px; }
QLabel#calibrationSection { color:#263451; font-size:15px; font-weight:600; }
QLabel#calibrationProgress { color:#4165a0; background:#edf2fa; border:1px solid #d9e3f1;
    border-radius:10px; padding:6px 12px; font-size:13px; font-weight:600; }
QFrame#calibrationSteps { background:#ffffff; border:1px solid #d7e0ee; border-radius:16px; }
QLabel#calibrationStepNumber { color:#4165a0; background:#edf2fa;
    border-radius:11px; font-size:13px; font-weight:600; }
QFrame#calibrationImpact { background:#fff7e9; border:1px solid #ead4aa; border-radius:16px; }
QLabel#calibrationImpactTitle { color:#78511a; font-size:15px; font-weight:600; }
QLabel#calibrationImpactText { color:#78511a; font-size:13px; }
QFrame#calibrationChecks { background:#ffffff; border:1px solid #d7e0ee; border-radius:16px; }
QCheckBox#calibrationCheck { background:transparent; color:#263451; spacing:10px;
    min-height:26px; font-weight:600; }
QCheckBox#calibrationCheck::indicator { width:19px; height:19px; background:#ffffff;
    border:1px solid #7e94b4; border-radius:5px; }
QCheckBox#calibrationCheck::indicator:checked { background:#4165a0; border:1px solid #4165a0;
    image:url('__CHECK_ICON__'); }
QCheckBox#calibrationCheck:focus { border:1px solid #4165a0; border-radius:5px; }
QPushButton#calibrationDetailsToggle { background:transparent; color:#4165a0;
    border:0; border-radius:6px; text-align:left; padding:8px 2px; font-size:13px; }
QPushButton#calibrationDetailsToggle:hover { color:#263451; background:#e6edf8; }
QPushButton#calibrationDetailsToggle:focus { border:1px solid #4165a0; }
QFrame#calibrationDetails { background:#e7edf7; border:1px solid #d7e0ee; border-radius:12px; }
QLabel#calibrationStatus { color:#596a83; font-size:13px; }
QLabel#calibrationStatus[blocked="true"] { color:#8a4f1d; }
QPushButton#calibrationLaunch { background:#4165a0; color:#ffffff; border:1px solid #4165a0;
    border-radius:10px; min-height:24px; padding:10px 20px; font-weight:600; }
QPushButton#calibrationLaunch:hover { background:#35588f; }
QPushButton#calibrationLaunch:pressed { background:#2c4b7b; }
QPushButton#calibrationLaunch:disabled { background:#e3e9f2; color:#65758e; border-color:#d5dfed; }
QPushButton#calibrationCancel { background:#ffffff; color:#4165a0; border:1px solid #bdcce4;
    border-radius:10px; min-height:24px; padding:10px 18px; font-weight:600; }
QPushButton#calibrationCancel:hover { background:#edf2fa; }
QPushButton#calibrationCancel:pressed { background:#e2eaf6; }
QPushButton#calibrationLaunch:focus, QPushButton#calibrationCancel:focus { border:2px solid #263451; }
""".replace("__CHECK_ICON__", (Path(__file__).parent / "web/brand/check-light.svg").as_posix())


def _label(text="", name="subtitle"):
    result = QLabel(text)
    result.setObjectName(name)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return result


class JointInspectionPanel(QWidget):
    manual_dialog_created = Signal(object)
    def __init__(self, connection_context=lambda: {}):
        super().__init__()
        self.connection_context = connection_context
        self._frame = None
        self._received_at = 0
        self.reference = None
        self.reference_path = None
        self.calibration_dialog = None
        self.calibration_terminal_opened = False
        self.postzero_dialog = None
        self.joint_angle_monitor = None
        self.zero_reference_dialog = None
        self.motor_control_dialog = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)
        self.identity = _label("尚未连接机器人", "sectionTitle")
        identity_row = QHBoxLayout()
        identity_row.addWidget(self.identity, 1)
        self.maintenance_toggle = QPushButton("标定维护")
        self.maintenance_toggle.setObjectName("secondary")
        self.maintenance_toggle.setCheckable(True)
        identity_row.addWidget(self.maintenance_toggle)
        layout.addLayout(identity_row)

        maintenance = QWidget()
        maintenance_layout = QVBoxLayout(maintenance)
        maintenance_layout.setContentsMargins(0, 0, 0, 0)
        tasks = QHBoxLayout()
        self.calibrate = QPushButton("标定（写入零位）")
        self.calibrate.clicked.connect(self.open_calibration)
        self.calibrate.setToolTip("使用机器人已有标零流程")
        self.motor_control = QPushButton("标定检查")
        self.motor_control.clicked.connect(self.open_motor_control)
        self.motor_control.setToolTip("按URDF动画手动摆动，自动检查所选限位")
        self.motor_control.setObjectName("secondary")
        tasks.addWidget(self.motor_control)
        tasks.addWidget(self.calibrate)
        tasks.addStretch()
        maintenance_layout.addLayout(tasks)
        self.calibration_note = _label("标定会写入硬件零位。标定检查需要人员按动画手动摆位。")
        maintenance_layout.addWidget(self.calibration_note)
        maintenance.hide()
        self.maintenance_toggle.toggled.connect(maintenance.setVisible)
        layout.addWidget(maintenance)
        self.feedback = _label()
        layout.addWidget(self.feedback)

        monitor_header = QHBoxLayout()
        monitor_header.addWidget(_label("角度监控", "sectionTitle"))
        monitor_header.addStretch()
        self.monitor_source = QComboBox()
        self.monitor_source.addItem("采集端关节角度 · 实时", "reported")
        self.monitor_source.addItem("电机原始角度 · 采样", "motor")
        self.monitor_source.addItem("编码器关节角度 · 实时", "encoder")
        self.monitor_source.setAccessibleName("角度监控数据来源")
        monitor_header.addWidget(self.monitor_source)
        layout.addLayout(monitor_header)
        self.monitor_stack = QStackedWidget()
        reported = QWidget()
        reported_layout = QVBoxLayout(reported)
        reported_layout.setContentsMargins(0, 0, 0, 0)
        self.monitor_stack.addWidget(reported)
        layout.addWidget(self.monitor_stack, 1)
        toolbar = QHBoxLayout()
        toolbar.addWidget(_label("显示单位"))
        self.unit = QComboBox()
        self.unit.addItem("角度 / °", "deg")
        self.unit.addItem("弧度 / rad", "rad")
        self.unit.setAccessibleName("关节位置显示单位")
        self.unit.currentIndexChanged.connect(self.render)
        toolbar.addWidget(self.unit)
        toolbar.addSpacing(12)
        toolbar.addWidget(_label("关节清单"))
        self.dof = QComboBox()
        for text, value in (("跟随配置 / 上报清单", None), ("24 关节", 24), ("28 关节", 28)):
            self.dof.addItem(text, value)
        self.dof.setAccessibleName("仅用于检查显示的关节数量")
        self.dof.currentIndexChanged.connect(self.render)
        toolbar.addWidget(self.dof)
        toolbar.addStretch()
        reported_layout.addLayout(toolbar)
        self.summary = _label("等待位置数据")
        reported_layout.addWidget(self.summary)
        self.data_status = _label()
        reported_layout.addWidget(self.data_status)
        self.table = QTableWidget(0, 6)
        self.table.setMinimumHeight(300)
        self.table.setAccessibleName("关节当前位置、参考位置、有符号偏差与接收新鲜度")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        reported_layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.save = QPushButton("保存当前为参考…")
        self.save.clicked.connect(self.save_current)
        self.load = QPushButton("载入参考…")
        self.load.clicked.connect(self.load_saved)
        self.clear = QPushButton("清除本次参考")
        self.clear.clicked.connect(self.clear_reference)
        for button in (self.save, self.load, self.clear):
            button.setMinimumHeight(40)
            button.setObjectName("secondary")
            actions.addWidget(button)
        actions.addStretch()
        reported_layout.addLayout(actions)
        self.reference_label = _label("未加载参考。参考姿态只保存在电脑，不写入电机。")
        reported_layout.addWidget(self.reference_label)
        reported_layout.addWidget(_label("逻辑关节角度来自 /joint_states；缺失或过期显示未知。参考文件保存在电脑，不修改零位。"))
        self.monitor_source.currentIndexChanged.connect(self.select_monitor_source)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_visible)
        self.timer.start(250)
        self.render()

    def set_frame(self, frame):
        self._frame = frame
        self._received_at = time.monotonic()

    def mark_disconnected(self):
        self._frame = None
        self._received_at = 0
        if self.calibration_dialog:
            self.calibration_dialog.reject()
        if self.postzero_dialog:
            self.postzero_dialog.invalidate()
        if self.joint_angle_monitor:
            self.joint_angle_monitor.stop_capture("机器人连接已断开，编码器读取已停止。")
        if self.zero_reference_dialog:
            self.zero_reference_dialog.invalidate()
        if self.motor_control_dialog:
            self.motor_control_dialog.invalidate()
        self.render()

    def open_motor_control(self):
        if self.zero_reference_capture_active():
            self.feedback.setText("请先结束标零位复查，再打开标定检查。")
            return
        if self.encoder_capture_active():
            self.feedback.setText("请先停止编码器角度读取，再打开标定检查。")
            return
        postzero_worker = self.postzero_dialog.worker if self.postzero_dialog else None
        if (postzero_worker and postzero_worker.isRunning()
                and self.postzero_dialog.mode.currentData() == "query"):
            self.feedback.setText("独立位置查询尚未结束，请先停止查询，再打开电机控制。")
            return
        if self.calibration_dialog is not None:
            self.feedback.setText("请先关闭标零流程，并确认独立标零终端和脚本已退出，再打开电机控制。")
            self.calibration_dialog.raise_()
            return
        if self.calibration_terminal_opened:
            answer = QMessageBox.question(
                self, "核对独立标零终端",
                "此前已打开独立标零终端，上位机无法可靠跟踪其结束。\n"
                "请确认标零脚本已退出，且已关闭独立标零终端，再继续电机控制。\n是否已确认？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.calibration_terminal_opened = False
        if self.motor_control_dialog and self.motor_control_dialog.isVisible():
            self.motor_control_dialog.raise_()
            self.motor_control_dialog.activateWindow()
            return
        if self.motor_control_dialog:
            if not self.motor_control_dialog.shutdown():
                self.feedback.setText("上一次电机控制会话仍在断开，请稍后重试。")
                return
            self.motor_control_dialog.deleteLater()
        self.motor_control_dialog = ManualCalibrationDialog(
            self.calibration_context, self.connection_context, self,
            observed_pose=self.observed_pose,
        )
        self.manual_dialog_created.emit(self.motor_control_dialog)
        self.feedback.clear()
        self.motor_control_dialog.show()

    def select_monitor_source(self):
        source = self.monitor_source.currentData()
        if source != "motor":
            if self.postzero_dialog and self.postzero_dialog.worker and self.postzero_dialog.worker.isRunning():
                self.postzero_dialog.stop_capture()
        if source == "motor":
            self.open_postzero()
        elif source == "encoder":
            if self.joint_angle_monitor is None:
                self.joint_angle_monitor = JointAngleMonitor(
                    self.calibration_context, self.connection_context, self.encoder_capture_problem, self,
                )
                self.joint_angle_monitor.stopped.connect(self.resume_raw_angle_controls)
                self.monitor_stack.addWidget(self.joint_angle_monitor)
            self.monitor_stack.setCurrentWidget(self.joint_angle_monitor)
        else:
            self.monitor_stack.setCurrentIndex(0)

    def encoder_capture_active(self):
        return self.joint_angle_monitor is not None and self.joint_angle_monitor.worker is not None

    def encoder_capture_problem(self):
        if self.zero_reference_capture_active():
            return "请先结束标零位复查，再开始编码器读取。"
        if self.postzero_dialog and self.postzero_dialog.worker and self.postzero_dialog.worker.isRunning():
            return "正在结束电机原始角度采样，请稍候。"
        if self.calibration_dialog is not None:
            return "请先关闭标零窗口，再开始编码器读取。"
        motor = self.motor_control_dialog
        if motor and (motor.isVisible() or motor.worker and motor.worker.isRunning()):
            return "请先结束控制会话并关闭标定检查窗口，再开始编码器读取。"
        return ""

    def resume_raw_angle_controls(self):
        if self.postzero_dialog:
            self.postzero_dialog.setEnabled(not (self.encoder_capture_active() or self.zero_reference_capture_active()))
            self.postzero_dialog.refresh()

    def zero_reference_capture_active(self):
        dialog = self.zero_reference_dialog
        return dialog is not None and dialog.reader.worker is not None

    def zero_reference_problem(self):
        if self.encoder_capture_active():
            return "请先停止编码器角度读取，再开始标零位复查。"
        return self.encoder_capture_problem()

    def open_zero_reference_check(self, sn, save_report):
        from .desktop_zero_reference_check import ZeroReferenceCheckDialog

        if self.zero_reference_dialog:
            self.zero_reference_dialog.raise_()
            self.zero_reference_dialog.activateWindow()
            return
        dialog = ZeroReferenceCheckDialog(
            sn, save_report, self.calibration_context, self.connection_context, self.zero_reference_problem, self,
        )
        self.zero_reference_dialog = dialog
        dialog.finished.connect(self.zero_reference_finished)
        dialog.reader.stopped.connect(self.resume_raw_angle_controls)
        dialog.open()

    def zero_reference_finished(self):
        dialog, self.zero_reference_dialog = self.zero_reference_dialog, None
        dialog.deleteLater()

    def open_postzero(self):
        # Selecting an angle source never starts capture.
        if self.monitor_source.currentData() != "motor":
            self.monitor_source.setCurrentIndex(self.monitor_source.findData("motor"))
            return
        if self.postzero_dialog is None:
            self.postzero_dialog = PostzeroDialog(self.calibration_context, self.connection_context, self, embedded=True)
            self.postzero_dialog.mode.currentIndexChanged.connect(self.warn_active_query)
            self.monitor_stack.addWidget(self.postzero_dialog)
        elif not (self.postzero_dialog.worker and self.postzero_dialog.worker.isRunning()):
            try:
                self.postzero_dialog.launch_context()
            except ValueError as error:
                self.postzero_dialog.invalidate(str(error))
        self.monitor_stack.setCurrentWidget(self.postzero_dialog)
        self.postzero_dialog.setEnabled(not (self.encoder_capture_active() or self.zero_reference_capture_active()))
        self.postzero_dialog.timer.start(250)
        self.postzero_dialog.show()

    def warn_active_query(self):
        worker = self.motor_control_dialog.worker if self.motor_control_dialog else None
        dialog = self.postzero_dialog
        if worker and worker.isRunning() and dialog and dialog.mode.currentData() == "query":
            dialog.mode.setCurrentIndex(dialog.mode.findData("passive"))
            message = "电机控制会话期间请勿并发主动位置查询；已切换为只监听返回帧。"
            dialog.status.setText(message)
            self.feedback.setText(message)

    def pose(self):
        return self.observed_pose(self.dof.currentData())

    def observed_pose(self, dof=None):
        return extract_joint_pose(
            self._frame, dof=dof,
            elapsed_ms=max(0, (time.monotonic() - self._received_at) * 1000) if self._frame else 0,
            transport_connected=self._frame is not None,
        )

    def calibration_context(self):
        # Calibration checks identity/configuration, never the angle display selector.
        return extract_calibration_context(
            self._frame,
            elapsed_ms=max(0, (time.monotonic() - self._received_at) * 1000) if self._frame else 0,
            transport_connected=self._frame is not None,
        )

    def showEvent(self, event):
        super().showEvent(event)
        self.render()

    def refresh_visible(self):
        if self.isVisible():
            self.render()

    def render(self):
        if not hasattr(self, "table"):
            return
        pose = self.pose()
        comparison = compare_joint_pose(pose, self.reference)
        unit, symbol = self.unit.currentData(), "°" if self.unit.currentData() == "deg" else "rad"
        precision = 2 if unit == "deg" else 4
        source = "模拟数据 simulated · 非实机" if pose["source_kind"] == "simulated" else "ROS2 来源 · 实机仍待验收"
        self.identity.setText(
            f"{pose['robot_id']} · {source} · {pose['joint_count']} 个逻辑关节"
            if pose["robot_id"] else "尚未连接机器人"
        )
        fresh = sum(row["status"] == "fresh" for row in pose["rows"])
        deviations = [abs(row["deviation_rad"]) for row in comparison["rows"] if row["deviation_rad"] is not None]
        self.summary.setText(
            f"接收新鲜位置 {fresh} / {pose['joint_count']} · 原始接收时间 {pose['sampled_at'] or '未知'}"
            + (f" · 最大姿态偏差 {format_angle(max(deviations), unit)} {symbol}" if deviations else "")
        )
        issues = "；".join(pose["issues"])
        self.data_status.setText(
            "请先在设备设置连接机器人。" if not pose["robot_id"] else
            "部分角度缺失或已过期，详见表格；完整有效时可保存参考。" if issues else
            "位置完整，可保存为观察参考。"
        )
        self.data_status.setToolTip(issues)
        self.table.setHorizontalHeaderLabels([
            "索引 / 上报名称", f"当前 / {symbol}", f"参考 / {symbol}",
            f"当前 − 参考 / {symbol}", "接收年龄 / ms", "数据状态",
        ])
        self.table.setRowCount(len(comparison["rows"]))
        states = {"fresh": "接收新鲜", "stale": "已过期", "missing": "未提供", "invalid": "无效数值",
                  "unknown_age": "年龄未知", "disconnected": "连接未知"}
        for index, row in enumerate(comparison["rows"]):
            topic_fresh = pose["topic_age_ms"] is not None and pose["topic_age_ms"] <= pose["max_age_ms"]
            usable = row["position_rad"] if row["status"] == "fresh" and topic_fresh else None
            values = (
                f"{row['index']:02d} · {row['name']}", format_angle(usable, unit, precision=precision),
                format_angle(row["reference_rad"], unit, precision=precision),
                format_angle(row["deviation_rad"], unit, signed=True, precision=precision),
                "—" if row["age_ms"] is None else f"{row['age_ms']:.0f}",
                states[row["status"]] if topic_fresh else "topic新鲜度未知或过期",
            )
            for column, value in enumerate(values):
                item = self.table.item(index, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(index, column, item)
                item.setText(value)
                item.setToolTip(row["reason"] + f"\n原始位置 / rad：{row['position_rad']}（旧值不作当前值）")
                item.setForeground(QColor("#263451" if row["status"] == "fresh" else "#835714"))
        self.save.setEnabled(pose["can_save"])
        self.save.setToolTip("；".join(pose["issues"]) or "将当前完整姿态保存到电脑，原始单位rad")
        self.clear.setEnabled(self.reference is not None)
        if self.reference:
            self.reference_label.setText(
                f"参考：{self.reference_path or '本次参考'} · {self.reference['sampled_at']}\n{comparison['reason']}"
            )
        else:
            self.reference_label.setText("未加载参考。参考姿态只保存在电脑，不写入电机。")

    def save_current(self):
        pose = self.pose()
        if not pose["can_save"]:
            self.feedback.setText("不能保存：" + "；".join(pose["issues"]))
            return
        robot_id, source = pose["robot_id"], pose["source_kind"]
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", robot_id)
        filename = f"{safe_id}_{source}_{datetime.now():%Y%m%d-%H%M%S}_reference.json"
        path, _ = QFileDialog.getSaveFileName(self, "将参考姿态保存在电脑", filename, "参考姿态 (*.json)")
        if not path:
            return
        # File dialogs process live events. Re-read, and never save another selected robot.
        current = self.pose()
        if (current["robot_id"], current["source_kind"]) != (robot_id, source):
            self.feedback.setText("机器人已切换，本次保存取消。")
            return
        try:
            reference = save_reference(path, current)
        except (ValueError, OSError) as error:
            self.feedback.setText("参考保存失败：" + str(error))
            self.render()
            return
        self.reference, self.reference_path = reference, Path(path)
        self.feedback.setText("已保存参考姿态到电脑；没有修改机器人零位或软件偏移。")
        self.render()

    def load_saved(self):
        path, _ = QFileDialog.getOpenFileName(self, "载入电脑上的参考姿态", "", "参考姿态 (*.json)")
        if not path:
            return
        try:
            with open(path, "rb") as stream:
                raw = stream.read(MAX_REFERENCE_BYTES + 1)
            if len(raw) > MAX_REFERENCE_BYTES:
                raise ValueError("参考姿态文件超过128 KiB")
            reference = load_reference(raw.decode("utf-8"))
        except (OSError, ValueError) as error:
            self.feedback.setText("参考载入失败：" + str(error))
            return
        self.reference, self.reference_path = reference, Path(path)
        self.feedback.setText("已载入参考；身份或关节映射不一致时不计算偏差。")
        self.render()

    def clear_reference(self):
        self.reference, self.reference_path = None, None
        self.feedback.setText("已清除本次比较参考，电脑上的文件仍保留。")
        self.render()

    def open_calibration(self):
        if self.zero_reference_capture_active():
            self.feedback.setText("请先结束标零位复查，再打开标零。")
            return
        if self.encoder_capture_active():
            self.feedback.setText("请先停止编码器角度读取，再打开标零。")
            return
        motor = self.motor_control_dialog
        if motor and motor.worker and motor.worker.isRunning():
            self.feedback.setText("电机控制会话仍在运行，请先停止并断开控制会话，再打开标零。")
            return
        if self.calibration_dialog is not None:
            self.calibration_dialog.raise_()
            self.calibration_dialog.activateWindow()
            return
        self.calibration_dialog = CalibrationDialog(self, self.connection_context, self.calibration_context)
        self.calibration_dialog.finished.connect(lambda: setattr(self, "calibration_dialog", None))
        self.calibration_dialog.open()


class CalibrationDialog(QDialog):
    def __init__(self, parent, connection_context, calibration_context):
        super().__init__(parent)
        self.context, self.current_calibration = connection_context, calibration_context
        initial = calibration_context()
        self.initial_identity = (initial["robot_id"], initial["source_kind"], initial["stream_id"])
        self.initial_configuration = initial["configuration_binding"]
        self.configured_dof = initial["configured_dof"]
        self.initial_binding = self.binding(connection_context())
        self.setObjectName("calibrationDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#eef2fa"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#eef2fa"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#263451"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.setStyleSheet(CALIBRATION_STYLE)
        self.setWindowTitle("整机标定 · 运行前核对")
        available = self.screen().availableGeometry()
        self.resize(min(840, available.width() - 48), min(760, available.height() - 64))
        self.setMinimumSize(580, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("calibrationHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(26, 20, 26, 18)
        header_layout.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(_label("整机标定 / 现场操作", "calibrationEyebrow"), 1)
        self.progress = _label("已确认 0 / 3", "calibrationProgress")
        heading.addWidget(self.progress)
        header_layout.addLayout(heading)
        header_layout.addWidget(_label("运行前核对", "calibrationTitle"))
        header_layout.addWidget(_label("终端会优先复用机器人已有的编译产物，之后等待现场标定确认。", "calibrationCaption"))
        self.target = _label(initial.get("robot_id") or "尚未绑定机器人", "calibrationTarget")
        self.target.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.connection_target = _label(connection_context().get("target") or "需要上位机后台 SSH 连接", "calibrationConnection")
        self.connection_target.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header_layout.addWidget(self.target)
        header_layout.addWidget(self.connection_target)
        model_row = QHBoxLayout()
        model_label = _label("标零机型", "calibrationSection")
        self.calibration_dof = QComboBox()
        self.calibration_dof.setAccessibleName("独立选择硬件标零机型，不使用角度表的显示数量")
        self.calibration_dof.addItem("请选择标零机型", None)
        self.calibration_dof.addItem("RP1 · 24关节", 24)
        self.calibration_dof.addItem("RP1 · 28关节", 28)
        self.calibration_dof.setMinimumHeight(36)
        if self.configured_dof is not None:
            self.calibration_dof.setCurrentIndex(self.calibration_dof.findData(self.configured_dof))
            self.calibration_dof.setEnabled(False)
        model_label.setBuddy(self.calibration_dof)
        model_row.addWidget(model_label)
        model_row.addWidget(self.calibration_dof, 1)
        header_layout.addLayout(model_row)
        self.configuration = _label("", "calibrationCaption")
        header_layout.addWidget(self.configuration)
        layout.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("calibrationScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.viewport().setObjectName("calibrationViewport")
        self.scroll.viewport().setPalette(palette)
        self.scroll.viewport().setAutoFillBackground(True)
        content = QWidget()
        content.setObjectName("calibrationBody")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        content.setPalette(palette)
        content.setAutoFillBackground(True)
        detail = QVBoxLayout(content)
        detail.setContentsMargins(26, 18, 26, 18)
        detail.setSpacing(14)

        steps = QFrame()
        steps.setObjectName("calibrationSteps")
        steps_layout = QGridLayout(steps)
        steps_layout.setContentsMargins(16, 14, 16, 14)
        steps_layout.setHorizontalSpacing(18)
        for column, (title, caption) in enumerate((
            ("SSH 认证", "终端输入密码"),
            ("构建检查", "核对源码，按需编译"),
            ("现场标定", "逐台摆位、确认"),
        )):
            step = QHBoxLayout()
            step.setSpacing(9)
            number = _label(str(column + 1), "calibrationStepNumber")
            number.setFixedSize(25, 25)
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            step.addWidget(number, alignment=Qt.AlignmentFlag.AlignTop)
            text = QVBoxLayout()
            text.setSpacing(4)
            text.addWidget(_label(title, "calibrationSection"))
            caption_label = _label(caption, "calibrationCaption")
            caption_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text.addWidget(caption_label)
            step.addLayout(text, 1)
            steps_layout.addLayout(step, 0, column)
            steps_layout.setColumnStretch(column, 1)
        detail.addWidget(steps)

        impact = QFrame()
        impact.setObjectName("calibrationImpact")
        impact_layout = QVBoxLayout(impact)
        impact_layout.setContentsMargins(18, 14, 18, 14)
        impact_layout.setSpacing(7)
        impact_layout.addWidget(_label("将写入电机硬件零位", "calibrationImpactTitle"))
        for text in (
            "会使能电机并持续发送 MIT 阻尼指令；在脚本中按 Enter 才写零，随后失能。结束后须重新上下电并复查。",
            "SSH 断开或脚本异常不能保证自动失能。请现场操作，并在标定期间保持其他运控程序关闭。",
        ):
            impact_layout.addWidget(_label(text, "calibrationImpactText"))
        detail.addWidget(impact)

        checks = QFrame()
        checks.setObjectName("calibrationChecks")
        checks_layout = QVBoxLayout(checks)
        checks_layout.setContentsMargins(18, 16, 18, 16)
        checks_layout.setSpacing(10)
        checks_layout.addWidget(_label("现场确认", "calibrationSection"))
        self.acknowledgements = []
        for title, caption in (
            ("已核对机器人身份与配置", "现场确认关节数量、LRO 电机、接线和机械摆位流程正确。"),
            ("已停用控制程序并支撑机器人", "现场有人操作，标定期间不从其他终端启动运控。"),
            ("理解写零位及后续复查要求", "结束后人工重新上下电，并按运控流程复查。"),
        ):
            row = QVBoxLayout()
            row.setSpacing(1)
            check = QCheckBox(title)
            check.setObjectName("calibrationCheck")
            check.setAccessibleDescription(caption)
            check.toggled.connect(self.refresh)
            row.addWidget(check)
            help_text = _label(caption, "calibrationCaption")
            help_text.setContentsMargins(29, 0, 0, 0)
            row.addWidget(help_text)
            checks_layout.addLayout(row)
            self.acknowledgements.append(check)
        detail.addWidget(checks)

        self.details_toggle = QPushButton("展开脚本与恢复说明")
        self.details_toggle.setObjectName("calibrationDetailsToggle")
        self.details_toggle.setCheckable(True)
        detail.addWidget(self.details_toggle)
        self.technical_details = QFrame()
        self.technical_details.setObjectName("calibrationDetails")
        technical_layout = QVBoxLayout(self.technical_details)
        technical_layout.setContentsMargins(16, 12, 16, 12)
        technical_layout.setSpacing(8)
        for text in (
            "配套工具随采集端一次安装。核对通过后按选定的 24 或 28 轴配置调用 roboparty_deploy/scripts/set_zero.py。",
            "请先退出推理和其他电机工具。上位机优先核对现有 colcon 构建，需要时才以普通用户补编译。",
            "写硬件零位不会同步修改软件偏移；原脚本未逐电机验证写零结果，退出码或完成文字不代表标定通过。",
        ):
            text_label = _label(text, "calibrationCaption")
            text_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            technical_layout.addWidget(text_label)
        self.technical_details.hide()
        self.details_toggle.toggled.connect(self.toggle_details)
        detail.addWidget(self.technical_details)
        detail.addStretch()
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)

        footer = QFrame()
        footer.setObjectName("calibrationFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(26, 12, 26, 18)
        footer_layout.setSpacing(10)
        self.status = _label("", "calibrationStatus")
        footer_layout.addWidget(self.status)
        buttons = QHBoxLayout()
        cancel = QPushButton("返回角度检查")
        cancel.setObjectName("calibrationCancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        buttons.addStretch()
        self.launch = QPushButton("打开独立标定终端")
        self.launch.setObjectName("calibrationLaunch")
        self.launch.clicked.connect(self.launch_terminal)
        buttons.addWidget(self.launch)
        footer_layout.addLayout(buttons)
        layout.addWidget(footer)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.calibration_dof.currentIndexChanged.connect(self.model_changed)
        self.timer.start(300)
        self.refresh()

    def toggle_details(self, expanded):
        self.technical_details.setVisible(expanded)
        self.details_toggle.setText("收起脚本与恢复说明" if expanded else "展开脚本与恢复说明")

    def clear_confirmations(self):
        for check in self.acknowledgements:
            previous = check.blockSignals(True)
            check.setChecked(False)
            check.blockSignals(previous)

    def model_changed(self):
        self.clear_confirmations()
        self.refresh()

    def launch_context(self):
        status, context = self.current_calibration(), self.context()
        motor = getattr(self.parent(), "motor_control_dialog", None)
        if motor and motor.worker and motor.worker.isRunning():
            raise ValueError("电机控制会话仍在运行，请先停止并断开后再标零")
        if (status["robot_id"], status["source_kind"], status["stream_id"]) != self.initial_identity:
            raise ValueError("机器人身份或采集会话已改变，请关闭后重新核对")
        if self.binding(context) != self.initial_binding:
            raise ValueError("SSH连接或目标已改变，请关闭后重新核对")
        if not status["can_preflight"]:
            raise ValueError("；".join(status["issues"]))
        if status["configuration_binding"] != self.initial_configuration:
            raise ValueError("机器人监测配置已改变，请关闭后重新核对")
        dof = self.calibration_dof.currentData()
        if dof not in (24, 28):
            raise ValueError("请选择标零机型；上方角度表的显示数量不能代替现场选型")
        if status["configured_dof"] not in (None, dof) or status["health_dof"] not in (None, dof):
            raise ValueError("所选标零机型与机器人监测配置冲突，请先核对配置")
        if context.get("robot_id") != status["robot_id"] or not context.get("connected"):
            raise ValueError("需要通过上位机后台SSH连接当前机器人，不能使用手填地址代替绑定")
        if not context.get("known_hosts") or not Path(context["known_hosts"]).is_file():
            raise ValueError("缺少已核对的SSH主机指纹记录")
        return context

    @staticmethod
    def binding(context):
        return tuple(context.get(key) for key in (
            "robot_id", "target", "ssh_port", "known_hosts", "relay_url", "generation",
        ))

    def refresh(self):
        confirmed = sum(check.isChecked() for check in self.acknowledgements)
        dof = self.calibration_dof.currentData()
        self.configuration.setText(
            (f"{'已绑定监测配置' if self.configured_dof else '现场选择'}：{dof}关节。"
             if dof else "未取得明确的机型配置，请独立选择24或28关节。")
            + "选型不依赖ROS角度或名称清单，也不代表电机在线；须现场核对实际机型。"
        )
        try:
            context = self.launch_context()
            self.target.setText(context["robot_id"])
            self.connection_target.setText(context["target"])
            self.status.setText(
                "现场确认完成。打开终端后认证，随后自动核对并按需编译。"
                if confirmed == 3 else "请完成3项现场确认。密码仅在终端认证，不保存。"
            )
            ready = shutil.which("x-terminal-emulator") is not None
            if not ready:
                self.status.setText("未找到系统终端 x-terminal-emulator，暂不能打开标定终端。")
        except ValueError as error:
            ready = False
            self.clear_confirmations()
            self.status.setText(str(error))
        confirmed = sum(check.isChecked() for check in self.acknowledgements)
        self.progress.setText(f"已确认 {confirmed} / 3")
        if self.status.property("blocked") != (not ready):
            self.status.setProperty("blocked", not ready)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
        self.launch.setEnabled(ready and all(check.isChecked() for check in self.acknowledgements))

    def launch_terminal(self):
        from .calibration import calibration_ssh_arguments

        self.refresh()
        if not self.launch.isEnabled():
            return
        try:
            context = self.launch_context()
            args = calibration_ssh_arguments(
                context["target"], context["known_hosts"], robot_id=context["robot_id"],
                dof=self.calibration_dof.currentData(), key=context.get("key", ""), ssh_port=context.get("ssh_port", 22),
            )
            terminal = shutil.which("x-terminal-emulator")
            if not terminal:
                raise ValueError("未找到系统终端")
            launched, _ = QProcess.startDetached(terminal, ["-e", *args])
            if not launched:
                raise ValueError("系统终端启动失败，请重试")
        except (ValueError, OSError) as error:
            self.status.setText(str(error))
            return
        # Terminal launch/exit says nothing about successful hardware calibration.
        self.parent().calibration_terminal_opened = True
        self.parent().feedback.setText("已打开标定终端，等待认证和构建检查。未判定标定通过。")
        self.accept()
