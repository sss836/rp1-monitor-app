"""Explicit desktop motor control session; opening the dialog sends no commands."""

import importlib
import json
import math
import queue
import threading
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def _label(text=""):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _binding(context):
    return tuple(str(context.get(key, "")) for key in (
        "robot_id", "target", "ssh_port", "known_hosts", "relay_url", "generation",
    ))


class MotorControlWorker(QThread):
    received = Signal(dict)
    failed = Signal(str)

    def __init__(self, connection, password, dof, session_id, parent=None):
        super().__init__(parent)
        self.connection, self.password = dict(connection), password
        self.dof, self.session_id = dof, session_id
        self.commands = queue.Queue()
        self.cancel_event = threading.Event()

    def run(self):
        transport = None
        try:
            transport = importlib.import_module(".motor_control_transport", __package__)

            transport.run_motor_session(
                self.connection, self.password, self.dof, self.session_id,
                self.received.emit, self.commands, self.cancel_event,
            )
        except Exception as error:
            controlled_error = getattr(transport, "MotorControlError", None)
            if isinstance(controlled_error, type) and isinstance(error, controlled_error):
                # Transport errors contain only our curated operator-facing text.
                self.failed.emit(str(error))
            else:
                # Arbitrary exception text can include remote commands or credentials.
                self.failed.emit("控制连接异常。已请求结束会话；请现场确认电机状态及急停，重新连接前核对SSH和sudo权限。")
        finally:
            self.password = ""

    def stop(self):
        self.cancel_event.set()


class MotorControlDialog(QDialog):
    def __init__(self, calibration_context, connection_context, parent=None):
        super().__init__(parent)
        self.current_context, self.connection_context = calibration_context, connection_context
        self.initial_binding = _binding(connection_context())
        initial = calibration_context()
        self.initial_identity = tuple(initial.get(key) for key in ("robot_id", "source_kind", "stream_id"))
        self.initial_configuration = initial.get("configuration_binding")
        self.worker = None
        self.session_id = None
        self.trajectory = None
        self.check_plan = None
        self.check_plan_load_error = ""
        self.check_result = None
        self.check_samples = []
        self.check_samples_omitted = 0
        self.check_waiting_review = False
        self.metadata = None
        self.last_positions = None
        self.last_feedback_at = 0.0
        self._start_pose_override = None
        self._start_pose_override_at = 0.0
        self._start_pose_override_tolerance_deg = None
        self._check_start_tolerance_deg = None
        self._preparation_confirmed_start = None
        self._pose_report_cache_key = None
        self._pose_report_cache = None
        self._pose_report_cache_error = None
        self.state = "disconnected"
        self._pending = False
        self._confirming_parameters = False
        self._invalidated = False
        self._close_requested = False
        self._completed = False
        self._failure_reason = ""
        self._completion_summary = ""
        self.setObjectName("motorControlDialog")
        self.setWindowTitle("电机使能与标定检查")
        self.setStyleSheet("""
            QDialog#motorControlDialog, QScrollArea, QWidget#motorControlBody { background:#eef2fa; color:#263451; }
            QLabel, QCheckBox { background:transparent; color:#263451; font-size:14px; }
            QFrame#motorControlNotice { background:#fff7e9; border:1px solid #ead4aa; border-radius:14px; }
            QLineEdit, QComboBox, QDoubleSpinBox { background:white; color:#263451; font-size:14px; border:1px solid #bdcce4; border-radius:9px; padding:7px; }
            QPushButton { font-size:14px; min-height:28px; padding:8px 14px; border:1px solid #bdcce4; border-radius:10px; color:#4165a0; background:white; }
            QPushButton:hover { background:#e6edf8; }
            QPushButton:pressed { background:#d9e4f4; }
            QPushButton:disabled { background:#e3e9f2; color:#65758e; }
            QPushButton#primary { background:#4165a0; color:white; }
            QPushButton#primary:disabled { background:#d5dfed; color:#65758e; }
            QPushButton#motorStop { background:#a2363b; color:white; border-color:#a2363b; }
            QPushButton#motorStop:disabled { background:#e3e9f2; color:#65758e; border-color:#bdcce4; }
            QPushButton:focus, QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QCheckBox:focus { border:2px solid #4165a0; }
            QCheckBox { spacing:10px; min-height:30px; }
            QCheckBox::indicator { width:19px; height:19px; background:white; border:1px solid #7e94b4; border-radius:5px; }
            QCheckBox::indicator:checked { background:#4165a0; image:url('__CHECK_ICON__'); }
            QTableWidget { background:white; alternate-background-color:#f2f5fb; color:#263451; border:1px solid #bdcce4; }
            QHeaderView::section { background:#e6edf8; color:#263451; padding:8px; border:0; }
        """.replace("__CHECK_ICON__", (Path(__file__).parent / "web/brand/check-light.svg").as_posix()))
        available = self.screen().availableGeometry()
        self.resize(min(1000, available.width() - 40), min(920, available.height() - 56))
        self.setMinimumSize(640, 520)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        title = _label("电机使能与标定检查")
        title.setStyleSheet("font-size:24px; font-weight:700;")
        outer.addWidget(title)
        connection = connection_context()
        self.identity = _label(f"{connection.get('robot_id') or '尚未绑定机器人'} · {connection.get('target') or '尚未绑定SSH'}")
        outer.addWidget(self.identity)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("motorControlBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        notice = QFrame()
        notice.setObjectName("motorControlNotice")
        notice_layout = QVBoxLayout(notice)
        notice_layout.addWidget(_label(
            "连接仅核对配置，点击使能或运行才发送电机指令。"
            "请先支撑固定机器人，并退出现有运控与标零工具。"
        ))
        self.impact_toggle = QPushButton("查看机器人侧执行范围")
        self.impact_toggle.setCheckable(True)
        self.impact_toggle.setStyleSheet("text-align:left; border:0; background:transparent; padding:2px; min-height:24px;")
        notice_layout.addWidget(self.impact_toggle)
        self.impact_details = _label(
            "通过SSH在机器人内存运行临时控制代码，读取现有deploy配置、调用已有SDK；"
            "创建/run/lock/rp1-monitor-motor-control.lock控制锁。"
            "不改写deploy源码、固件参数、硬件零位或自启服务。"
            "检测到已有运控或标零工具时拒绝控制。"
        )
        self.impact_details.hide()
        self.impact_toggle.toggled.connect(self.impact_details.setVisible)
        notice_layout.addWidget(self.impact_details)
        layout.addWidget(notice)
        form = QFormLayout()
        self.dof = QComboBox()
        self.dof.addItem("请选择机型", None)
        self.dof.addItem("RP1 · 24电机", 24)
        self.dof.addItem("RP1 · 28电机", 28)
        configured = calibration_context().get("configured_dof")
        if configured in (24, 28):
            self.dof.setCurrentIndex(self.dof.findData(configured))
        self.dof.setAccessibleName("控制会话机型")
        form.addRow("机型", self.dof)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("仅用于本次SSH和sudo认证，不保存")
        self.password.setAccessibleName("本次SSH和sudo密码")
        form.addRow("SSH / sudo密码", self.password)
        layout.addLayout(form)
        self.site_ready = QCheckBox("机器人已支撑固定、人员已避开运动区域，并已停止现有运控与标零工具")
        self.site_ready.setAccessibleName("现场控制条件确认")
        layout.addWidget(self.site_ready)
        connect_actions = QHBoxLayout()
        self.connect_button = QPushButton("连接控制会话")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self.connect_session)
        self.enable_button = QPushButton("使能电机")
        self.enable_button.clicked.connect(self.enable_motors)
        self.disable_button = QPushButton("失能电机")
        self.disable_button.clicked.connect(self.disable_motors)
        self.disconnect_button = QPushButton("断开控制会话")
        self.disconnect_button.clicked.connect(self.disconnect_session)
        for button in (self.connect_button, self.enable_button, self.disable_button, self.disconnect_button):
            connect_actions.addWidget(button)
        layout.addLayout(connect_actions)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("标定检查（视频同款）", "calibration_check")
        self.mode_combo.addItem("普通电机轨迹（HMI NPZ / JSON）", "trajectory")
        self.mode_combo.setAccessibleName("电机执行模式")
        layout.addWidget(self.mode_combo)
        self.check_plan_label = _label()
        layout.addWidget(self.check_plan_label)
        self.check_readiness_label = _label("可离线查看视频方案；连接后将核对真机映射与闭链运行环境。")
        layout.addWidget(self.check_readiness_label)
        self.check_start_pose_label = _label("起始姿态待检查：等待已绑定机器人的新鲜角度反馈。准备偏差不代表端点标零误差。")
        self.check_start_pose_label.setAccessibleName("起始姿态检查摘要")
        layout.addWidget(self.check_start_pose_label)
        self.check_start_pose_button = QPushButton("检查起始姿态（只核对当前反馈）")
        self.check_start_pose_button.clicked.connect(self.inspect_start_pose)
        layout.addWidget(self.check_start_pose_button)
        self.prepare_speed_label = _label("旧版复位速度参数（本模式不使用）")
        self.prepare_speed_label.setParent(self)
        self.prepare_speed_label.hide()
        self.prepare_speed = self._number(0.01, 20, 5, 2, " °/s")
        self.prepare_speed.setParent(self)
        self.prepare_speed.hide()
        self.prepare_timeout_label = _label("旧版复位时限参数（本模式不使用）")
        self.prepare_timeout_label.setParent(self)
        self.prepare_timeout_label.hide()
        self.prepare_timeout = self._number(1, 90, 90, 1, " s")
        self.prepare_timeout.setParent(self)
        self.prepare_timeout.hide()
        self.prepare_tolerance_label = _label("姿态参考差值（仅展示，不用于控制门槛）")
        self.prepare_tolerance_label.setParent(self)
        self.prepare_tolerance_label.hide()
        self.prepare_tolerance = self._number(0.5, 15, 5, 1, " °")
        self.prepare_tolerance.setParent(self)
        self.prepare_tolerance.hide()
        self.prepare_gains_label = _label(
            "复位按deploy固定4秒三次平滑插值；逐物理电机Kp为deployKp/2.5、Kd保持deployKd，结束恢复完整deployKp。"
            "不额外增加2秒渐增；下方搜索Kp/Kd独立设置。"
            "复位、保持及检查交接不按角度偏差或停稳窗口中止；仍读取力矩反馈，保留通信、故障与停止路径。"
            "此处只读展示不会发送运动指令。"
        )
        self.prepare_gains_label.setAccessibleName("独立复位与搜索增益说明")
        layout.addWidget(self.prepare_gains_label)
        self.prepare_check_button = QPushButton("复位到检查起点…")
        self.prepare_check_button.setAccessibleName("人工确认后按deploy固定4秒复位到标定检查起点")
        self.prepare_check_button.clicked.connect(self.prepare_calibration_check)
        layout.addWidget(self.prepare_check_button)
        self.prepare_check_hint = _label("复位不可用：请先连接并使能，等待新鲜角度反馈。")
        self.prepare_check_hint.setAccessibleName("固定4秒复位可用条件与未就绪原因")
        layout.addWidget(self.prepare_check_hint)
        self.check_mapping_toggle = QPushButton("展开起始姿态与关节—电机映射")
        self.check_mapping_toggle.setCheckable(True)
        layout.addWidget(self.check_mapping_toggle)
        self.check_mapping_table = QTableWidget(0, 7)
        self.check_mapping_table.setHorizontalHeaderLabels([
            "逻辑关节（视频坐标）", "物理电机 / 总线 / ID", "方向 / 联动", "当前逻辑角 / °",
            "视频开始角 / °", "准备偏差 / °", "起始姿态状态",
        ])
        self.check_mapping_table.setAccessibleName("标定检查完整起始姿态与逻辑关节物理电机映射")
        self.check_mapping_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.check_mapping_table.verticalHeader().hide()
        # Content-based resizing runs synchronously for every streamed cell.
        # Keep stable, user-resizable columns instead of remeasuring 196 cells.
        self.check_mapping_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((240, 180, 190, 130, 130, 130, 150)):
            self.check_mapping_table.setColumnWidth(column, width)
        self.check_mapping_table.horizontalHeader().setStretchLastSection(True)
        self.check_mapping_table.setMinimumHeight(220)
        self.check_mapping_table.hide()
        self.check_mapping_toggle.toggled.connect(self.check_mapping_table.setVisible)
        layout.addWidget(self.check_mapping_table)
        preview_actions = QHBoxLayout()
        self.check_preview_button = QPushButton("查看视频方案")
        self.check_preview_button.clicked.connect(self.open_check_preview)
        self.check_plan_button = QPushButton("查看标定检查说明")
        self.check_plan_button.clicked.connect(self.open_check_document)
        preview_actions.addWidget(self.check_preview_button)
        preview_actions.addWidget(self.check_plan_button)
        preview_actions.addStretch()
        layout.addLayout(preview_actions)
        self.trajectory_notice = _label(
            "轨迹使用物理电机SDK输出轴坐标（弧度），顺序以机器人deploy电机映射为准；"
            "第N列对应下表第N行，NPZ无映射时也按此顺序。踝部两台物理电机分别控制，"
            "不要导入URDF逻辑关节角度。此入口不执行自动限位搜索。"
        )
        layout.addWidget(self.trajectory_notice)
        trajectory_actions = QHBoxLayout()
        self.load_button = QPushButton("载入轨迹…")
        self.load_button.clicked.connect(self.load_trajectory)
        trajectory_actions.addWidget(self.load_button)
        self.trajectory_label = _label("未载入轨迹 · 支持HMI NPZ与JSON")
        trajectory_actions.addWidget(self.trajectory_label, 1)
        layout.addLayout(trajectory_actions)
        params = QGridLayout()
        self.speed = self._number(0.01, 10, 1, 2, " °/s")
        self.minimum = self._number(-180, 180, -5, 2, " °")
        self.maximum = self._number(-180, 180, 5, 2, " °")
        self.kp = self._number(0.01, 50, 5, 2)
        self.kd = self._number(0, 5, 0.5, 2)
        self.max_torque = self._number(0.01, 100, 10, 2, " N·m")
        self.search_speed = self._number(0.01, 1, 1, 2, " °/s")
        self.search_overtravel = self._number(0, 3, 1, 2, " °")
        self.search_timeout = self._number(1, 120, 30, 1, " s")
        self.contact_torque = self._number(0.01, 99, 0.5, 2, " N·m")
        self.torque_rise = self._number(0.01, 99, 3, 2, " N·m")
        fields = (("速度上限", self.speed), ("允许位置下界", self.minimum), ("允许位置上界", self.maximum),
                  ("Kp", self.kp), ("Kd", self.kd), ("中止力矩", self.max_torque),
                  ("近端点搜索速度", self.search_speed), ("候选接触力矩", self.contact_torque),
                  ("力矩相对升幅", self.torque_rise), ("参考端点后追加搜索上限", self.search_overtravel),
                  ("每组搜索时限（未检出也结束）", self.search_timeout))
        self.parameter_widgets = [widget for _, widget in fields]
        self.parameter_labels = {}
        for index, (name, widget) in enumerate(fields):
            widget.setAccessibleName(name)
            label = _label(name)
            self.parameter_labels[widget] = label
            params.addWidget(label, index // 3 * 2, index % 3)
            params.addWidget(widget, index // 3 * 2 + 1, index % 3)
        layout.addLayout(params)
        self.ordinary_summary = _label(
            "使能使用低阻尼，不支撑机器人站立。默认仅允许±5°小范围；首帧与实时角度相差超过3°会拒绝运行。"
            "按速度上限平滑接近首帧并播放，轨迹结束自动失能。"
        )
        layout.addWidget(self.ordinary_summary)
        self.check_summary = _label(
            "检查沿视频分组姿态转场，默认以1°/s搜索；实际角度1秒内跨度≤0.2°、力矩达到接触值并相对搜索前负载上升时识别候选，"
            "物理电机目标不再以固定20°/s中止，转场和搜索仍按所设逻辑关节速度规划。"
            "同组任一轴出现候选阻挡即冻结整组目标、缓退，再等待现场核对；软件不验证物理停稳。"
            "按每关节同侧参考角比较，容差±3°；候选结果不自动判定整机标定通过。"
            "肩部约166°端点仍有大幅运动。接触默认0.5 N·m仍为未验证的软件候选；力矩升幅默认3 N·m用于相对搜索前负载的接触判断。"
            "本标定模式不设置统一力矩中止阈值，仍保留反馈、故障与停止路径。工厂参数待按每关节负载参考确定。"
            "按所设低速持续推进至接触或本组超时，非无限推进；每组超时后未碰到也记录未检出。"
            "低Kp可能达不到接触力矩；不会自动提高增益或力矩。"
        )
        layout.addWidget(self.check_summary)
        self.check_parameter_hint = _label()
        self.check_parameter_hint.setAccessibleName("候选参数配合检查提示")
        layout.addWidget(self.check_parameter_hint)
        self.check_options_confirmed = QCheckBox(
            "已核对本次机型、电机与逻辑关节映射、同侧机械参考及力矩参数，确认现场允许视频方案各组动作"
        )
        self.check_options_confirmed.setAccessibleName("标定检查参数与机械参考确认")
        layout.addWidget(self.check_options_confirmed)
        run_actions = QHBoxLayout()
        self.run_button = QPushButton("运行轨迹…")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.run_trajectory)
        self.stop_button = QPushButton("停止并失能")
        self.stop_button.setObjectName("motorStop")
        self.stop_button.clicked.connect(self.stop_and_disable)
        run_actions.addWidget(self.run_button)
        self.continue_check_button = QPushButton("核对姿态并继续下一组…")
        self.continue_check_button.clicked.connect(self.continue_check)
        run_actions.addWidget(self.continue_check_button)
        layout.addLayout(run_actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setAccessibleName("轨迹播放进度")
        layout.addWidget(self.progress)
        self.status = _label("请先选择机型并连接控制会话；连接不会自动使能。")
        self.status.setAccessibleName("电机控制状态与错误信息")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["物理电机 / 总线 / ID", "当前角度 / °", "力矩 / N·m", "使能/反馈状态"])
        self.table.setAccessibleName("物理电机实时角度、力矩和使能反馈状态")
        self.table.setMinimumHeight(220)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((230, 170, 170, 230)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.check_results_table = QTableWidget(0, 6)
        self.check_results_table.setHorizontalHeaderLabels([
            "逻辑关节", "同侧参考 / °", "候选实测 / °", "偏差 / °", "±3°比较", "状态",
        ])
        self.check_results_table.setAccessibleName("标定检查候选端点结果")
        self.check_results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.check_results_table.setAlternatingRowColors(True)
        self.check_results_table.verticalHeader().hide()
        self.check_results_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.check_results_table.horizontalHeader().setStretchLastSection(True)
        self.check_results_table.setMinimumHeight(220)
        layout.addWidget(self.check_results_table)
        self.save_check_result_button = QPushButton("保存本次检查结果…")
        self.save_check_result_button.clicked.connect(self.save_check_result)
        layout.addWidget(self.save_check_result_button)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        footer = QHBoxLayout()
        footer.addWidget(self.stop_button)
        footer.addStretch()
        footer.addWidget(self.close_button)
        outer.addLayout(footer)
        self.dof.currentIndexChanged.connect(self.model_changed)
        self.mode_combo.currentIndexChanged.connect(self.mode_changed)
        self.check_options_confirmed.toggled.connect(self.refresh)
        for widget in self.parameter_widgets:
            widget.valueChanged.connect(self.check_parameters_changed)
        self.prepare_speed.valueChanged.connect(self.refresh)
        self.prepare_timeout.valueChanged.connect(self.refresh)
        self.prepare_tolerance.valueChanged.connect(self.refresh)
        self.password.textChanged.connect(self.refresh)
        self.site_ready.toggled.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.mode_changed()
        self.refresh()

    @staticmethod
    def _number(low, high, value, decimals, suffix=""):
        widget = QDoubleSpinBox()
        widget.setRange(low, high)
        widget.setDecimals(decimals)
        widget.setValue(value)
        widget.setSuffix(suffix)
        return widget

    def is_check_mode(self):
        return self.mode_combo.currentData() == "calibration_check"

    def mode_changed(self):
        checking = self.is_check_mode()
        self.speed.setMaximum(20 if checking else 10)
        # Mode presets are applied only on initialization or an explicit mode switch.
        # Editing a parameter never triggers an automatic gain increase.
        self.speed.setValue(1)
        self.kp.setValue(50 if checking else 5)
        self.kd.setValue(0.5)
        self.max_torque.setValue(10)
        if checking:
            self.search_speed.setValue(1)
            self.search_overtravel.setValue(1)
            self.contact_torque.setValue(0.5)
            self.torque_rise.setValue(3)
        else:
            self.minimum.setValue(-5)
            self.maximum.setValue(5)
        for widget in (self.minimum, self.maximum, self.max_torque):
            widget.setVisible(not checking)
            self.parameter_labels[widget].setVisible(not checking)
        for widget in (self.search_speed, self.search_timeout, self.contact_torque, self.torque_rise):
            widget.setVisible(checking)
            self.parameter_labels[widget].setVisible(checking)
        self.search_overtravel.hide()
        self.parameter_labels[self.search_overtravel].hide()
        for widget in (self.check_plan_label, self.check_readiness_label, self.check_start_pose_label,
                       self.check_start_pose_button, self.prepare_check_button, self.prepare_check_hint,
                       self.prepare_gains_label,
                       self.check_mapping_toggle, self.check_preview_button,
                       self.check_plan_button, self.check_summary, self.check_parameter_hint, self.check_options_confirmed,
                       self.continue_check_button, self.check_results_table, self.save_check_result_button):
            widget.setVisible(checking)
        self.check_mapping_table.setVisible(checking and self.check_mapping_toggle.isChecked())
        for widget in (self.trajectory_notice, self.load_button, self.trajectory_label, self.ordinary_summary):
            widget.setVisible(not checking)
        self.run_button.setText("开始标定检查…" if checking else "运行轨迹…")
        self.progress.setAccessibleName("标定检查进度" if checking else "轨迹播放进度")
        self.check_options_confirmed.setChecked(False)
        self.load_builtin_check_plan()
        self.refresh()

    def load_builtin_check_plan(self):
        self.check_plan = None
        self.check_plan_load_error = ""
        self.check_options_confirmed.setChecked(False)
        dof = self.dof.currentData()
        if dof not in (24, 28):
            self.check_plan_label.setText("请选择24或28电机机型，内置视频方案会自动载入，无需选择NPZ文件。")
            self.check_mapping_table.setRowCount(0)
            return
        try:
            from .calibration_check import builtin_video_plan

            self.check_plan = builtin_video_plan(dof)
            groups = self.check_plan["group_order"]
            self.check_plan_label.setText(
                f"已载入文档第10节单侧视频方案 · {dof}轴 / {len(groups)}组 · 每轴同侧参考容差±3°\n"
                + " → ".join(groups) + "\n视频为simulated动作预览；本次真机结果单独记录。"
                "按方案关节范围执行；起点角差只展示，不阻断检查，软件不验证实际到位或停稳。"
            )
        except (ImportError, ValueError, OSError, KeyError, TypeError) as error:
            self.check_plan_load_error = str(error)
            self.check_plan_label.setText("内置视频方案不可用：" + str(error))
        self.populate_check_mapping()

    def start_pose_report(self):
        """Compare fresh physical feedback after the same version-bound conversion as the runtime."""
        if self.check_plan is None or self.metadata is None:
            raise ValueError("等待已绑定机器人的新鲜角度反馈")
        tolerance = self.prepare_tolerance.value()
        if (self._start_pose_override is not None and time.monotonic() - self._start_pose_override_at <= 0.25
                and self._start_pose_override_tolerance_deg == tolerance):
            return self._start_pose_override
        if self.last_positions is None or time.monotonic() - self.last_feedback_at > 0.25:
            raise ValueError("角度反馈缺失或超过250ms")
        from .calibration_check import start_pose_report
        from .calibration_mapping import motor_to_logical, version_bound_mapping

        key = json.dumps((self.dof.currentData(), self.metadata["motors"], self.last_positions,
                          self.check_plan["joint_names"], self.check_plan["initial_pose_rad"], tolerance),
                         sort_keys=True, allow_nan=False)
        if key != self._pose_report_cache_key:
            try:
                mapping = version_bound_mapping(self.dof.currentData(), self.metadata["motors"])
                # Zero torque is only an unused math input, never a displayed reading.
                logical, _ = motor_to_logical(self.last_positions, [0.0] * self.dof.currentData(), mapping)
                report = start_pose_report(self.check_plan, logical, tolerance_deg=tolerance)
            except ValueError as error:
                self._pose_report_cache = None
                self._pose_report_cache_error = str(error)
            else:
                self._pose_report_cache, self._pose_report_cache_error = report, None
            self._pose_report_cache_key = key
        if self._pose_report_cache_error is not None:
            raise ValueError(self._pose_report_cache_error)
        return [dict(row) for row in self._pose_report_cache]

    @staticmethod
    def _set_cell(table, row, column, value):
        text = str(value)
        item = table.item(row, column)
        if item is None:
            table.setItem(row, column, QTableWidgetItem(text))
        elif item.text() != text:
            item.setText(text)

    def inspect_start_pose(self):
        self.populate_check_mapping()
        self.check_mapping_toggle.setChecked(True)

    def populate_check_mapping(self):
        mapping, error = None, ""
        if self.dof.currentData() in (24, 28):
            try:
                from .calibration_mapping import version_bound_mapping

                mapping = version_bound_mapping(self.dof.currentData(), self.metadata["motors"] if self.metadata else None)
            except (ImportError, ValueError, OSError, KeyError, TypeError) as failure:
                error = "角度映射无法核对：" + str(failure)
                try:
                    from .calibration_mapping import version_bound_mapping

                    mapping = version_bound_mapping(self.dof.currentData())
                except (ImportError, ValueError, OSError, KeyError, TypeError):
                    mapping = None
        rows = mapping.get("rows", []) if isinstance(mapping, dict) else []
        self.check_mapping_table.setRowCount(len(rows))
        if not rows:
            self.check_mapping_toggle.setText("关节—电机映射尚未载入（连接后核对）")
        else:
            self.check_mapping_toggle.setText("展开起始姿态与关节—电机映射")
        report = []
        if not error:
            try:
                report = self.start_pose_report()
            except (ImportError, ValueError, OSError, KeyError, TypeError) as failure:
                error = str(failure)
        by_name = {item["joint"]: item for item in report}
        if report:
            text = f"起始姿态：已读取{len(report)}/{len(report)}关节反馈，可查看当前角与视频开始角。"
        else:
            text = "起始姿态未知：" + (error or "等待新鲜角度反馈") + "。"
        self.check_start_pose_label.setText(text + " 角度偏差只展示，不阻断动作，未验证实际到位或停稳；不代表端点标零误差，起始姿态核对不发送运动指令。")
        for row, item in enumerate(rows):
            buses = item.get("busids", [])
            name = item.get("name", item.get("joint", "—"))
            start = "—"
            if self.check_plan and name in self.check_plan["joint_names"]:
                start = f"{math.degrees(self.check_plan['initial_pose_rad'][self.check_plan['joint_names'].index(name)]):+.2f}"
            result = by_name.get(name)
            actual = "—" if result is None else f"{math.degrees(result['actual_rad']):+.2f}"
            deviation = "—" if result is None else f"{result['deviation_deg']:+.2f}"
            status = "未知" if result is None else "已读取（仅展示）"
            cells = (name, " + ".join(map(str, buses)),
                     f"方向 {item.get('sign', '—')}" + (f" / {item['coupled_side']}踝成对解算" if item.get("coupled_side") else ""),
                     actual, start, deviation, status)
            for column, value in enumerate(cells):
                self._set_cell(self.check_mapping_table, row, column, value)

    def check_parameters_changed(self):
        if self.sender() is not self.max_torque or not self.is_check_mode():
            self.check_options_confirmed.setChecked(False)
        self.refresh()

    def open_check_preview(self):
        root = Path(__file__).resolve().parents[2]
        path = root / "runtime/mujoco-preview/single-side-shorter-20261030/single-side-both.mp4"
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.status.setText("本机未找到视频文件，请查看标定检查说明第10节的播放地址与文件路径。")

    def open_check_document(self):
        path = Path(__file__).resolve().parents[2] / "docs/JOINT_CALIBRATION_LIMIT_PLAN.md"
        if not path.is_file() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.status.setText("无法打开本机标定检查说明：" + str(path))

    def launch_context(self):
        connection = self.connection_context()
        panel = self.parent()
        if panel is not None:
            if getattr(panel, "calibration_dialog", None) is not None:
                raise ValueError("请先关闭标零流程，并确认独立标零终端和脚本已退出。")
            if getattr(panel, "calibration_terminal_opened", False):
                raise ValueError("独立标零终端可能仍在运行，请重新打开控制窗口并核对退出状态。")
            postzero = getattr(panel, "postzero_dialog", None)
            if (postzero is not None and postzero.worker is not None and postzero.worker.isRunning()
                    and postzero.mode.currentData() == "query"):
                raise ValueError("主动位置查询正在运行，请先停止查询；控制期间可使用被动监听。")
        if self._invalidated or _binding(connection) != self.initial_binding:
            raise ValueError("机器人或SSH绑定已改变，请重新打开控制窗口。")
        if not connection.get("connected") or not connection.get("robot_id") or not connection.get("target"):
            raise ValueError("请先在设备设置连接目标机器人。")
        if not connection.get("known_hosts") or not Path(connection["known_hosts"]).is_file():
            raise ValueError("请先核对并保存SSH主机指纹。")
        status = self.current_context()
        if (tuple(status.get(key) for key in ("robot_id", "source_kind", "stream_id")) != self.initial_identity
                or status.get("configuration_binding") != self.initial_configuration):
            raise ValueError("机器人采集会话或机型配置已改变，请重新打开控制窗口。")
        if status.get("source_kind") != "ros2" or status.get("robot_id") != connection.get("robot_id"):
            raise ValueError("仅允许控制当前绑定的真实机器人，模拟来源不能使能。")
        dof = self.dof.currentData()
        if dof not in (24, 28):
            raise ValueError("请选择24或28电机机型。")
        if status.get("configured_dof") not in (None, dof) or status.get("health_dof") not in (None, dof):
            raise ValueError("所选机型与当前机器人配置不一致。")
        return dict(connection)

    def refresh(self):
        active = self.worker is not None and self.worker.isRunning()
        try:
            self.launch_context()
            valid = True
        except (ValueError, OSError) as error:
            valid = False
            if active and not self._invalidated:
                self.invalidate(str(error))
            elif not active and self.state == "disconnected" and self.worker is None:
                self.status.setText(str(error))
        closing = active and self.worker.cancel_event.is_set()
        ready = active and valid and self.metadata is not None and not self._pending and not closing
        confirmed = self.site_ready.isChecked()
        self.connect_button.setEnabled(valid and not active and bool(self.password.text()))
        self.enable_button.setEnabled(ready and confirmed and self.state in ("ready", "disabled"))
        self.load_button.setEnabled(not active or (ready and self.state in ("ready", "disabled", "enabled")))
        fresh = self.last_positions is not None and time.monotonic() - self.last_feedback_at <= 1.0
        if self.is_check_mode():
            self.populate_check_mapping()
            check_ready = bool(self.metadata and self.metadata.get("calibration_check_ready") is True)
            parameters_fit, hint = self.candidate_parameter_fit()
            self.check_parameter_hint.setText(hint)
            self.run_button.setEnabled(ready and confirmed and self.state in ("enabled", "prepared") and self.check_plan is not None
                                       and check_ready and parameters_fit and self.check_options_confirmed.isChecked() and fresh)
            prepare_reasons = []
            if not active:
                prepare_reasons.append("请先连接控制会话")
            elif not valid or closing:
                prepare_reasons.append("连接绑定已变化或会话正在结束")
            elif self._pending:
                prepare_reasons.append("正在等待当前操作反馈")
            if active and self.state not in ("enabled", "prepared"):
                prepare_reasons.append("请先使能电机，或等待当前动作结束")
            if not confirmed:
                prepare_reasons.append("请先确认机器人支撑固定和现场条件")
            if self.check_plan is None or not check_ready:
                prepare_reasons.append("检查方案或控制端映射环境未就绪")
            if self.last_positions is None or time.monotonic() - self.last_feedback_at > 0.25:
                prepare_reasons.append("等待250ms内的新鲜角度反馈")
            try:
                self.preparation_options()
            except ValueError as error:
                prepare_reasons.append(str(error))
            self.prepare_check_button.setEnabled(ready and not prepare_reasons)
            prepare_message = ("复位不可用：" + "；".join(prepare_reasons) if prepare_reasons else
                               "可以按deploy固定4秒复位：无需输入速度或时限；"
                               "点击后核对本次增益和路径；本标定模式不设置统一力矩中止阈值，仍保留反馈、故障与停止路径。"
                               "无需先确认整套限位检查，不验证实际到位或停稳。")
            if self._failure_reason:
                prepare_message = "本次控制已中止：" + self._failure_reason
                if self._completion_summary:
                    prepare_message += "\n" + self._completion_summary
                prepare_message += "\n处理原因后，重新输入密码并连接控制会话；不会自动重连或恢复运动。"
            elif not active and self._completion_summary:
                prepare_message = self._completion_summary + "\n" + prepare_message
            self.prepare_check_hint.setText(prepare_message)
            self.prepare_check_button.setToolTip(prepare_message)
            if self.metadata and not check_ready:
                reasons = self.metadata.get("calibration_check_reasons")
                if not isinstance(reasons, list):
                    reasons = [self.metadata.get("calibration_check_reason") or "控制端未提供标定检查就绪证明"]
                self.check_readiness_label.setText("本次不能执行标定检查：" + "；".join(map(str, reasons)))
            elif self.metadata:
                self.check_readiness_label.setText("控制端标定检查环境已就绪；仍需现场核对映射、机械参考与本次参数。")
            else:
                self.check_readiness_label.setText("控制会话未就绪：连接后重新核对机器人映射环境；当前不能执行复位或检查。")
        else:
            self.run_button.setEnabled(ready and confirmed and self.state == "enabled" and self.trajectory is not None and fresh)
        self.continue_check_button.setEnabled(active and valid and confirmed and fresh and not closing
                                              and self.check_waiting_review and self.is_check_mode())
        self.save_check_result_button.setEnabled(self.check_result is not None)
        self.stop_button.setEnabled(active and not closing)
        self.disable_button.setEnabled(active and not closing)
        self.disconnect_button.setEnabled(active and not closing)
        self.password.setEnabled(not active)
        self.dof.setEnabled(not active)
        self.mode_combo.setEnabled(not active or (ready and self.state in ("ready", "disabled", "enabled")))
        self.site_ready.setEnabled(not active or self.state in ("ready", "disabled", "enabled"))
        editable = not self._confirming_parameters and (not active or (ready and self.state in ("ready", "disabled", "enabled", "prepared")))
        self.check_options_confirmed.setEnabled(editable)
        for widget in [*self.parameter_widgets, self.prepare_speed, self.prepare_timeout, self.prepare_tolerance]:
            widget.setEnabled(editable)
        if self._failure_reason:
            self.status.setText(self._failure_reason + ("\n" + self._completion_summary if self._completion_summary else ""))

    def model_changed(self):
        self.trajectory = None
        self.trajectory_label.setText("未载入轨迹 · 请按所选机型载入物理电机轨迹")
        self.check_result = None
        self.check_samples = []
        self.check_samples_omitted = 0
        self.check_waiting_review = False
        self.check_results_table.setRowCount(0)
        self.load_builtin_check_plan()
        self.check_options_confirmed.setChecked(False)
        self.site_ready.setChecked(False)
        self.refresh()

    def connect_session(self):
        self.refresh()
        if not self.connect_button.isEnabled():
            return
        connection = self.launch_context()
        if self.worker is not None:
            self.worker.deleteLater()
        self.session_id = str(uuid4())
        self.metadata = None
        self.check_waiting_review = False
        self.last_positions, self.last_feedback_at = None, 0.0
        self._start_pose_override, self._start_pose_override_at = None, 0.0
        self._preparation_confirmed_start = None
        self._check_start_tolerance_deg = None
        self.state, self._pending, self._completed = "connecting", True, False
        self._failure_reason, self._completion_summary = "", ""
        password = self.password.text()
        self.password.clear()
        self.worker = MotorControlWorker(connection, password, self.dof.currentData(), self.session_id, self)
        self.worker.received.connect(self.on_event)
        self.worker.failed.connect(self.on_error)
        self.worker.finished.connect(self.session_finished)
        self.status.setText("正在连接并核对配置… 尚未发送使能或运动指令。")
        self.table.setRowCount(0)
        self.progress.setValue(0)
        self.worker.start()
        self.refresh()

    def _confirm(self, title, text):
        if len(text) > 1800:
            # Complete group/axis targets must remain readable on smaller displays.
            dialog = QDialog(self)
            dialog.setObjectName("motorActionConfirmation")
            dialog.setWindowTitle(title)
            available = self.screen().availableGeometry()
            dialog.resize(min(900, available.width() - 40), min(720, available.height() - 56))
            layout = QVBoxLayout(dialog)
            details = QPlainTextEdit(text)
            details.setReadOnly(True)
            details.setAccessibleName("本次真机标定检查完整目标与参数")
            details.setStyleSheet("background:white; color:#263451; padding:12px; border:1px solid #bdcce4;")
            layout.addWidget(details, 1)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
            buttons.button(QDialogButtonBox.StandardButton.Yes).setText("确认执行")
            buttons.button(QDialogButtonBox.StandardButton.Yes).setAutoDefault(False)
            buttons.button(QDialogButtonBox.StandardButton.No).setText("取消")
            buttons.button(QDialogButtonBox.StandardButton.No).setDefault(True)
            buttons.button(QDialogButtonBox.StandardButton.No).setFocus()
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            return dialog.exec() == QDialog.DialogCode.Accepted
        return QMessageBox.question(
            self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def enable_motors(self):
        self.refresh()
        if not self.enable_button.isEnabled():
            return
        target = self.connection_context().get("target")
        if not self._confirm("确认使能电机", f"目标：{target}\n机型：{self.dof.currentData()}电机\n"
                             "将发送使能与Kp=0、Kd=0.5低阻尼指令，不支撑站立。\n"
                             "确认机器人已支撑固定、人员避让和急停可用后继续。"):
            return
        self.refresh()
        if self.enable_button.isEnabled():
            self.worker.commands.put({"command": "enable"})
            self._pending = True
            self.status.setText("已请求使能，等待控制程序反馈。")
            self.refresh()

    def load_trajectory(self):
        if not self.load_button.isEnabled():
            return
        dof = self.dof.currentData()
        if dof not in (24, 28):
            self.status.setText("请先选择24或28电机机型。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "载入物理电机轨迹", "", "电机轨迹 (*.npz *.json)")
        if not path:
            return
        if self.dof.currentData() != dof or not self.load_button.isEnabled():
            return
        try:
            from .motor_trajectory import load_trajectory

            trajectory = load_trajectory(path, dof)
        except (ValueError, OSError, KeyError, TypeError) as error:
            self.trajectory = None
            self.trajectory_label.setText("轨迹载入失败")
            self.status.setText("轨迹载入失败：" + str(error))
        else:
            self.trajectory = trajectory
            self.trajectory_label.setText(
                f"{Path(path).name} · {dof}电机 · {trajectory['frame_count']}帧 · {trajectory['duration_s']:.2f}s"
            )
            self.status.setText("轨迹已载入电脑。使能后点击运行，并核对本次参数。")
        self.refresh()

    def run_trajectory(self):
        if self.is_check_mode():
            self.run_calibration_check()
            return
        self.refresh()
        if not self.run_button.isEnabled():
            return
        if self.minimum.value() >= self.maximum.value():
            self.status.setText("允许位置下界必须小于上界。")
            return
        trajectory = self.trajectory
        command = {
            "command": "run", "trajectory": trajectory, "speed_deg_s": self.speed.value(),
            "position_min_deg": self.minimum.value(), "position_max_deg": self.maximum.value(),
            "kp": self.kp.value(), "kd": self.kd.value(), "max_torque_nm": self.max_torque.value(),
        }
        try:
            from .gentle_motion import SmoothMove
            from .motor_trajectory import prepare_run

            plan = prepare_run(trajectory, self.dof.currentData(), self.last_positions, command)
            expected = [f"{motor['interface']}/{motor['motor_id']}" for motor in self.metadata["motors"]]
            if plan["trajectory"].get("motors") not in (None, expected):
                raise ValueError("轨迹电机列顺序与当前CAN配置表不一致")
            approach = max(SmoothMove.bounded(
                current, first, speed_rad_s=math.radians(command["speed_deg_s"]),
                acceleration_rad_s2=math.radians(2), jerk_rad_s3=math.radians(5),
                maximum_stroke_rad=math.radians(3),
            ).duration_s for current, first in zip(self.last_positions, plan["trajectory"]["positions_rad"][0]))
        except (ValueError, TypeError, KeyError) as error:
            self.status.setText("运行前检查未通过：" + str(error))
            return
        if not self._confirm("确认真机运行轨迹", (
            f"目标：{self.connection_context().get('target')}\n机型：{self.dof.currentData()}电机\n"
            f"文件：{Path(trajectory['path']).name}\n{trajectory['frame_count']}帧 · 限速后轨迹{plan['duration_s']:.2f}s\n"
            f"首帧接近预计{approach:.2f}s · 本次合计预计{approach + plan['duration_s']:.2f}s\n"
            f"速度上限：{command['speed_deg_s']:g}°/s · 允许位置：{command['position_min_deg']:g}～{command['position_max_deg']:g}°\n"
            f"Kp / Kd：{command['kp']:g} / {command['kd']:g} · 停止力矩：{command['max_torque_nm']:g} N·m\n"
            "NPZ没有映射时，第N列按电机表第N行执行，不换算URDF关节角度。\n"
            "首帧差超过3°会拒绝运行；超出速度时整体降速，结束自动失能。确认开始真机运动？"
        )):
            return
        self.refresh()
        if self.run_button.isEnabled() and self.trajectory is trajectory:
            self.worker.commands.put(command)
            self._pending = True
            self.progress.setValue(0)
            self.status.setText("轨迹已提交，等待运行前反馈与范围检查。")
            self.refresh()

    def check_options(self):
        from .calibration_check import validate_check_options

        try:
            return validate_check_options(self.check_option_values())
        except ValueError as error:
            raise ValueError("检查参数：" + str(error)) from error

    def confirm_check_parameters(self, title, text):
        self._confirming_parameters = True
        self.refresh()
        try:
            return self._confirm(title, text)
        finally:
            self._confirming_parameters = False
            self.refresh()

    def preparation_options(self):
        from .calibration_check import validate_preparation_options
        from .preparation_gains import preparation_gain_profile

        options = validate_preparation_options({"reset_profile": "deploy_reset_4s", "require_pose_settling": False,
                                             "gain_profile": preparation_gain_profile(self.dof.currentData())["profile_id"],
                                             "kp": 50.0, "kd": 0.5,
                                             "abort_torque_nm": None})
        return {name: value for name, value in options.items()
                if name not in ("speed_deg_s", "preparation_timeout_s")}

    def check_option_values(self):
        return {
            "speed_deg_s": self.speed.value(), "search_speed_deg_s": self.search_speed.value(),
            "search_overtravel_deg": 1.0,
            "search_timeout_s": self.search_timeout.value(),
            "hold_s": 1.0, "position_span_deg": 0.2,
            "kp": self.kp.value(), "kd": self.kd.value(),
            "contact_torque_nm": self.contact_torque.value(), "torque_rise_nm": self.torque_rise.value(),
            "abort_torque_nm": None, "tolerance_deg": 3.0,
            "require_pose_settling": False,
        }

    def candidate_parameter_fit(self):
        try:
            options = self.check_options()
        except (ImportError, ValueError) as error:
            return False, str(error) + " 不会自动增加Kp或力矩。"
        return True, (f"候选参数建议：本次Kp={options['kp']:g}；低Kp可能无法达到接触阈值。"
                      "增益建议仅提示，不阻断运行；这不是实机可达性保证。接触默认0.5 N·m仍为未验证的软件候选，力矩升幅默认3 N·m用于接触判断；工厂参数待按每关节负载参考确定，不会自动增加Kp或力矩。")

    def check_confirmation_text(self, options):
        plan = self.check_plan
        targets = []
        for index, group in enumerate(plan["group_order"]):
            selected = {item["joint"] for item in plan["outcomes"] if item["group"] == group}
            targets.append(f"{index + 1}. {group}: " + "、".join(
                f"{item['joint']} {math.degrees(item['requested_rad']):+.2f}°（{item['direction']}）"
                for item in plan["outcomes"] if item["group"] == group
            ))
            segment = next(item for item in plan["segments"]
                           if item["group"] == group and item["phase"] == "各轴末端同步降速")
            auxiliary = [f"{name} {math.degrees(value):+.2f}°" for name, value in zip(plan["joint_names"], segment["end"])
                         if name not in selected and abs(value) > 1e-8]
            targets.append("配合轴非零目标：" + ("、".join(auxiliary) if auxiliary else "无") + "；其他配合轴为0°。")
        return (
            f"目标：{self.connection_context().get('target')}\n机型：{self.dof.currentData()}电机\n"
            f"视频同款：{len(plan['group_order'])}组，每轴检查一侧，同侧参考容差±3°。\n"
            f"转场≤{options['speed_deg_s']:g}°/s，近端点搜索≤{options['search_speed_deg_s']:g}°/s；"
            f"Kp / Kd：{options['kp']:g} / {options['kd']:g}\n"
            "开始检查前，使用2秒从复位deploy逐电机增益或低阻尼平滑切换到本次搜索Kp/Kd；"
            "新会话FF为0，本模式不更新积分；已有补偿仅冻结，不自动增力。"
            "交接期间保持原目标，2秒平滑切换后接续，不以角度偏差或停稳窗口阻断；未验证物理停稳。"
            "\n"
            f"候选接触 {options['contact_torque_nm']:g} N·m、力矩升幅 {options['torque_rise_nm']:g} N·m，用于相对搜索前负载的接触判断。"
            "本标定模式不设置统一力矩中止阈值，仍保留反馈、故障与停止路径；接触默认0.5 N·m仍为未验证的软件候选，工厂参数待按每关节负载参考确定。\n"
            f"接触检测使用实际反馈：连续{options['hold_s']:g}秒角度跨度≤{options['position_span_deg']:g}°，力矩持续达到接触值和相对负载升幅；不要求先运动0.2°。"
            "目标位置仍持续增加；闭链换算后的物理电机目标不再套固定20°/s中止门。\n"
            f"每组搜索最多{options['search_timeout_s']:g}秒；按所设低速持续推进至接触或本组超时，非无限推进。"
            "超时未碰到也停止搜索并记录未检出。\n"
            "低Kp可能达不到接触阈值；未检出保持未检出，不自动提高增益或力矩。\n"
            + self.candidate_parameter_fit()[1] + "\n"
            "以下角度是逻辑关节参考，经已核对的电机映射与踝部成对解算执行，不能当作普通NPZ电机角度。\n"
            "肩部约166°仍有大幅运动；非检查轴按视频避让姿态同步转场，并非都在零位，也非必须左右对称。\n"
            "同组任一候选阻挡会冻结整组目标、回退后暂停，现场核对后才能继续下一组。\n"
            "开始姿态及运行角差只作展示，不阻断动作；力矩反馈记录、反馈时效、心跳、故障、停止、指令编码与解算有效性条件仍有效。"
            "高力矩停滞仅记候选，不自动认定机械止挡或标定通过。\n\n"
            + "\n".join(targets) + "\n\n确认现场工装、包络、映射、参考端点及以上参数，开始真机标定检查？"
        )

    def run_calibration_check(self):
        self.refresh()
        if not self.is_check_mode() or not self.run_button.isEnabled():
            return
        if not self.start_pose_ready():
            return
        try:
            options = self.check_options()
            text = self.check_confirmation_text(options)
        except (ImportError, ValueError, TypeError, KeyError) as error:
            self.status.setText("标定检查参数未通过：" + str(error))
            return
        plan = self.check_plan
        if not self.confirm_check_parameters("确认真机标定检查", text):
            return
        self.refresh()
        if not self.run_button.isEnabled() or self.check_plan is not plan:
            return
        if not self.start_pose_ready():
            return
        try:
            if self.check_options() != options:
                raise ValueError("确认期间参数发生改变，请重新核对")
        except (ValueError, TypeError, KeyError) as error:
            self.status.setText("标定检查参数未通过：" + str(error))
            return
        self.check_result = None
        self.check_samples = []
        self.check_samples_omitted = 0
        self.check_waiting_review = False
        self.check_results_table.setRowCount(0)
        self.worker.commands.put({"command": "check", "plan": plan, "options": options,
                                  "reference_confirmed": True, "mapping_confirmed": True,
                                  "gain_handoff_confirmed": True})
        self._check_start_tolerance_deg = options["start_tolerance_deg"]
        self._pending = True
        self.progress.setValue(0)
        self.status.setText("标定检查已提交，等待控制端核对实时反馈、映射与指令有效性。")
        self.refresh()

    def start_pose_ready(self):
        try:
            self.start_pose_report()
        except (ImportError, ValueError, OSError, KeyError, TypeError) as error:
            self.status.setText("未开始运动：起始姿态无法检查，" + str(error) + "。")
        else:
            return True
        self.inspect_start_pose()
        return False

    def prepare_calibration_check(self):
        self.refresh()
        if not self.is_check_mode() or not self.prepare_check_button.isEnabled():
            return
        try:
            rows = self.start_pose_report()
            options = self.preparation_options()
            from .calibration_check import PreparationMove
            from .preparation_gains import preparation_gain_profile

            preparation = PreparationMove(self.check_plan, options, [item["actual_rad"] for item in rows], now_s=0.0)
            profile = preparation_gain_profile(self.dof.currentData())
            gain_rows = [
                f"{motor['interface']}/{motor['motor_id']}："
                f"复位Kp {profile['kp'][motor['index']] / 2.5:g} / Kd {profile['kd'][motor['index']]:g}，"
                f"结束恢复Kp {profile['kp'][motor['index']]:g}"
                for motor in self.metadata["motors"]
            ]
        except (ImportError, ValueError, OSError, KeyError, TypeError) as error:
            self.status.setText("尚未复位：起始姿态预检未通过，" + str(error) + "。")
            self.inspect_start_pose()
            return
        plan = self.check_plan
        text = (
            f"目标：{self.connection_context().get('target')}\n机型：{self.dof.currentData()}电机\n"
            f"按deploy逻辑复位固定{preparation.duration_s:g}秒三次平滑插值，无需输入速度或时限。"
            "本标定模式不设置统一力矩中止阈值，仍保留反馈、故障与停止路径。\n"
            f"复位/保持增益配置：{profile['profile_id']}；robot.yaml SHA256：{profile['robot_yaml_sha256']}。\n"
            "逐物理电机Kp=deployKp/2.5、Kd=deployKd；结束恢复完整deployKp，不额外增加2秒渐增。"
            "与本次搜索Kp/Kd分开，不自动提高搜索Kp。\n"
            "本次复位FF为0，不更新积分，也不自动增力。\n"
            "目标仍为视频检查起点，不调用ROS服务；控制端核对闭链解算与指令有效性，不把固定4秒复位显示成实际到位。\n"
            "本模式不以角度偏差或停稳窗口阻断复位、保持、交接及检查；力矩反馈记录、反馈时效、心跳、故障、停止、指令编码与解算有效性条件仍有效。"
            "确认后使用最新新鲜反馈建立复位轨迹，不按确认前后的角差拒绝。\n"
            "将从当前新鲜角度平滑转到视频检查起点，所有逻辑轴经已核对映射及踝部成对解算执行。\n"
            "这段复位会运动；开始前需现场确认完整路径的工装、人员和机器人部位均有足够空间。"
            "软件指令校验不能保证任意姿态下无碰撞。\n"
            "复位目标已发送并保持后，未验证实际到位；等待你另行点击开始标定检查，不会自动接着检查。\n"
            "物理电机增益（CAN / ID，按deploy物理顺序）：\n"
            + "\n".join(gain_rows) + "\n\n"
            "下表为当前角 / 视频开始角 / 准备偏差：\n\n"
            + "\n".join(
                f"{item['joint']}：{math.degrees(item['actual_rad']):+.2f}° / "
                f"{math.degrees(item['target_rad']):+.2f}° / {item['deviation_deg']:+.2f}°"
                for item in rows
            ) + "\n\n确认现场完整复位路径，执行固定4秒复位？"
        )
        if not self.confirm_check_parameters("确认固定4秒复位到检查起点", text):
            return
        self.refresh()
        if not self.prepare_check_button.isEnabled() or self.check_plan is not plan:
            return
        try:
            current_rows = self.start_pose_report()
            current_options = self.preparation_options()
            if current_options != options:
                raise ValueError("确认期间参数改变，请重新确认")
        except (ImportError, ValueError, OSError, KeyError, TypeError) as error:
            self.status.setText("尚未复位：" + str(error) + "。")
            return
        self.worker.commands.put({"command": "prepare_check", "plan": plan, "options": options,
                                  "reference_confirmed": True, "mapping_confirmed": True,
                                  "preparation_confirmed": True,
                                  "confirmed_start_rad": [item["actual_rad"] for item in current_rows]})
        self._preparation_confirmed_start = [item["actual_rad"] for item in current_rows]
        self.state, self._pending = "preparing", True
        self.progress.setValue(0)
        self.status.setText("固定4秒复位已提交，等待控制端核对实时反馈、映射、指令有效性与力矩；可随时停止并失能。")
        self.refresh()

    def continue_check(self):
        self.refresh()
        if not self.continue_check_button.isEnabled():
            return
        if not self._confirm("现场核对后继续", "本组已进入暂停核对阶段。\n"
                             "请现场核对物理运动、回退情况、接触位置、工装与下一组运动包络；软件未验证实际停稳。"
                             "高力矩停滞可能来自碰撞或卡滞。\n确认允许从当前姿态接续下一组？"):
            return
        self.refresh()
        if self.continue_check_button.isEnabled():
            self.worker.commands.put({"command": "continue_check", "confirmed": True})
            self.check_waiting_review = False
            self.status.setText("已提交现场继续确认，等待控制端核对后接续。")
            self.refresh()

    def save_check_result(self):
        if self.check_result is None:
            return
        result = self.check_result
        path, _ = QFileDialog.getSaveFileName(self, "保存本次标定检查", "calibration-check.json", "JSON (*.json)")
        if not path or self.check_result is not result:
            return
        try:
            saved = dict(result, samples=self.check_samples, samples_omitted=self.check_samples_omitted)
            Path(path).write_text(json.dumps(saved, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        except (OSError, ValueError, TypeError) as error:
            self.status.setText("保存检查结果失败：" + str(error))
        else:
            self.status.setText("本次候选检查结果已保存到电脑：" + path)

    def stop_and_disable(self):
        if self.worker is None or not self.worker.isRunning():
            return
        # Cancellation bypasses queued trajectories and is forwarded as priority stop.
        self.worker.cancel_event.set()
        self.check_waiting_review = False
        self._pending = True
        self.state = "stopping"
        self.status.setText("已请求停止并失能，等待控制程序返回；请现场确认电机已停止。")
        self.refresh()

    def disable_motors(self):
        if self.worker is None or not self.worker.isRunning() or self.worker.cancel_event.is_set():
            return
        self.worker.commands.put({"command": "disable"})
        self.check_waiting_review = False
        self._pending = True
        self.state = "stopping"
        self.status.setText("已请求失能，控制会话保留；请现场确认电机状态。")
        self.refresh()

    def disconnect_session(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self._pending = True
            self.status.setText("正在结束控制会话并请求失能… 请现场确认电机已停止。")
            self.refresh()

    def invalidate(self, reason="连接已断开，正在结束电机控制会话。"):
        self._invalidated = True
        self.check_waiting_review = False
        self.site_ready.setChecked(False)
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
        self.status.setText(reason + " 已请求失能；机器人实际停止状态请现场确认。")

    def on_event(self, event):
        if event.get("session_id") != self.session_id:
            return
        kind = event.get("type")
        if kind == "metadata":
            if self.worker:
                self.worker.password = ""
            motors = event.get("motors")
            valid = (event.get("dof") == self.dof.currentData()
                     and event.get("position_basis") == "motor_output_sdk_rad"
                     and isinstance(motors, list) and len(motors) == self.dof.currentData()
                     and all(isinstance(motor, dict) for motor in motors)
                     and [motor.get("index") for motor in motors] == list(range(len(motors)))
                     and bool(event.get("configuration_sha256")))
            if not valid:
                self.invalidate("控制端电机清单或坐标版本不匹配。")
                return
            self.metadata = event
            self.populate_check_mapping()
            self.table.setRowCount(len(motors))
            for row, motor in enumerate(motors):
                for column, value in enumerate((f"{row:02d} · {motor.get('interface', '—')} / {motor.get('motor_id', '—')}", "—", "—", "未收到")):
                    self._set_cell(self.table, row, column, value)
        elif kind == "state":
            state = event.get("state")
            if state not in ("ready", "enabled", "preparing", "prepared", "running", "disabled", "stopping", "fault"):
                self.invalidate("控制端返回了未知状态。")
                return
            self.state, self._pending = state, state in ("running", "preparing", "stopping")
            messages = {
                "ready": "控制会话已连接，尚未使能。请确认现场条件后点击使能。",
                "enabled": "低阻尼已使能，当前位置未锁定；可选择模式并确认运行。",
                "preparing": "正在准备复位或检查交接；可随时停止并失能。",
                "prepared": "复位目标已发送并保持，未验证实际到位。请现场核对后另行点击开始检查；切换模式前先失能。",
                "running": "正在运行轨迹。可随时停止并失能。",
                "disabled": "控制程序已向SDK提交失能指令；物理停止状态请现场确认。",
                "stopping": "正在停止轨迹并请求失能，请现场确认。",
                "fault": "控制程序报告故障，已请求失能；请现场核对电机状态。",
            }
            self.status.setText(messages[state] + (" " + event["message"] if event.get("message") else ""))
            if state == "fault" and not self._failure_reason:
                self._failure_reason = event.get("message") or messages[state]
            if state in ("disabled", "fault", "stopping"):
                self.check_waiting_review = False
        elif kind == "enable_diagnostics":
            motors = event.get("motors")
            statuses = {"fresh": "反馈正常", "missing": "未收到", "stale": "反馈过期",
                        "disabled": "未返回使能位", "fault": "故障"}
            expected = self.metadata.get("motors", []) if self.metadata else []
            if (not isinstance(motors, list) or len(motors) != len(expected) or not expected
                    or any(not isinstance(item, dict) or item.get("index") != row
                           or item.get("interface") != expected[row].get("interface")
                           or item.get("motor_id") != expected[row].get("motor_id")
                           or item.get("status") not in statuses for row, item in enumerate(motors))):
                self.on_error("控制端使能反馈诊断与当前电机清单不一致。")
                return
            self.last_positions, self.last_feedback_at = None, 0.0
            for row, motor in enumerate(motors):
                label = statuses[motor["status"]]
                if motor["status"] == "fault" and type(motor.get("fault")) is int:
                    label += f"0x{motor['fault']:x}"
                self._set_cell(self.table, row, 1, "—")
                self._set_cell(self.table, row, 2, "—")
                self._set_cell(self.table, row, 3, label)
            message = event.get("message")
            summary = f"使能反馈：{event.get('received_count', 0)} / {event.get('expected_count', len(expected))}台收到新鲜反馈。"
            self.status.setText(summary + (" " + message if isinstance(message, str) else ""))
        elif kind == "feedback":
            positions, torques = event.get("positions_rad"), event.get("torques_nm")
            count = self.dof.currentData()
            if not self.metadata or not self._numbers(positions, count) or (torques is not None and not self._numbers(torques, count, missing=True)):
                self.invalidate("控制端反馈缺失或数值无效。")
                return
            for row, position in enumerate(positions):
                self._set_cell(self.table, row, 1, f"{math.degrees(position):+.2f}")
                torque = torques[row] if torques is not None else None
                self._set_cell(self.table, row, 2, "—" if torque is None else f"{torque:+.2f}")
                self._set_cell(self.table, row, 3, "反馈正常" if event.get("enabled") is True else "未返回使能位")
            self.last_positions = list(positions)
            self.last_feedback_at = time.monotonic()
            self._start_pose_override, self._start_pose_override_at = None, 0.0
            progress = event.get("progress")
            if isinstance(progress, (int, float)) and math.isfinite(progress):
                self.progress.setValue(round(max(0, min(1, progress)) * 1000))
        elif kind == "command_rejected":
            rows = event.get("rows")
            names = self.check_plan["joint_names"] if self.check_plan else []
            if event.get("command") == "prepare_check" and event.get("code") == "preparation_start_changed":
                shown = self._preparation_confirmed_start
                valid = (event.get("state") in ("enabled", "prepared") and self.state == "preparing"
                         and event.get("tolerance_deg") == 0.5 and isinstance(rows, list) and len(rows) == len(names)
                         and self._numbers(shown, len(names)) and bool(names)
                         and isinstance(event.get("message"), str)
                         and all(isinstance(item, dict) and item.get("joint") == name
                                 and self._numbers([item.get(key) for key in ("actual_rad", "target_rad", "deviation_deg")], 3)
                                 and type(item.get("within_tolerance")) is bool
                                 and abs(item["deviation_deg"] - math.degrees(item["actual_rad"] - item["target_rad"])) < 1e-6
                                 and item["within_tolerance"] == (abs(item["actual_rad"] - item["target_rad"]) <= math.radians(0.5) + 1e-12)
                                 and abs(item["target_rad"] - shown[index]) < 1e-9
                                 for index, (item, name) in enumerate(zip(rows, names)))
                         and any(not item["within_tolerance"] for item in rows))
                if not valid:
                    self.on_error("控制端复位确认姿态核对结果不完整或与本次确认不一致。")
                    return
                self.state, self._pending = event["state"], False
                self._start_pose_override, self._start_pose_override_at = None, 0.0
                self.status.setText(event["message"] or "尚未复位：确认后起始姿态变化超过0.5°，请重新确认。")
                self.inspect_start_pose()
                self.refresh()
                return
            tolerance = (self._check_start_tolerance_deg if self._pending and self._check_start_tolerance_deg is not None
                         else self.prepare_tolerance.value())
            valid = (event.get("command") == "check" and event.get("code") == "start_pose_mismatch"
                     and type(event.get("tolerance_deg")) in (int, float)
                     and event.get("state") in ("enabled", "prepared") and event.get("tolerance_deg") == tolerance
                     and event.get("state") == self.state and isinstance(rows, list) and len(rows) == len(names)
                     and all(isinstance(item, dict) and item.get("joint") == name
                             and self._numbers([item.get(key) for key in ("actual_rad", "target_rad", "deviation_deg")], 3)
                             and type(item.get("within_tolerance")) is bool
                             and abs(item["deviation_deg"] - math.degrees(item["actual_rad"] - item["target_rad"])) < 1e-6
                             and item["within_tolerance"] == (abs(item["actual_rad"] - item["target_rad"]) <= math.radians(tolerance) + 1e-12)
                             and abs(item["target_rad"] - self.check_plan["initial_pose_rad"][index]) < 1e-9
                             for index, (item, name) in enumerate(zip(rows, names)))
                     and any(not item["within_tolerance"] for item in rows))
            if not valid:
                self.on_error("控制端起始姿态预检结果不完整或与当前方案不一致。")
                return
            self._pending = False
            self._start_pose_override = [dict(item) for item in rows]
            self._start_pose_override_at = time.monotonic()
            self._start_pose_override_tolerance_deg = tolerance
            self.status.setText(f"未开始运动：控制端核对到起始姿态偏差超过{tolerance:g}°。"
                                "控制会话保留，请查看完整起始姿态表；不会自动转场。")
            self.inspect_start_pose()
        elif kind == "prepare_state":
            phase, progress = event.get("phase"), event.get("progress")
            if (phase not in ("gain_ramp", "preparing", "confirm_stop", "prepared")
                    or type(progress) not in (int, float) or not math.isfinite(progress) or not 0 <= progress <= 1
                    or not isinstance(event.get("message", ""), str)):
                self.on_error("控制端返回了无效的复位状态。")
                return
            self.state = "prepared" if phase == "prepared" else "preparing"
            self._pending = phase != "prepared"
            phases = {"gain_ramp": "正在渐增复位及保持增益", "preparing": "正在发送复位目标", "confirm_stop": "复位目标已发送并保持，未验证实际到位",
                      "prepared": "复位目标已发送并保持，未验证实际到位，等待另行开始检查"}
            self.status.setText(phases[phase] + ("。" + event["message"] if event.get("message") else "。"))
            self.progress.setValue(round(progress * 1000))
        elif kind == "check_state":
            phase = event.get("phase")
            if phase not in ("approach", "search", "confirm_stop", "retreat", "confirm_retreat_stop", "confirm_return_stop",
                             "waiting_review", "transition",
                             "return", "complete", "aborted"):
                self.on_error("控制端返回了未知标定检查阶段。")
                return
            self.check_waiting_review = phase == "waiting_review" and event.get("waiting_review") is True
            self.state, self._pending = "running", True
            names = {"approach": "接近本组参考姿态", "search": "低速搜索与角度/力矩检测", "confirm_stop": "整组目标已冻结，未验证物理停稳",
                     "retreat": "本组缓退", "confirm_retreat_stop": "回退目标已发送，未验证物理停稳", "confirm_return_stop": "返回目标已发送，未验证物理停稳",
                     "waiting_review": "等待现场核对后继续", "transition": "转到下一组",
                     "return": "缓慢返回初始工装姿态", "complete": "本次检查结束", "aborted": "检查已中止"}
            self.status.setText("标定检查 · " + names[phase] + " · " + str(event.get("group") or "")
                                + ("\n" + str(event["message"]) if event.get("message") else ""))
            progress = event.get("progress")
            if type(progress) in (int, float) and math.isfinite(progress):
                self.progress.setValue(round(max(0, min(1, progress)) * 1000))
        elif kind == "check_result":
            result = event.get("result")
            if (not isinstance(result, dict) or result.get("source_kind") != "hardware"
                    or result.get("candidate_only") is not True or result.get("physical_stop_confirmed") is not False
                    or "calibration_passed" not in result or result["calibration_passed"] is not None):
                self.on_error("标定检查结果的来源或候选状态无效，未保存为通过。")
                return
            rows = result.get("outcomes", result.get("rows"))
            if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("joint"), str) for row in rows):
                self.on_error("标定检查结果缺少逐关节候选记录。")
                return
            try:
                # Snapshot the received record rather than building an apparent pass from UI state.
                self.check_result = json.loads(json.dumps(result, allow_nan=False))
            except (TypeError, ValueError):
                self.on_error("标定检查结果包含无效数值。")
                return
            self.check_results_table.setRowCount(len(rows))
            statuses = {"candidate_obstruction": "候选阻挡（待核对）", "unexpected_obstruction": "异常阻挡",
                        "not_covered": "本组未覆盖", "endpoint_not_detected": "到搜索上限仍未检出"}
            for row, item in enumerate(rows):
                def angle(value, radians=False):
                    if type(value) not in (int, float) or not math.isfinite(value):
                        return "—"
                    return f"{math.degrees(value) if radians else value:+.2f}"

                compared = item.get("within_tolerance")
                cells = (item["joint"], angle(item.get("expected_rad"), True), angle(item.get("measured_rad"), True),
                         angle(item.get("deviation_deg")),
                         "容差内（候选）" if compared is True else "超出容差（候选）" if compared is False else "未比较",
                         statuses.get(item.get("status"), item.get("status", "未覆盖")))
                for column, value in enumerate(cells):
                    self.check_results_table.setItem(row, column, QTableWidgetItem(str(value)))
            if result.get("phase") in ("complete", "aborted"):
                self.check_waiting_review = False
            if self.check_waiting_review:
                self.status.setText("本组候选端点记录已返回，等待现场核对后继续下一组；可保存当前记录，不自动判定整机标定通过。")
            else:
                self.status.setText("本次候选端点记录已返回，可保存到电脑；未确认物理止挡，不自动判定整机标定通过。"
                                    + ("\n中止原因：" + str(result["abort_reason"]) if result.get("abort_reason") else ""))
        elif kind == "check_sample":
            count = self.dof.currentData()
            if (event.get("source_kind") != "hardware" or not self._numbers(event.get("positions_rad"), count)
                    or not self._numbers(event.get("torques_nm"), count)
                    or not self._numbers(event.get("targets_rad"), count)
                    or type(event.get("time_s")) not in (int, float) or not math.isfinite(event["time_s"])
                    or not isinstance(event.get("phase"), str) or not isinstance(event.get("group"), str)):
                self.on_error("标定检查曲线采样缺失或数值无效。")
                return
            if len(self.check_samples) < 20000:
                self.check_samples.append({key: event[key] for key in (
                    "source_kind", "time_s", "positions_rad", "torques_nm", "targets_rad", "phase", "group",
                )})
            else:
                self.check_samples_omitted += 1
        elif kind == "error":
            self.on_error(event.get("message") or "控制程序报告错误。")
        elif kind == "complete":
            self._completed = True
            self.state, self._pending = "disconnected", False
            self.check_waiting_review = False
            sent = "已向SDK提交失能指令" if event.get("disable_sent") is True else "未确认失能指令已提交"
            self._completion_summary = f"控制会话结束 · {sent}；物理停止状态请现场确认。"
            reason = event.get("reason")
            if isinstance(reason, str) and reason and not self._failure_reason:
                self._completion_summary += " " + reason
            self.status.setText(self._completion_summary)
        self.refresh()

    @staticmethod
    def _numbers(values, count, missing=False):
        return isinstance(values, list) and len(values) == count and all(
            (missing and value is None) or (type(value) in (int, float) and math.isfinite(value))
            for value in values
        )

    def on_error(self, message):
        if not self._failure_reason:
            self._failure_reason = message
        self.state, self._pending = "fault", False
        self.check_waiting_review = False
        self.status.setText(self._failure_reason)
        if self.worker is not None:
            self.worker.stop()
        self.refresh()

    def session_finished(self):
        if not self._completed and not self._invalidated:
            self._completion_summary = "控制会话已断开，未收到完整结束记录。请现场确认失能和停止状态。"
            self.status.setText(self._completion_summary)
        self.state, self._pending = "disconnected", False
        self.metadata = None
        self.last_positions, self.last_feedback_at = None, 0.0
        self.refresh()
        if self._close_requested:
            self.password.clear()
            self.timer.stop()
            super().reject()

    def shutdown(self):
        self.password.clear()
        if self.worker is not None and self.worker.isRunning():
            self.disconnect_session()
            return self.worker.wait(1500)
        return True

    def reject(self):
        self._close_requested = True
        self.password.clear()
        if self.shutdown():
            self.timer.stop()
            super().reject()

    def closeEvent(self, event):
        self._close_requested = True
        self.password.clear()
        if self.shutdown():
            self.timer.stop()
            event.accept()
        else:
            event.ignore()
