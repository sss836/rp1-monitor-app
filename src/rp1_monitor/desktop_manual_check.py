"""Explicit manual endpoint inspection; no connection or motor action on open."""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtCore import Qt, QTimer, Signal
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

from .desktop_motor_control import MotorControlDialog, MotorControlWorker, _binding
from .desktop_urdf_guide import UrdfJointGuide
from .manual_calibration_check import validate_report

_LABELS = {'hip': '髋', 'knee': '膝', 'ankle': '踝', 'waist': '腰',
           'shoulder': '肩', 'elbow': '肘', 'wrist': '腕'}
_STATUSES = {'pending': '未测', 'active': '检查中', 'passed': '合格', 'failed': '不合格（本轮保留）',
             'skipped': '已跳过', 'interrupted': '已中断'}
_PHASES = {'ready': '等待手动摆动', 'collecting': '正在采集', 'paused': '已暂停',
           'failed': '本关节不合格，暂停后可重测', 'complete': '本轮检查结束'}


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

    def __init__(self, calibration_context, connection_context, parent=None, *, directory=None):
        super().__init__(parent)
        self.current_context, self.connection_context = calibration_context, connection_context
        initial = calibration_context()
        self.initial_binding = _binding(connection_context())
        self.initial_identity = tuple(initial.get(k) for k in ('robot_id', 'source_kind', 'stream_id'))
        self.initial_configuration = initial.get('configuration_binding')
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
        setup_layout.addWidget(_label('先支撑固定机器人并退出其他运控/标零工具。连接只读核对；使能后由人操作，程序不自动运动。腰部插销和左右髋 pitch 在零位±2°保持5秒；其余关节在参考±3°内且稳定。力矩仅辅助记录。'))
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
        self.connect_button = QPushButton('连接控制会话')
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
        self.pin_confirm_button = QPushButton('销钉已插好，开始检查')
        self.pin_confirm_button.hide()
        for button in (self.enable_button, self.disable_button, self.start_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        review_actions = QHBoxLayout()
        for button in (self.pause_button, self.retest_button, self.skip_button):
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
        measurement.addWidget(_label('关节角度用于判定，力矩仅辅助记录；结果自动保存在电脑。'))
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
        layout.addWidget(_label('腰部侧摆先插好销钉并确认；腰部和左右髋 pitch 在零位±2°内保持5秒。其他关节检查所选单侧±3°。暂停后可重测，历史保留。'))
        self.diagnostics = _label()
        layout.addWidget(self.diagnostics)
        self.save_status = _label('收到检查报告后自动保存到本机；拔线不删除已收记录。')
        layout.addWidget(self.save_status)
        self.export_button = QPushButton('另存本轮 JSON')
        layout.addWidget(self.export_button)
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
        self.skip_button.clicked.connect(self.skip_current)
        self.pin_confirm_button.clicked.connect(self.confirm_pin)
        self.stop_button.clicked.connect(self.stop_and_disable)
        self.export_button.clicked.connect(self.export_report)
        self.dof.currentIndexChanged.connect(self.load_plan)
        for signal in (self.password.textChanged, self.site_ready.toggled, self.damping.valueChanged):
            signal.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
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

    def set_row(self, index, row, status, attempt, count):
        peak = (attempt or {}).get('peak_angle_deg')
        ref = row.get('reference_deg', math.degrees(row.get('requested_rad', 0)))
        delta = peak - ref if type(peak) in (int, float) else None
        side = {'lower': '下限', 'upper': '上限', 'center': '插销零位' if row.get('check_mode') == 'pin_zero' else '零位'}.get(row['direction'], '未知')
        values = (joint_title(row['joint']) + ' · ' + side,
                  _number(ref), _number(peak), _number(delta, signed=True), _STATUSES.get(status, status), str(count))
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(row['joint'])
            self.table.setItem(index, col, item)

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

    def refresh(self, *_):
        active = self.active()
        try:
            self.launch_context()
            valid = True
        except ValueError as error:
            valid = False
            if active and not self._invalidated:
                self.invalidate(str(error))
        enabled = active and self.state == 'enabled' and not self._pending
        manual_active = self._manual_live and self.report is not None
        interrupted = bool(self.report and self.report.get('interruption_reason'))
        self.connect_button.setEnabled(valid and not active and bool(self.password.text()))
        self.disconnect_button.setEnabled(active and self.state != 'stopping')
        self.enable_button.setEnabled(valid and active and self.metadata is not None and self.state in ('ready', 'disabled')
                                      and self.site_ready.isChecked() and not self._pending)
        self.enable_button.setText(f'使能 · Kd {self.damping.value():.2f}')
        self.disable_button.setEnabled(enabled)
        self.start_button.setEnabled(valid and enabled and self.site_ready.isChecked() and self.reference_ready.isChecked()
                                     and (self.metadata or {}).get('calibration_check_ready') is True
                                     and (not manual_active or self.report.get('phase') == 'complete') and not self._awaiting_manual)
        self.pause_button.setEnabled(valid and enabled and manual_active and not interrupted
                                    and self.report.get('phase') not in ('complete', 'failed'))
        self.pause_button.setText('继续人工检查' if self.report and self.report.get('paused') else '暂停检查')
        self.retest_button.setEnabled(valid and enabled and manual_active and not interrupted
                                     and (self.report.get('paused') is True or self.report.get('phase') == 'complete'))
        self.skip_button.setEnabled(valid and enabled and manual_active and not interrupted and self.report.get('current_index') is not None)
        current = self.report.get('current') if self.report else None
        pin_active = bool(current and current.get('check_mode') == 'pin_zero' and current.get('status') == 'active')
        self.pin_confirm_button.setVisible(pin_active)
        self.pin_confirm_button.setEnabled(valid and enabled and manual_active and not interrupted and pin_active
                                           and not self.report.get('paused') and current.get('pin_confirmed_at_s') is None
                                           and not self._pin_confirmation_pending)
        self.stop_button.setEnabled(active)
        self.export_button.setEnabled(self.report is not None)
        self.damping.setEnabled(not self._pending and (not active or self.state in ('ready', 'disabled')))
        self.dof.setEnabled(not active and self.report is None)
        editable = (not manual_active or self.report.get('phase') == 'complete') and not self._awaiting_manual
        self.hold_s.setEnabled(editable)
        self.position_span.setEnabled(editable)
        self.setup_toggle.setText(f'连接与参数 · Kd {self.damping.value():.2f} · 稳定 {self.hold_s.value():g}s / {self.position_span.value():g}°（点击展开/收起）')
        if self.report is not None and not self._manual_live and not self.current_joint.text().startswith('上轮记录'):
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
            self.send({'command': 'manual_pause', 'paused': not self.report.get('paused', False)})

    def retest_selected(self):
        self.refresh()
        index = self.table.currentRow()
        if self.retest_button.isEnabled() and 0 <= index < len(self.plan['outcomes']):
            self.send({'command': 'manual_retest', 'index': index})

    def skip_current(self):
        self.refresh()
        if self.skip_button.isEnabled():
            self.send({'command': 'manual_skip'})

    def confirm_pin(self):
        self.refresh()
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
            self._terminal_reason = event.get('reason') or '控制会话结束'
            self.persist(force=True)
            self.status.setText('控制会话结束；' + ('已向SDK提交失能指令，物理停止请现场确认。' if event.get('disable_sent') else '未收到完整失能执行回执，请现场确认。'))
        self.refresh()

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

    def export_report(self):
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
        if not self._completed:
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
