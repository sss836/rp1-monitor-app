"""Explicit manual endpoint inspection; no connection or motor action on open."""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtCore import QLocale, Qt, QTimer, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .desktop_angle_check import AngleObservations, EncoderAngleCheck
from .desktop_motor_control import MotorControlDialog, MotorControlWorker, _binding
from .desktop_urdf_guide import UrdfJointGuide
from .manual_calibration_check import MAX_ENCODER_FRAME_GAP_S, validate_report

_LABELS = {'hip': '髋', 'knee': '膝', 'ankle': '踝', 'waist': '腰',
           'shoulder': '肩', 'elbow': '肘', 'wrist': '腕'}
_STATUSES = {'pending': '未测', 'active': '检查中', 'passed': '合格', 'failed': '不合格（本轮保留）',
             'skipped': '已跳过', 'interrupted': '已中断'}
_PHASES = {'ready': '等待手动摆动', 'collecting': '正在采集', 'paused': '已暂停',
           'failed': '本关节不合格，暂停后可重测', 'complete': '本轮检查结束'}
_NO_CURRENT_ANGLE_COLUMN = object()


def _label(text=''):
    value = QLabel(text)
    value.setWordWrap(True)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return value


def joint_title(name):
    side = '左' if name.startswith('left_') else '右' if name.startswith('right_') else ''
    part = next((title for token, title in _LABELS.items() if token in name), '')
    axis = '前后摆 / pitch' if 'pitch' in name else '侧摆 / roll' if 'roll' in name else '旋转 / yaw' if 'yaw' in name else '屈伸'
    return f'{side}{part} · {axis}' if part else name


def _number(value, suffix='', signed=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        return '未知'
    return format(value, '+.2f' if signed else '.2f') + suffix


class ManualCalibrationDialog(QDialog):
    check_started = Signal(object)
    check_saved = Signal(object)
    launch_context = MotorControlDialog.launch_context

    def __init__(self, calibration_context, connection_context, parent=None, *, directory=None, observed_pose=None):
        super().__init__(parent)
        self.current_context, self.connection_context = calibration_context, connection_context
        initial = calibration_context()
        self.initial_binding = _binding(connection_context())
        self.initial_identity = tuple(initial.get(k) for k in ('robot_id', 'source_kind', 'stream_id'))
        self.initial_configuration = initial.get('configuration_binding')
        self.angles = AngleObservations(observed_pose, self.initial_identity) if observed_pose else None
        self.angle_sn = ''
        self.angle_view = False
        self.angle_index = 0
        self.encoder_query = None
        self.auto_check = None
        self.auto_mode = False
        self.auto_query = None
        self.auto_sent_at = None
        self._skip_target = None
        self._unreached_target = None
        self._angle_save_path = None
        self._angle_report_pending = None
        self.auto_latest = None
        self.auto_last_stamp = None
        self.auto_error = ''
        self.worker = self.session_id = self.metadata = None
        self.state = 'disconnected'
        self._invalidated = self._close_requested = self._completed = False
        self._pending = self._awaiting_manual = self._manual_live = False
        self._pin_confirmation_pending = False
        self._last_report_at = 0.0
        self._report_configuration_digest = None
        self._failure_reason = self._terminal_reason = ''
        self._enabled_kd = None
        self._configuration_digest = None
        self.run_id = self.report = self.last_event = self.last_saved_path = None
        self._save_at = 0.0
        self._save_signature = None
        self._factory_saved_signature = None
        self.save_error = ''
        default = Path(os.environ.get('RP1_FACTORY_DIR') or Path.home() / '.local/share/rp1-monitor/factory') / 'calibration-checks'
        self.directory = Path(directory or os.environ.get('RP1_MANUAL_CHECK_DIR') or default)
        self.setWindowTitle('标定检查')
        self.setObjectName('manualCalibrationDialog')
        self.setStyleSheet('''QDialog#manualCalibrationDialog {background:#eef2fa;color:#263451;}
            QLabel,QCheckBox {color:#263451;font-size:14px;background:transparent;}
            QLineEdit,QComboBox,QDoubleSpinBox,QTableWidget {background:white;color:#263451;border:1px solid #bdcce4;padding:6px;}
            QPushButton {background:white;color:#355886;border:1px solid #bdcce4;border-radius:7px;padding:9px 12px;min-height:24px;}
            QPushButton:disabled {color:#79869b;background:#e3e9f2;}
            QPushButton:focus,QLineEdit:focus,QComboBox:focus,QDoubleSpinBox:focus {border:2px solid #4165a0;}
            QPushButton#manualStop {background:#a2363b;color:white;font-weight:700;}
            QHeaderView::section {background:#e6edf8;color:#263451;padding:7px;}
        ''')
        area = self.screen().availableGeometry()
        self.resize(min(1120, area.width()-32), min(880, area.height()-48))
        self.setMinimumSize(700, 540)
        outer = QVBoxLayout(self)
        title = _label('标定检查')
        title.setStyleSheet('font-size:23px;font-weight:700;')
        outer.addWidget(title)
        outer.addWidget(_label(f"{connection_context().get('robot_id') or '未绑定机器人'} · 按引导检查，自动记录结果"))
        self.error_banner = _label()
        self.error_banner.setStyleSheet('background:#fff0f0;color:#982e35;padding:9px;border:1px solid #dfb5b9;')
        self.error_banner.hide()
        outer.addWidget(self.error_banner)
        self.save_error_banner = _label()
        self.save_error_banner.setStyleSheet('background:#fff0f0;color:#982e35;padding:9px;border:1px solid #dfb5b9;')
        self.save_error_banner.hide()
        outer.addWidget(self.save_error_banner)
        self.readiness = _label()
        self.readiness.setAccessibleName('逐关节检查就绪状态')
        self.readiness.setStyleSheet('background:white;padding:9px;border:1px solid #bdcce4;')
        outer.addWidget(self.readiness)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        self.setup_toggle = QPushButton('连接与参数（点击展开/收起）')
        self.setup_toggle.setCheckable(True)
        self.setup_toggle.setChecked(True)
        self.setup_panel = QWidget()
        setup_layout = QVBoxLayout(self.setup_panel)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.setup_toggle)
        layout.addWidget(self.setup_panel)
        self.setup_toggle.toggled.connect(self.setup_panel.setVisible)
        setup_layout.addWidget(_label('先支撑固定机器人并退出其他运控或标零工具。连接成功后可逐项检查角度。需要电机辅助时，请先核对映射并确认现场条件。'))
        bar = QHBoxLayout()
        self.dof = QComboBox()
        self.dof.addItem('请选择机型', None)
        self.dof.addItem('24 电机', 24)
        self.dof.addItem('28 电机', 28)
        if initial.get('configured_dof') in (24, 28):
            self.dof.setCurrentIndex(self.dof.findData(initial['configured_dof']))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('本次 SSH / sudo 密码，不保存')
        self.password.setAccessibleName('本次连接密码')
        self.connect_button = QPushButton('连接并核对')
        self.disconnect_button = QPushButton('断开连接')
        bar.addWidget(self.dof)
        bar.addWidget(self.password, 1)
        bar.addWidget(self.connect_button)
        bar.addWidget(self.disconnect_button)
        setup_layout.addLayout(bar)
        params = QHBoxLayout()
        form = QFormLayout()
        self.damping = QDoubleSpinBox()
        self.damping.setRange(0, .5)
        self.damping.setDecimals(2)
        self.damping.setSingleStep(.05)
        self.damping.setValue(0)
        self.hold_s = QDoubleSpinBox()
        self.hold_s.setRange(.3, 5)
        self.hold_s.setValue(1)
        self.hold_s.setSuffix(' s')
        self.position_span = QDoubleSpinBox()
        self.position_span.setRange(.02, .5)
        self.position_span.setDecimals(2)
        self.position_span.setValue(.2)
        self.position_span.setSuffix(' °')
        form.addRow('本次阻尼 Kd（默认无阻尼）', self.damping)
        form.addRow('稳定持续时间', self.hold_s)
        form.addRow('稳定窗口最大位置跨度', self.position_span)
        params.addLayout(form)
        params.addWidget(_label('Kp=0，前馈=0；Kd使能前确定，本次保持不变。\n改变Kd需先失能后明确重新使能。\n腰部和髋 pitch 零位±2°、5秒；其他关节固定容差±3°。'), 1)
        setup_layout.addLayout(params)
        self.site_ready = QCheckBox('现场已支撑固定、急停可用、其他运控已退出；已核对关节映射与表中参考')
        setup_layout.addWidget(self.site_ready)
        self.reference_ready = self.site_ready
        actions = QHBoxLayout()
        self.enable_button = QPushButton('使能 · Kd 0.00')
        self.disable_button = QPushButton('失能（保留连接）')
        self.start_button = QPushButton('开始人工逐关节检查')
        self.pause_button = QPushButton('暂停检查')
        self.retest_button = QPushButton('重测选中关节（保留历史）')
        self.skip_button = QPushButton('跳过当前关节，记未完成')
        self.unreached_button = QPushButton('记录未达目标并继续')
        self.unreached_button.setToolTip('确认已经尝试摆到目标角度。记录本次最接近目标的实测值为不合格。')
        self.unreached_button.hide()
        self.continue_failed_button = QPushButton('记录不合格并继续')
        self.continue_failed_button.hide()
        self.pin_confirm_button = QPushButton('销钉已插好，开始检查')
        self.pin_confirm_button.hide()
        for button in (self.enable_button, self.disable_button, self.start_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        review_actions = QHBoxLayout()
        for button in (self.pause_button, self.retest_button, self.skip_button, self.unreached_button,
                       self.continue_failed_button):
            review_actions.addWidget(button)
        review_actions.addWidget(self.pin_confirm_button)
        layout.addLayout(review_actions)
        details = QHBoxLayout()
        self.guide = UrdfJointGuide()
        details.addWidget(self.guide, 1)
        measurement = QVBoxLayout()
        self.current_joint = _label()
        self.current_joint.setStyleSheet('font-size:20px;font-weight:700;')
        self.reference = _label()
        self.values = _label('等待本次实测。动画仅为 simulated 示意。')
        self.conditions = _label()
        self.conditions.setStyleSheet('background:white;padding:12px;border:1px solid #bdcce4;')
        for widget in (self.current_joint, self.reference, self.values, self.conditions):
            measurement.addWidget(widget)
        self.powered_measurement_note = _label('关节角度用于判定，力矩仅辅助记录；结果自动保存在电脑。')
        measurement.addWidget(self.powered_measurement_note)
        self.angle_mode_button = QPushButton('单次读取与人工记录')
        self.angle_mode_button.clicked.connect(self.toggle_angle_mode)
        self.angle_mode_button.hide()
        measurement.addWidget(self.angle_mode_button)
        self.angle_panel = QWidget()
        angle_layout = QVBoxLayout(self.angle_panel)
        angle_layout.setContentsMargins(0, 0, 0, 0)
        self.angle_input = QLineEdit()
        self.angle_input.setAccessibleName('人工实测角度')
        validator = QDoubleValidator(-360, 360, 3, self.angle_input)
        locale = QLocale.c()
        locale.setNumberOptions(QLocale.NumberOption.RejectGroupSeparator)
        validator.setLocale(locale)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.angle_input.setValidator(validator)
        self.angle_input.setPlaceholderText('填写量角工具实测值')
        angle_form = QFormLayout()
        angle_form.addRow('人工实测 °', self.angle_input)
        encoder_controls = QHBoxLayout()
        self.encoder_motor = QComboBox()
        self.encoder_motor.setAccessibleName('选择电机总线和 ID')
        self.encoder_read = QPushButton('读取轴角')
        self.encoder_read.setToolTip('查询所选电机的编码器位置')
        encoder_controls.addWidget(self.encoder_motor, 1)
        encoder_controls.addWidget(self.encoder_read)
        angle_form.addRow('电机轴角', encoder_controls)
        self.encoder_value = _label('选择总线和 ID 后读取。')
        angle_form.addRow('', self.encoder_value)
        angle_layout.addLayout(angle_form)
        angle_actions = QHBoxLayout()
        self.angle_previous = QPushButton('上一项')
        self.angle_next = QPushButton('下一项')
        self.angle_record = QPushButton('记录角度并继续')
        for button in (self.angle_previous, self.angle_next, self.angle_record):
            angle_actions.addWidget(button)
        angle_layout.addLayout(angle_actions)
        self.angle_panel.hide()
        measurement.addWidget(self.angle_panel)
        measurement.addStretch()
        details.addLayout(measurement, 2)
        layout.addLayout(details)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['关节 / 所选单侧', '参考 °', '实测峰值 °', '偏差 °', '结果', '轮次'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(200)
        layout.addWidget(self.table)
        self.powered_review_note = _label('腰部侧摆先插好销钉并确认；腰部和左右髋 pitch 在零位±2°内保持5秒。其他关节检查所选单侧±3°。暂停后可重测，历史保留。')
        layout.addWidget(self.powered_review_note)
        self.diagnostics = _label()
        layout.addWidget(self.diagnostics)
        self.save_status = _label('收到检查报告后自动保存到本机；拔线不删除已收记录。')
        layout.addWidget(self.save_status)
        save_actions = QHBoxLayout()
        self.save_to_results_button = QPushButton('保存到 SYS-006 查看结果')
        self.save_to_results_button.hide()
        save_actions.addWidget(self.save_to_results_button)
        self.export_button = QPushButton('另存本轮 JSON')
        save_actions.addWidget(self.export_button)
        layout.addLayout(save_actions)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.status = _label('尚未连接；未发送电机指令。')
        outer.addWidget(self.status)
        self.stop_button = QPushButton('停止并失能')
        self.stop_button.setObjectName('manualStop')
        self.stop_button.setMinimumHeight(44)
        outer.addWidget(self.stop_button)
        self.connect_button.clicked.connect(self.connect_session)
        self.disconnect_button.clicked.connect(self.disconnect_session)
        self.enable_button.clicked.connect(self.enable_motors)
        self.disable_button.clicked.connect(self.disable_motors)
        self.start_button.clicked.connect(self.start_check)
        self.pause_button.clicked.connect(self.pause_check)
        self.retest_button.clicked.connect(self.retest_selected)
        self.skip_button.pressed.connect(self.remember_skip_target)
        self.skip_button.clicked.connect(self.skip_current)
        self.unreached_button.pressed.connect(self.remember_unreached_target)
        self.unreached_button.clicked.connect(self.record_unreached_angle)
        self.continue_failed_button.clicked.connect(self.continue_failed_angle)
        self.pin_confirm_button.clicked.connect(self.confirm_pin)
        self.stop_button.clicked.connect(self.stop_and_disable)
        self.save_to_results_button.clicked.connect(self.save_angles_to_results)
        self.export_button.clicked.connect(self.export_report)
        self.angle_input.textChanged.connect(self.refresh)
        self.angle_previous.clicked.connect(lambda: self.move_angle(-1))
        self.angle_next.clicked.connect(lambda: self.move_angle(1))
        self.angle_record.clicked.connect(self.capture_angle)
        self.encoder_motor.currentIndexChanged.connect(self.reset_encoder_display)
        self.encoder_read.clicked.connect(self.read_encoder)
        self.dof.currentIndexChanged.connect(self.load_plan)
        for signal in (self.password.textChanged, self.site_ready.toggled, self.damping.valueChanged):
            signal.connect(self.refresh)
        self.site_ready.toggled.connect(self.angle_site_changed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self.poll_automatic_angle)
        self.auto_timer.setInterval(300)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.load_plan()

    def load_plan(self, *_):
        from .manual_check_plan import builtin_manual_plan

        if self.dof.currentData() not in (24, 28):
            self.plan = None
            self.table.setRowCount(0)
            self.current_joint.setText('先选择24或28电机机型')
            self.reference.setText('机型未明确，尚未加载检查参考。')
            self.guide.set_plan(None)
            self.guide.set_outcome(None)
            self.refresh()
            return
        self.plan = builtin_manual_plan(self.dof.currentData())
        self.guide.set_plan(self.plan)
        if self.report is None:
            self.table.setRowCount(len(self.plan['outcomes']))
            for index, row in enumerate(self.plan['outcomes']):
                self.set_row(index, row, 'pending', None, 0)
            self.show_current(None, 0)
        self.refresh()

    def set_row(self, index, row, status, attempt, count, *, current_angle=_NO_CURRENT_ANGLE_COLUMN, pending_record=False):
        peak = (attempt or {}).get('peak_angle_deg')
        ref = row.get('reference_deg', math.degrees(row.get('requested_rad', 0)))
        delta = peak - ref if type(peak) in (int, float) else None
        side = {'lower': '下限', 'upper': '上限', 'center': '插销零位' if row.get('check_mode') == 'pin_zero' else '零位'}.get(row['direction'], '未知')
        angles = (_number(peak) + ('（待记）' if pending_record else ''), _number(delta, signed=True))
        if current_angle is not _NO_CURRENT_ANGLE_COLUMN:
            angles = (_number(current_angle), *angles)
        values = (joint_title(row['joint']) + ' · ' + side, _number(ref), *angles, _STATUSES.get(status, status), str(count))
        for col, value in enumerate(values):
            item = self.table.item(index, col)
            if item is None:
                item = QTableWidgetItem(value)
                self.table.setItem(index, col, item)
            elif item.text() != value:
                item.setText(value)
            if item.toolTip() != row['joint']:
                item.setToolTip(row['joint'])

    def show_current(self, current, index):
        row = self.plan['outcomes'][index] if type(index) is int and 0 <= index < len(self.plan['outcomes']) else None
        if row is None:
            self.current_joint.setText('本轮检查结束')
            self.reference.setText('腰部和髋 pitch 检查零位；其余仅覆盖所选单侧。查看各项结果，未测不算合格。')
            self.guide.set_outcome(None)
            self.values.setText('本轮所选单侧结果见下表；跳过或未测不算合格。')
            self.conditions.setText('单侧检查结束不代表双侧全行程标定合格。')
            return
        self.guide.set_outcome(row)
        self.current_joint.setText(f'{index+1}/{len(self.plan["outcomes"])} · {joint_title(row["joint"])}')
        ref = math.degrees(row['requested_rad'])
        if row.get('check_mode') in {'pin_zero', 'center_zero'}:
            pin = row.get('check_mode') == 'pin_zero'
            self.reference.setText(('腰部侧摆 / roll\n插好定位销钉，参考 0°，允许 ±2°。\n点击“销钉已插好，开始检查”，连续保持至少5秒。' if pin else '髋前后摆 / pitch\n将当前关节摆到 0° ±2°，连续保持至少5秒，自动判定。'))
            self.reference.setToolTip('插销是否就位由操作人员确认；不执行自动运动或零位写入。' if pin else '仅按实时角度检查零位，不写入零位或自动运动。')
            current = current or {}
            self.values.setText('当前角 ' + _number(current.get('current_angle_deg'), '°', True))
            confirmed = current.get('pin_confirmed_at_s') is not None
            flags = current.get('conditions') or {}
            in_range = '范围内' if flags.get('in_range') else '范围外或未知'
            self.conditions.setText(('销钉：' + ('已由操作人员确认' if confirmed else '等待操作人员确认') + '\n' if pin else '')
                                    + '角度：' + in_range + '（−2° 至 +2°）'
                                    + '\n连续保持：' + _number(current.get('stable_elapsed_s'), ' s') + ' / 5.00 s'
                                    + '\n超出范围或反馈不连续后重新计时；连接中断时检查会停止。')
            return
        direction = '逻辑角减小 → 下限' if row['direction'] == 'lower' else '逻辑角增大 → 上限' if row['direction'] == 'upper' else '检查方向未知，不能猜测'
        side_names = {'lower': '下限', 'upper': '上限'}
        zero_side = ('居中标零' if 'center' in str(row.get('check_side_basis', ''))
                     else side_names.get(row.get('zero_direction'), '未知'))
        check_side = side_names.get(row.get('direction'), '未知')
        zero_source = row.get('zero_source') or self.plan.get('zero_source') or '未知，须现场核对'
        self.reference.setText(f'{row["joint"]}\n{direction} · 参考 {ref:+.2f}°，允许 ±3°\n标零侧：{zero_side} · 本次检查侧：{check_side}\n参考：模型限位；只核查所选单侧。')
        self.reference.setToolTip(str(zero_source) + '\n标零姿态：' + str(row.get('zero_pose_deg'))
                                  + '°；参考推导误差：' + str(row.get('zero_reference_error_deg')) + '°')
        current = current or {}
        self.values.setText('当前角 ' + _number(current.get('current_angle_deg'), '°', True)
                            + '　峰值 ' + _number(current.get('peak_angle_deg'), '°', True)
                            + '\n偏差 ' + _number(None if current.get('peak_angle_deg') is None else current['peak_angle_deg']-ref, '°', True)
                            + '　窗口跨度 ' + _number(current.get('span_deg'), '°')
                            + '\n力矩 ' + _number(current.get('torque_nm'), ' Nm')
                            + '　基线 ' + _number(current.get('baseline_nm'), ' Nm')
                            + '　增量 ' + _number(current.get('rise_nm'), ' Nm'))
        flags = current.get('conditions') or {}
        def mark(key):
            return '满足' if flags.get(key) is True else '未满足' if flags.get(key) is False else '未知'
        self.conditions.setText('角度容差：' + mark('in_range') + '\n稳定窗口：' + mark('stable')
                                + '（已持续 ' + _number(current.get('stable_elapsed_s'), ' s') + '）'
                                + '\n外侧越界：' + ('未越界' if flags.get('no_overshoot') is True else '越界/未知')
                                + '\n力矩仅辅助，缺失或不升不阻止角度与稳定性合格。')

    def active(self):
        return self.worker is not None and self.worker.isRunning()

    def angle_ready(self):
        return (self.angles is not None and self.active() and self.metadata is not None
                and self.state == 'ready' and self.plan is not None
                and not (self._failure_reason or self._pending or self._completed
                         or self.worker.cancel_event.is_set()))

    def show_angle_joint(self):
        row = self.plan['outcomes'][self.angle_index]
        self.guide.set_outcome(row)
        self.current_joint.setText(f'{self.angle_index + 1}/{len(self.plan["outcomes"])} · {joint_title(row["joint"])}')
        side = {'lower': '下限', 'upper': '上限', 'center': '零位'}[row['direction']]
        self.reference.setText(f'{row["joint"]}\n{side}参考 {math.degrees(row["requested_rad"]):+.2f}°')
        self.reference.setToolTip('')
        self.angle_input.clear()

    def move_angle(self, step):
        self.angle_index = max(0, min(self.angle_index + step, len(self.plan['outcomes']) - 1))
        self.show_angle_joint()
        self.refresh()

    def set_six_check_columns(self, labels):
        if self.table.columnCount() != 6:
            self.table.setColumnCount(6)
            for column in (1, 3):
                self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            for column in (2, 3, 4):
                self.table.horizontalHeaderItem(column).setToolTip('')
        self.table.setHorizontalHeaderLabels(labels)

    def show_angle_rows(self):
        count = len(self.plan['outcomes'])
        self.set_six_check_columns(['关节 / 所选单侧', '参考 °', '实测 °', '偏差 °', '记录', '次数'])
        self.table.setRowCount(count)
        recorded = {}
        for measurement in self.angles.records:
            joint = measurement['joint']
            recorded[joint] = (measurement, recorded.get(joint, (None, 0))[1] + 1)
        for index, row in enumerate(self.plan['outcomes']):
            latest, attempts = recorded.get(row['joint'], (None, 0))
            self.set_row(index, row, '已记录角度' if latest else 'pending',
                         {'peak_angle_deg': self.angles.degrees(latest)} if latest else None, attempts)
        self.progress.setRange(0, count)
        self.progress.setValue(len(recorded))
        self.progress.setFormat(f'{len(recorded)}/{count} 已记录角度')

    def refresh_angle(self, available):
        if self.auto_mode:
            if self.auto_latest is not None and time.monotonic() - self.auto_latest[2] > MAX_ENCODER_FRAME_GAP_S:
                self.clear_automatic_feedback('等待新的编码器反馈')
            return
        row = self.plan['outcomes'][self.angle_index]
        measurement = self.angles.reading(self.dof.currentData(), row['joint']) if available else None
        value = self.angles.degrees(measurement) if measurement else None
        self.values.setText(f'采集端角度 {_number(value, "°", True)}'
                            + (f' · 偏差 {value - math.degrees(row["requested_rad"]):+.2f}°' if measurement
                               else ' · 可单独读取下方电机轴角'))
        self.angle_input.setEnabled(available)
        self.angle_previous.setEnabled(self.angle_index > 0)
        has_next = self.angle_index + 1 < len(self.plan['outcomes'])
        self.angle_next.setEnabled(has_next)
        self.angle_record.setText('记录角度并继续' if has_next else '记录角度')
        manual = self.angle_input.text().strip()
        self.angle_record.setEnabled(available and (
            self.angle_input.hasAcceptableInput() if manual else measurement is not None))
        self.encoder_motor.setEnabled(available and self.encoder_query is None and self.auto_query is None)
        self.encoder_read.setEnabled(available and self.encoder_query is None and self.auto_query is None
                                     and self.encoder_motor.currentData() is not None)

    def toggle_angle_mode(self):
        self.auto_mode = not self.auto_mode
        self.angle_panel.setVisible(not self.auto_mode)
        self.angle_mode_button.setText('单次读取与人工记录' if self.auto_mode else '继续自动角度检查')
        self.auto_check.clear_site_confirmation()
        self.clear_automatic_feedback('', discard_inflight=True)
        if self.auto_mode:
            self.auto_timer.start()
            self.poll_automatic_angle()
        else:
            self.auto_timer.stop()
            self.conditions.setText('可读取电机轴角，或录入量角工具的实测角度。')
            self.show_angle_rows()
            self.show_angle_joint()
        self.refresh()

    def poll_automatic_angle(self):
        if not self.auto_mode or not self.angle_ready() or self.auto_query or self.encoder_query:
            return
        report = self.auto_check.report()
        index = report['current_index']
        if index is None or report['paused']:
            return
        joint = self.plan['outcomes'][index]['joint']
        request_id = str(uuid4())
        try:
            if self.send({'command': 'read_joint_encoders', 'joint': joint, 'request_id': request_id}):
                self.auto_query = (request_id, joint)
                self.auto_sent_at = time.monotonic()
        except ValueError as error:
            self.invalidate(str(error))

    def angle_site_changed(self, checked):
        if self.auto_check is not None and not checked:
            self.auto_check.clear_site_confirmation()
            self.clear_automatic_feedback('', discard_inflight=True)

    def clear_automatic_feedback(self, reason, *, discard_inflight=False):
        if discard_inflight:
            # Drain the reply before sending another request, but drop its measurement.
            self.auto_sent_at = None
        self.auto_latest = None
        self.auto_error = reason
        if self.auto_check is not None:
            self.auto_check.invalidate()
        if self.auto_mode:
            self.render_automatic_angle()

    def receive_automatic_angle(self, event):
        if self.auto_query != (event['request_id'], event['joint']):
            return
        sent_at = self.auto_sent_at
        self.auto_query = self.auto_sent_at = None
        if sent_at is None or not self.auto_mode or not self.angle_ready():
            return
        try:
            self.launch_context()
        except ValueError as error:
            self.invalidate(str(error))
            return
        if time.monotonic() - sent_at > .65:
            self.clear_automatic_feedback('反馈已过期，等待新的编码器读数。')
            return
        before = self.auto_check.report()
        index = before['current_index']
        if index is None or self.plan['outcomes'][index]['joint'] != event['joint'] or before['paused']:
            return
        if event['position_rad'] is None:
            self.clear_automatic_feedback(event['error'])
            return
        samples = event['samples']
        stamp = min(sample['rx_monotonic_ns'] for sample in samples)
        if self.auto_last_stamp is not None and stamp <= self.auto_last_stamp:
            self.clear_automatic_feedback('反馈时间没有更新，等待新样本。')
            return
        self.auto_last_stamp = stamp
        self.auto_latest = (event['joint'], event['position_rad'], time.monotonic())
        self.auto_error = ''
        if self.site_ready.isChecked():
            report = self.auto_check.feed(event['joint'], event['position_rad'], stamp, {'samples': samples})
            if report['current_index'] != index:
                self.auto_latest = None
                self.status.setText(f'{joint_title(event["joint"])}角度达标，继续检查。'
                                    if report['current_index'] is not None else '本轮角度检查已完成。')
                if report['current_index'] is None:
                    self.persist_automatic_angles()
        else:
            self.auto_check.invalidate()
        self.render_automatic_angle()

    def render_automatic_angle(self):
        report = self.auto_check.report()
        current, index = report['current'], report['current_index']
        failed = report['phase'] == 'failed'
        current_angle = None
        if not failed and current and self.auto_latest and self.auto_latest[0] == current['joint']:
            current_angle = math.degrees(self.auto_latest[1])
        pending_angle = self.auto_check.engine.target_not_reached_angle()
        count = len(report['results'])
        if self.table.columnCount() != 7:
            self.table.setColumnCount(7)
            self.table.setHorizontalHeaderLabels(['关节 / 所选单侧', '参考 °', '当前角度 °', '记录角度 °',
                                                  '记录偏差 °', '角度检查', '次数'])
            for column in (1, 3, 6):
                self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            self.table.horizontalHeaderItem(2).setToolTip('当前关节最新有效的编码器读数。')
            self.table.horizontalHeaderItem(3).setToolTip('达标或超限时采用的实测角度。待记为本次最接近目标的角度，确认未达目标后保存。')
            self.table.horizontalHeaderItem(4).setToolTip('记录角度减去参考角度。')
        self.table.setRowCount(count)
        for row in report['results']:
            attempts = row['attempts']
            attempt = attempts[-1] if attempts else None
            is_current = row['index'] == index
            pending_record = is_current and pending_angle is not None
            recorded = pending_angle if pending_record else None
            if row['status'] == 'failed':
                recorded = attempt['peak_angle_deg']
            elif row['status'] == 'passed':
                recorded = attempt['current_angle_deg']
            label = {'passed': '角度达标', 'failed': '角度超限'}.get(row['status'], row['status'])
            if row['status'] == 'failed' and 'failure_confirmation' in attempt:
                label = '未达目标' if attempt.get('reason') == 'operator_target_not_reached' else '不合格'
            self.set_row(row['index'], row, label, {'peak_angle_deg': recorded}, len(attempts),
                         current_angle=current_angle if is_current else None, pending_record=pending_record)
        done = sum(row['status'] == 'passed' for row in report['results'])
        self.progress.setRange(0, count)
        self.progress.setValue(done)
        self.progress.setFormat(f'{done}/{count} 角度达标')
        if index is None:
            self.show_current(None, None)
            failed_count = sum(row['status'] == 'failed' for row in report['results'])
            skipped_count = sum(row['status'] == 'skipped' for row in report['results'])
            self.conditions.setText(f'本轮已结束。达标 {done} 项，不合格 {failed_count} 项，跳过 {skipped_count} 项。')
            return
        if self.angle_index != index or not self.current_joint.text().startswith(f'{index + 1}/'):
            self.angle_index = index
            self.show_angle_joint()
        row = self.plan['outcomes'][index]
        ref = math.degrees(row['requested_rad'])
        angle = current['peak_angle_deg'] if failed else current_angle
        label = '超限时实测角度' if failed else '当前角度'
        self.values.setText(f'{label} {_number(angle, "°", True)}'
                            + (f' · 偏差 {angle - ref:+.2f}°' if angle is not None else ''))
        if failed:
            note = '本项角度超限。可记录不合格并继续，或选中该关节重测。'
        elif self.auto_error:
            note = self.auto_error
        elif not self.site_ready.isChecked():
            note = '正在刷新角度。请在「连接与参数」核对现场条件和关节对应关系，随后自动判定。'
        elif row.get('pin_confirm_required') and (current or {}).get('pin_confirmed_at_s') is None:
            note = '插好腰部定位销后，点击「销钉已插好，开始检查」。'
        elif report['paused']:
            note = '角度检查已暂停。'
        else:
            hold = row.get('pin_hold_s') or report['options']['hold_s']
            note = f'编码器角度按当前 deploy 配置换算 · 连续稳定 {(current or {}).get("stable_elapsed_s", 0):.2f} / {hold:.2f} 秒'
            if pending_angle is not None:
                note += f' · 本次最接近目标 {pending_angle:+.2f}°'
        self.conditions.setText(note)

    def reset_encoder_display(self):
        self.encoder_value.setToolTip('')
        self.encoder_value.setText('选择总线和 ID 后读取。' if self.encoder_motor.currentData() is None else '等待读取。')
        self.refresh()

    def populate_encoder_motors(self):
        selected = self.encoder_motor.currentData()
        self.encoder_motor.blockSignals(True)
        self.encoder_motor.clear()
        self.encoder_motor.addItem('选择总线 / ID', None)
        for motor in self.metadata['motors']:
            self.encoder_motor.addItem(f'{motor["interface"]} / ID {motor["motor_id"]}', motor['index'])
        self.encoder_motor.setCurrentIndex(max(0, self.encoder_motor.findData(selected)))
        self.encoder_motor.blockSignals(False)
        self.reset_encoder_display()

    def read_encoder(self):
        self.refresh()
        if not self.angle_view or self.auto_mode or not self.encoder_read.isEnabled():
            return
        index = self.encoder_motor.currentData()
        request_id = str(uuid4())
        if self.send({'command': 'read_encoder', 'index': index, 'request_id': request_id}):
            self.encoder_query = (request_id, index)
            self.encoder_value.setText('正在读取编码器…')
            self.refresh()

    def receive_encoder(self, event):
        if (not self.angle_view or self.metadata is None
                or self.encoder_query != (event['request_id'], event['index'])):
            return
        self.encoder_query = None
        motor = self.metadata['motors'][event['index']]
        if (event['interface'], event['motor_id']) != (motor['interface'], motor['motor_id']):
            self.encoder_value.setText('返回的总线或电机 ID 与所选地址不符。')
            return
        if event['status'] == 'fresh':
            evidence = {key: value for key, value in event.items() if key not in ('type', 'source_kind', 'session_id')}
            self.angles.encoder_samples.append({'source': 'lro_position_query', **evidence})
            received = datetime.fromisoformat(event['received_at'].replace('Z', '+00:00')).astimezone()
            self.encoder_value.setText(f'实测轴角 {event["angle_deg"]:+.2f}° · {received:%H:%M:%S}')
            self.encoder_value.setToolTip(event['received_at'])
        else:
            reason = event.get('message') or {'timeout': '电机未应答', 'fault': '电机返回异常状态'}.get(
                event['status'], '未收到有效位置')
            self.encoder_value.setText(f'轴角未知 · {reason}')

    def capture_angle(self):
        self.refresh()
        if self.auto_mode or not self.angle_record.isEnabled():
            return
        row = self.plan['outcomes'][self.angle_index]
        manual = self.angle_input.text().strip()
        measurement = ({'source': 'manual_angle_gauge', 'position_deg': float(manual)} if manual
                       else self.angles.reading(self.dof.currentData(), row['joint']))
        if measurement is None:
            self.refresh()
            return
        record = self.angles.capture(row, measurement)
        self.show_angle_rows()
        self.status.setText(f'已记录{joint_title(row["joint"])}，偏差 {record["difference_deg"]:+.2f}°。')
        self.move_angle(1)

    def refresh(self, *_):
        active = self.active()
        connection_error = ''
        try:
            self.launch_context()
            valid = True
        except ValueError as error:
            valid = False
            connection_error = str(error)
            if active and not self._invalidated:
                self.invalidate(connection_error)
        metadata = self.metadata or {}
        mapping_ready = metadata.get('calibration_check_ready') is True
        angle_ready = valid and self.angle_ready()
        mapping_reason = (metadata.get('calibration_check_reason') or '未收到关节映射核对结果。') if not mapping_ready else ''
        enabled = active and self.state == 'enabled' and not self._pending
        manual_active = self._manual_live and self.report is not None
        interrupted = bool(self.report and self.report.get('interruption_reason'))
        self.connect_button.setEnabled(valid and not active and bool(self.password.text()))
        self.disconnect_button.setEnabled(active and self.state != 'stopping')
        self.enable_button.setEnabled(valid and active and mapping_ready and self.state in ('ready', 'disabled')
                                      and self.site_ready.isChecked() and not (self._pending or self.angle_view))
        self.enable_button.setToolTip(mapping_reason)
        self.enable_button.setText(f'使能 · Kd {self.damping.value():.2f}')
        self.disable_button.setEnabled(enabled)
        self.start_button.setEnabled((angle_ready and not self.angle_view) or (
            valid and enabled and self.site_ready.isChecked() and self.reference_ready.isChecked() and mapping_ready
            and (not manual_active or self.report.get('phase') == 'complete') and not self._awaiting_manual))
        next_step = ''
        if not self._failure_reason and not self._completed:
            if connection_error:
                next_step = connection_error
            elif not active:
                next_step = '点击“连接并核对”，自动检查机器人版本和关节映射。'
            elif self.state == 'connecting' or self.metadata is None:
                next_step = '正在核对机器人版本和关节映射。'
            elif angle_ready:
                next_step = '' if self.angle_view else '连接核对完成。点击“开始人工逐关节检查”查看并记录角度。'
            elif not mapping_ready:
                next_step = f'电机使能待核对。{mapping_reason}'
            elif self.enable_button.isEnabled():
                next_step = '版本核对通过。点击“使能”。'
            elif self.state in ('ready', 'disabled') and not self._pending:
                next_step = '版本核对通过。请在“连接与参数”中确认现场条件。'
            elif self.start_button.isEnabled():
                next_step = '已使能。点击“开始人工逐关节检查”。'
        if self.readiness.text() != next_step:
            self.readiness.setText(next_step)
        self.readiness.setVisible(bool(next_step))
        for button in (self.enable_button, self.disable_button, self.start_button):
            button.setVisible(not self.angle_view)
        for button in (self.pause_button, self.retest_button):
            button.setVisible(self.auto_mode or not self.angle_view)
        auto_failed = False
        endpoint = can_record_unreached = False
        if self.auto_mode:
            scan = self.auto_check.report()
            current = scan['current'] or {}
            current_open = scan['current_index'] is not None
            paused = scan['paused']
            auto_failed = scan['phase'] == 'failed'
            endpoint = current.get('status') == 'active' and current.get('direction') in ('lower', 'upper')
            fresh = (self.auto_latest is not None and self.auto_latest[0] == current.get('joint')
                     and time.monotonic() - self.auto_latest[2] <= MAX_ENCODER_FRAME_GAP_S)
            can_record_unreached = (endpoint and angle_ready and self.site_ready.isChecked() and fresh
                                    and self.auto_check.can_mark_target_not_reached())
            self.pause_button.setEnabled(angle_ready and current_open and not auto_failed)
            self.skip_button.setEnabled(angle_ready and current_open and not auto_failed)
            self.retest_button.setEnabled(angle_ready and (paused or scan['phase'] == 'complete'))
            pin_pending = (current.get('pin_confirm_required') is True
                           and current.get('pin_confirmed_at_s') is None)
            self.pin_confirm_button.setVisible(pin_pending)
            self.pin_confirm_button.setEnabled(angle_ready and self.site_ready.isChecked() and pin_pending
                                               and not paused and not self.auto_check.pin_confirmation_pending)
        else:
            report = self.report or {}
            paused = report.get('paused') is True
            manual_ready = valid and enabled and manual_active and not interrupted
            phase = report.get('phase')
            self.pause_button.setEnabled(manual_ready and phase not in ('complete', 'failed'))
            self.retest_button.setEnabled(manual_ready and (paused or phase == 'complete'))
            self.skip_button.setEnabled(manual_ready and report.get('current_index') is not None)
            current = report.get('current') or {}
            pin_active = current.get('check_mode') == 'pin_zero' and current.get('status') == 'active'
            self.pin_confirm_button.setVisible(pin_active)
            self.pin_confirm_button.setEnabled(manual_ready and pin_active and not paused
                                               and current.get('pin_confirmed_at_s') is None
                                               and not self._pin_confirmation_pending)
        self.skip_button.setVisible(not self.angle_view or (self.auto_mode and not auto_failed))
        self.unreached_button.setVisible(endpoint)
        self.unreached_button.setEnabled(can_record_unreached)
        self.continue_failed_button.setVisible(auto_failed)
        self.continue_failed_button.setEnabled(angle_ready and auto_failed)
        self.pause_button.setText('继续人工检查' if paused else '暂停检查')
        self.stop_button.setEnabled(active)
        self.stop_button.setText('结束检查并断开' if self.angle_view else '停止并失能')
        self.export_button.setText('导出角度记录…' if self.angle_view else '另存本轮 JSON')
        automatic = self.auto_check if self.angle_view else None
        self.save_to_results_button.setVisible(automatic is not None)
        self.save_to_results_button.setEnabled(bool(self.angle_sn and automatic and automatic.evidence_count
                                                   and not self._angle_report_pending))
        self.save_to_results_button.setToolTip(f'归档到本轮开始时选定的 SN：{self.angle_sn}' if self.angle_sn else
                                             '从整机测试选好 SN 后进入 SYS-006 标定检查。')
        self.export_button.setEnabled(bool(self.angles.records or self.angles.encoder_samples
                                          or (automatic and automatic.evidence_count))
                                      if self.angle_view else self.report is not None)
        if self.angle_view:
            self.refresh_angle(angle_ready)
        self.damping.setEnabled(not self._pending and (not active or self.state in ('ready', 'disabled')))
        self.dof.setEnabled(not active and self.report is None and automatic is None
                            and not (self.angles and self.angles.records))
        editable = ((not manual_active or self.report.get('phase') == 'complete')
                    and not self._awaiting_manual and not (self.angle_view and self.auto_check is not None))
        self.hold_s.setEnabled(editable)
        self.position_span.setEnabled(editable)
        self.setup_toggle.setText(f'连接与参数 · Kd {self.damping.value():.2f} · 稳定 {self.hold_s.value():g}s / {self.position_span.value():g}°（点击展开/收起）')
        if self.report is not None and not self.angle_view and not self._manual_live and not self.current_joint.text().startswith('上轮记录'):
            self.current_joint.setText('上轮记录 · ' + self.current_joint.text())
        if active and manual_active and not self._failure_reason and time.monotonic()-self._last_report_at > 1:
            self.status.setText('电脑已超过1秒未收到新报告，当前为最后记录；不推断继续合格。')
        if self._failure_reason:
            self.error_banner.setText(self._failure_reason)
            self.error_banner.show()

    def connect_session(self):
        self.refresh()
        if not self.connect_button.isEnabled():
            return
        connection = self.launch_context()
        self.encoder_query = None
        self.auto_query = None
        if self.angle_view:
            self.angle_view = False
            self.auto_mode = False
            self.auto_timer.stop()
            self.angle_mode_button.hide()
            self.angle_panel.hide()
            self.powered_measurement_note.show()
            self.powered_review_note.show()
            self.set_six_check_columns(['关节 / 所选单侧', '参考 °', '实测峰值 °', '偏差 °', '结果', '轮次'])
            if self.report is None:
                self.load_plan()
                self.progress.reset()
            else:
                self.show_powered_report(self.report)
        if self.worker is not None:
            self.worker.deleteLater()
        self.session_id = str(uuid4())
        self.metadata = None
        self._completed = self._pending = self._manual_live = self._awaiting_manual = False
        self._failure_reason = self._terminal_reason = ''
        self.error_banner.hide()
        self.state = 'connecting'
        self.setup_toggle.setChecked(True)
        password = self.password.text()
        self.password.clear()
        self.worker = MotorControlWorker(connection, password, self.dof.currentData(), self.session_id, self)
        self.worker.received.connect(self.on_event)
        self.worker.failed.connect(self.on_error)
        self.worker.finished.connect(self.session_finished)
        self.worker.start()
        self.status.setText('正在只读核对配置；连接不会自动使能或续测。')
        self.refresh()

    def send(self, command):
        if not self.active() or self.worker.cancel_event.is_set():
            return False
        self.launch_context()
        self.worker.commands.put(command)
        return True

    def enable_motors(self):
        self.refresh()
        if not self.enable_button.isEnabled():
            return
        kd = self.damping.value()
        answer = QMessageBox.question(self, '明确使能', f'本次 Kp=0、Kd={kd:.2f}、前馈=0。\n请确认机器人已支撑固定。继续后仅人工摆动，不自动运动。',
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        self.refresh()
        if answer == QMessageBox.StandardButton.Yes and self.enable_button.isEnabled() and self.damping.value() == kd:
            self._enabled_kd = kd
            self._pending = True
            self.send({'command': 'enable', 'damping_kd': kd})
            self.status.setText(f'正在确认全部电机使能；本次 Kd={kd:.2f}。')
            self.refresh()

    def start_check(self):
        self.refresh()
        if not self.start_button.isEnabled():
            return
        if self.angle_ready():
            self.angle_view = True
            self._angle_save_path = None
            self.angle_index = min(self.angle_index, len(self.plan['outcomes']) - 1)
            self.auto_check = (EncoderAngleCheck(self.plan, hold_s=self.hold_s.value(),
                                                 position_span_deg=self.position_span.value())
                               if self.metadata.get('encoder_observation_ready') is True else None)
            self.auto_mode = self.auto_check is not None
            self.check_started.emit({'kind': 'angle_observations', 'robot_id': self.initial_identity[0],
                                     'source_kind': self.initial_identity[1]})
            self.auto_latest, self.auto_last_stamp, self.auto_error = None, None, ''
            self.setup_toggle.setChecked(self.auto_mode and not self.site_ready.isChecked())
            self.angle_panel.show()
            self.angle_panel.setVisible(not self.auto_mode)
            self.angle_mode_button.setVisible(self.auto_mode)
            self.angle_mode_button.setText('单次读取与人工记录')
            self.powered_measurement_note.hide()
            self.powered_review_note.hide()
            self.conditions.setText('采集角度和人工实测值可用于角度复核。')
            self.save_status.setText(f'本轮报告归档 SN：{self.angle_sn}。' if self.auto_mode and self.angle_sn else
                                     '需要留档时点击“导出角度记录”。')
            self.show_angle_rows()
            self.show_angle_joint()
            self.populate_encoder_motors()
            self.status.setText('正在逐项检查角度。')
            if self.auto_mode:
                self.render_automatic_angle()
                self.auto_timer.start()
                self.poll_automatic_angle()
            self.refresh()
            return
        self.persist(force=True)
        self._factory_saved_signature = None
        self.check_started.emit({'robot_id': self.initial_identity[0], 'source_kind': self.initial_identity[1]})
        self._awaiting_manual = True
        self.send({'command': 'manual_check', 'plan': self.plan,
                   'options': {'tolerance_deg': 3, 'hold_s': self.hold_s.value(), 'position_span_deg': self.position_span.value(),
                               'torque_rise_nm': 3, 'contact_torque_nm': .5, 'torque_required': False},
                   'mapping_confirmed': True, 'reference_confirmed': True})
        self.status.setText('已提交人工检查，等待实测报告；按当前关节手动摆动。')
        self.refresh()

    def pause_check(self):
        self.refresh()
        if self.pause_button.isEnabled():
            if self.auto_mode:
                self.auto_check.pin_confirmation_pending = False
                self.auto_check.engine.pause(not self.auto_check.report()['paused'])
                self.clear_automatic_feedback('', discard_inflight=True)
                self.refresh()
                return
            self.send({'command': 'manual_pause', 'paused': not self.report.get('paused', False)})

    def retest_selected(self):
        self.refresh()
        index = self.table.currentRow()
        if self.retest_button.isEnabled() and 0 <= index < len(self.plan['outcomes']):
            if self.auto_mode:
                self.auto_check.retest(index)
                self.clear_automatic_feedback('', discard_inflight=True)
                self.refresh()
                return
            self.send({'command': 'manual_retest', 'index': index})

    def remember_skip_target(self):
        self._skip_target = self.auto_check.report()['current_index'] if self.auto_mode else None

    def skip_current(self):
        target, self._skip_target = self._skip_target, None
        self.refresh()
        if self.skip_button.isEnabled():
            if self.auto_mode:
                if target is not None and self.auto_check.report()['current_index'] != target:
                    return
                report = self.auto_check.skip()
                self.clear_automatic_feedback('', discard_inflight=True)
                if report['current_index'] is None:
                    self.persist_automatic_angles()
                self.refresh()
                return
            self.send({'command': 'manual_skip'})

    def continue_failed_angle(self):
        self.refresh()
        if not self.continue_failed_button.isEnabled():
            return
        joint = self.auto_check.report()['current']['joint']
        report = self.auto_check.engine.continue_failed()
        self.finish_angle_failure(joint, report, '不合格')

    def remember_unreached_target(self):
        current = self.auto_check.report()['current'] if self.auto_mode else None
        self._unreached_target = (current['index'], current['attempt_id']) if current else None

    def record_unreached_angle(self):
        target, self._unreached_target = self._unreached_target, None
        self.refresh()
        if not self.unreached_button.isEnabled():
            return
        current = self.auto_check.report()['current']
        if target != (current['index'], current['attempt_id']):
            return
        report = self.auto_check.mark_target_not_reached()
        self.finish_angle_failure(current['joint'], report, '不合格（未达目标）')

    def finish_angle_failure(self, joint, report, result):
        self.clear_automatic_feedback('', discard_inflight=True)
        self._angle_save_path = self.directory / f'angles-{self.session_id}.json'
        self.persist_automatic_angles()
        next_step = '本轮检查已结束。' if report['current_index'] is None else '继续检查。'
        self.status.setText(f'{joint_title(joint)}已记录{result}。{next_step}')
        self.refresh()

    def confirm_pin(self):
        self.refresh()
        if self.auto_mode:
            if self.pin_confirm_button.isEnabled():
                self.auto_check.confirm_pin()
                self.auto_sent_at = None
                self.status.setText('已确认销钉，等待下一帧开始计时。')
                self.refresh()
            return
        if self.pin_confirm_button.isEnabled() and self.send({'command': 'manual_confirm_pin'}):
            self._pin_confirmation_pending = True
            self.status.setText('已确认销钉，等待确认后的新角度反馈。')
            self.refresh()

    def disable_motors(self):
        if self.disable_button.isEnabled() and self.send({'command': 'disable'}):
            self._pending = True
            self.status.setText('已请求失能，等待回执；请现场确认。')
            self.refresh()

    def stop_and_disable(self):
        if self.active():
            if self.angle_view:
                self.worker.stop()
                self.state, self._pending = 'stopping', True
                self.status.setText('正在结束角度检查连接。')
                self.refresh()
                return
            self._terminal_reason = '用户请求停止并失能，报告仅覆盖最后收到的数据'
            self.persist(force=True)
            self.worker.stop()
            self.state, self._pending = 'stopping', True
            self.status.setText('正在停止并请求失能，等待控制端结束；请现场确认。')
            self.refresh()

    def disconnect_session(self):
        self.stop_and_disable()

    def invalidate(self, reason='机器人连接或配置绑定已改变'):
        self._invalidated = True
        self.site_ready.setChecked(False)
        self.on_error(reason + '；已请求停止并失能，不会自动续测。')

    def on_event(self, event):
        if event.get('session_id') != self.session_id:
            return
        kind = event.get('type')
        if kind == 'metadata':
            motors = event.get('motors')
            if (event.get('dof') != self.dof.currentData() or event.get('position_basis') != 'motor_output_sdk_rad'
                    or not isinstance(motors, list) or len(motors) != self.dof.currentData()
                    or not all(isinstance(m, dict) for m in motors)
                    or [m.get('index') for m in motors] != list(range(len(motors)))
                    or not event.get('configuration_sha256')):
                self.on_error('控制端配置或电机清单不匹配。')
                return
            self.metadata = event
            self._configuration_digest = event['configuration_sha256']
            if self.worker:
                self.worker.password = ''
        elif kind == 'state':
            self.state = event.get('state')
            self._pending = self.state == 'stopping'
            if self.state == 'enabled':
                self.status.setText(f'已确认全部电机使能，本次Kd={_number(self._enabled_kd)}；仅人工摆动。')
            elif self.state in ('ready', 'disabled'):
                self._manual_live = False
                self._awaiting_manual = False
                self.status.setText('控制会话就绪，尚未使能。' if self.state == 'ready' else '已请求失能；物理状态请现场确认。')
        elif kind == 'encoder_position':
            self.receive_encoder(event)
        elif kind == 'joint_encoder_positions':
            self.receive_automatic_angle(event)
        elif kind == 'enable_diagnostics':
            lines = [str(event.get('message') or '使能确认失败；请查看具体总线/ID与原始反馈。')]
            for row in event.get('motors', []):
                if row.get('status') != 'fresh':
                    lines.append(f"{row.get('interface')}/{row.get('motor_id')} · {row.get('status')} · 原始Type1 {row.get('raw_hex') or '未知'}")
            self.diagnostics.setText('\n'.join(lines))
        elif kind == 'manual_state':
            try:
                run_id = str(UUID(event.get('run_id', '')))
                json.dumps(event, allow_nan=False)
                report = validate_report(event['report'], self.dof.currentData())
                if (self._enabled_kd is not None and (type(event.get('damping_kd')) not in (int, float)
                        or abs(event['damping_kd']-self._enabled_kd) > 1e-9)):
                    raise ValueError('报告阻尼与本次明确使能的选择不符')
                if event.get('source_kind') != 'hardware':
                    raise ValueError('实测事件来源不是hardware')
                if report['source'].get('plan_sha256') != self.plan['plan_sha256']:
                    raise ValueError('参考方案摘要不符')
                if (report.get('dof') != self.dof.currentData() or not isinstance(report.get('results'), list)
                        or len(report['results']) != len(self.plan['outcomes'])):
                    raise ValueError('报告机型/结果数量不符')
                if (self.run_id is None or run_id != self.run_id) and not self._awaiting_manual:
                    raise ValueError('收到未请求的新检查轮次')
                for index, row in enumerate(report['results']):
                    plan_row = self.plan['outcomes'][index]
                    if (row.get('index') != index or row.get('joint') != plan_row['joint'] or row.get('direction') != plan_row['direction']
                            or abs(row['reference_deg']-math.degrees(plan_row['requested_rad'])) > 1e-7):
                        raise ValueError('报告关节顺序/方向不符')
            except (KeyError, TypeError, ValueError) as error:
                self.on_error('人工检查报告无效：' + str(error))
                return
            if self.run_id is None or run_id != self.run_id:
                self.persist(force=True)
                self._awaiting_manual = False
                self._terminal_reason = ''
                self._report_configuration_digest = self._configuration_digest
                self.setup_toggle.setChecked(False)
            self._manual_live = True
            current = report.get('current') or {}
            if current.get('check_mode') != 'pin_zero' or current.get('pin_confirmed_at_s') is not None:
                self._pin_confirmation_pending = False
            self._last_report_at = time.monotonic()
            self.run_id, self.report = run_id, report
            self.last_event = json.loads(json.dumps(event, allow_nan=False))
            self._enabled_kd = event.get('damping_kd', self._enabled_kd)
            self.show_powered_report(report)
            self.status.setText(_PHASES.get(report.get('phase'), str(report.get('phase'))))
            if not self._failure_reason:
                if report.get('phase') == 'failed':
                    if (report.get('current') or {}).get('check_mode') == 'pin_zero':
                        message = '腰部插销零位检查中断，请核对状态后重测或跳过。'
                    else:
                        message = '本关节已越过外侧±3°容差：本轮不合格保留。可重测选中关节，或跳过并记未完成。'
                    self.error_banner.setText(message)
                    self.error_banner.show()
                else:
                    self.error_banner.hide()
            if report.get('interruption_reason'):
                self.status.setText(self.status.text() + ' · ' + report['interruption_reason'])
            self.persist()
        elif kind == 'error':
            self.on_error(event.get('message') or '控制端报告异常')
        elif kind == 'complete':
            self._completed = True
            if self.angle_view:
                self.status.setText('角度检查连接已结束。')
            else:
                self._terminal_reason = event.get('reason') or '控制会话结束'
                self.persist(force=True)
                self.status.setText('控制会话结束；' + ('已向SDK提交失能指令，物理停止请现场确认。' if event.get('disable_sent') else '未收到完整失能执行回执，请现场确认。'))
        self.refresh()

    def show_powered_report(self, report):
        for index, row in enumerate(report['results']):
            attempts = row.get('attempts') or []
            attempt = next((a for a in reversed(attempts) if a.get('attempt_id') == row.get('effective_attempt_id')), None)
            self.set_row(index, row, row['status'], attempt, len(attempts))
        self.show_current(report.get('current'), report.get('current_index'))
        count = len(self.plan['outcomes'])
        self.progress.setRange(0, count)
        done = sum(row['status'] in ('passed', 'failed', 'skipped') for row in report['results'])
        self.progress.setValue(done)
        self.progress.setFormat(f'{done}/{count} 已记录')

    def payload(self):
        return {'schema_version': 1, 'run_id': self.run_id, 'robot_id': self.initial_identity[0],
                'configuration_sha256': self._report_configuration_digest, 'source_kind': 'hardware',
                'reference_source_kind': self.report.get('source', {}).get('reference_source_kind', 'unknown'),
                'reference_plan_source_kind': self.plan.get('source_kind', 'unknown'), 'plan_sha256': self.plan['plan_sha256'],
                'damping_kd': self.last_event.get('damping_kd'), 'saved_at': datetime.now(timezone.utc).isoformat(),
                'terminal_reason': self._terminal_reason or None,
                'coverage_note': '腰部侧摆（roll）插销确认后、左右髋 pitch 自动检查零位±2°并保持5秒；其他关节仅检查所选单侧。仅覆盖最后收到的数据，不代表双侧全行程合格。',
                'reference_plan': self.plan, 'report': self.report, 'event': self.last_event}

    def write_report(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(self.payload(), stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def persist(self, *, force=False):
        if self.report is None or self.run_id is None:
            return
        signature = (self.run_id, self.report.get('phase'), self.report.get('current_index'),
                     tuple((r.get('status'), r.get('effective_attempt_id'), len(r.get('attempts') or [])) for r in self.report['results']))
        now = time.monotonic()
        if not force and signature == self._save_signature and now-self._save_at < 1:
            return
        self._save_at, self._save_signature = now, signature
        path = self.directory / (self.run_id + '.json')
        try:
            self.write_report(path)
        except (OSError, TypeError, ValueError) as error:
            self.save_error = '自动保存失败：' + str(error) + '；当前最新记录尚未确认落盘，请另存。'
            self.save_status.setText(self.save_error)
            self.save_error_banner.setText(self.save_error)
            self.save_error_banner.show()
            self.save_status.setStyleSheet('color:#982e35;font-weight:700;')
        else:
            self.save_error = ''
            self.save_error_banner.hide()
            self.last_saved_path = path
            self.save_status.setText('本轮已自动保存在电脑：' + str(path))
            self.save_status.setStyleSheet('color:#276b48;')
            if self.report.get('phase') == 'complete' and signature != self._factory_saved_signature:
                self._factory_saved_signature = signature
                self.check_saved.emit({'path': str(path), 'robot_id': self.initial_identity[0], 'run_id': self.run_id})

    def persist_automatic_angles(self):
        path = self._angle_save_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.angles.export(path, self.angle_sn, self.dof.currentData(),
                               automatic=self.auto_check, run_id=self.session_id)
        except (OSError, ValueError) as error:
            self.save_status.setText(f'角度记录保存失败，可另行导出。{error}')
        else:
            self.save_status.setText(f'本轮角度记录已保存到电脑：{path}')

    def save_angles_to_results(self):
        if not self.save_to_results_button.isEnabled():
            return
        data = self.angles.payload(self.angle_sn, self.dof.currentData(),
                                   automatic=self.auto_check, run_id=self.session_id)
        self._angle_report_pending = self.session_id
        self.save_status.setText(f'正在保存本轮角度记录到 SN：{self.angle_sn}。')
        self.refresh()
        self.check_saved.emit(data)

    def angle_report_finished(self, run_id, message):
        if self._angle_report_pending != run_id:
            return
        self._angle_report_pending = None
        if self.session_id == run_id:
            self.save_status.setText(message)
        self.refresh()

    def export_report(self):
        if self.angle_view:
            path, _ = QFileDialog.getSaveFileName(self, '导出角度记录', 'angle-observations.json', 'JSON (*.json)')
            if path:
                try:
                    self.angles.export(path, self.angle_sn, self.dof.currentData(),
                                       automatic=self.auto_check, run_id=self.session_id)
                except (OSError, ValueError) as error:
                    self.save_status.setText(f'导出角度记录失败：{error}')
                else:
                    automatic_count = self.auto_check.evidence_count if self.auto_check else 0
                    count = len(self.angles.records) + len(self.angles.encoder_samples) + automatic_count
                    self.save_status.setText(f'已导出 {count} 条角度记录：{path}')
            return
        if self.report is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, '另存人工检查结果', 'manual-calibration-check.json', 'JSON (*.json)')
        if path:
            try:
                self.write_report(path)
            except (OSError, TypeError, ValueError) as error:
                self.save_status.setText('另存失败：' + str(error))
                self.save_error_banner.setText('另存失败：' + str(error))
                self.save_error_banner.show()
                self.save_status.setStyleSheet('color:#982e35;')
            else:
                self.save_status.setText('本次检查已另存在电脑：' + path)

    def on_error(self, message):
        self._failure_reason = self._failure_reason or message
        self._terminal_reason = self._failure_reason + '；报告只覆盖最后收到的数据'
        self.persist(force=True)
        self.state, self._pending = 'fault', False
        self.status.setText(self._terminal_reason)
        if self.active():
            self.worker.stop()
        self.refresh()

    def session_finished(self):
        self.encoder_query = None
        self.auto_query = None
        self.auto_timer.stop()
        if self.auto_mode:
            self.auto_latest = None
            self.auto_error = '角度检查连接已断开。'
            self.render_automatic_angle()
        self.persist_automatic_angles()
        if not self._completed:
            if self.angle_view:
                self.status.setText('角度检查连接已断开，仍可导出已有记录。')
            else:
                self._terminal_reason = self._terminal_reason or '连接已断开，未收到完整结束回执；报告只覆盖最后收到的数据'
                self.persist(force=True)
                self.status.setText(self._terminal_reason + '；请现场确认失能。')
        self.state, self._pending = 'disconnected', False
        self._manual_live = self._awaiting_manual = False
        self.metadata = None
        self.refresh()
        if self._close_requested:
            self.timer.stop()
            self.guide.shutdown()
            super().reject()

    def shutdown(self):
        self.password.clear()
        self.persist(force=True)
        if self.active():
            self.stop_and_disable()
            return self.worker.wait(1500)
        return True

    def reject(self):
        self._close_requested = True
        if self.shutdown():
            self.timer.stop()
            self.guide.shutdown()
            super().reject()

    def closeEvent(self, event):
        self._close_requested = True
        if self.shutdown():
            self.timer.stop()
            self.guide.shutdown()
            event.accept()
        else:
            event.ignore()
