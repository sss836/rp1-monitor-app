"""Local factory recordings; operators start strategies on the robot manually."""

import hashlib
import json
import os
import sqlite3
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .board_check_view import BoardCheckView, board_check_view
from .desktop_result_summary import ResultSummaryPanel
from .factory_duration_policy import duration_status
from .factory_models import FactorySegmentStart
from .factory_store import (
    BATTERY_SWAP_STAGES,
    FUNCTIONAL_CHECK_NAMES,
    ITEM_NAMES,
    STATUS_NAMES,
    FactoryStore,
)
from .factory_test_catalog import (
    BOARD_CASE_CODES,
    CATEGORY_NAMES,
    ESTOP_PAIRING_CASE_CODE,
    FUNCTION_CASE_CODES,
    MOTION_CASE_CODES,
    MOTOR_PARAMETERS_CASE_CODE,
    TEST_FAMILIES,
    standalone_catalog,
    test_catalog,
)

_WORKERS = set()
TYPES = {"standing": "站立", "walking": "行走", "running": "跑步", "stunt": "特技"}


class SegmentImportError(ValueError):
    def __init__(self, message, identifier, sealed_info=None):
        super().__init__(message)
        self.identifier = identifier
        self.sealed_info = sealed_info


class FactoryJob(QThread):
    completed = Signal(object)
    failed = Signal(object)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation())
        except Exception as exc:
            self.failed.emit(exc)


def _send_light_test(context, password=None):
    from .deploy_policy import station_headers
    from .factory_client import _local_url

    body = {"robot_id": context["robot_id"], "relay_url": context["relay_url"]}
    if password is not None:
        body["sudo_password"] = password
    try:
        with httpx.Client(timeout=25, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                _local_url(context["station_url"]) + "/api/v1/station/light-test",
                headers=station_headers(context),
                json=body,
            )
    except (OSError, httpx.HTTPError) as exc:
        raise ValueError("未收到点灯回执，请核对电脑后台和机器人连接") from exc
    if response.status_code == 404:
        raise ValueError("电脑后台尚未支持点灯测试，请使用新版上位机后台并重新连接")
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("电脑后台返回的点灯回执无效")
    if response.status_code != 200:
        detail = result.get("detail")
        raise ValueError(detail if isinstance(detail, str) else "电脑后台未确认点灯命令")
    allowed = {"command_sent", "password_required"} if password is None else {"command_sent"}
    if result.get("robot_id") != context["robot_id"] or result.get("status") not in allowed:
        raise ValueError("点灯回执与所选机器人不匹配")
    return result["status"]


class _NoticeLabel(QLabel):
    text_changed = Signal(str)

    def setText(self, text):
        super().setText(text)
        self.text_changed.emit(text)


def _text(text, *, label_type=QLabel):
    widget = label_type(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def _table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.setMinimumHeight(165)
    return widget


class UnavailableFactoryPanel(QScrollArea):
    def __init__(self, error):
        super().__init__()
        self.notice = _text(f"工厂本地目录暂不可用：{error}。已有文件未删除；请核对目录权限和磁盘空间后重开上位机。研发页面仍可使用。")
        self.setWidgetResizable(True)
        self.setWidget(self.notice)

    def shutdown(self):
        return True


def create_factory_panel(source_provider, directory=None, deploy_context_provider=None):
    try:
        return FactoryPanel(source_provider, directory, deploy_context_provider)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return UnavailableFactoryPanel(exc)


class FactoryPanel(QScrollArea):
    tool_requested = Signal(str, int)
    sn_changed = Signal(str)
    board_saved = Signal(str, object)

    def __init__(self, source_provider, directory=None, deploy_context_provider=None):
        super().__init__()
        self.source_provider = source_provider
        self.deploy_context_provider = deploy_context_provider or (lambda: {})
        self.battery_swap_dialog = None
        self.live_frame = None
        self.live_frame_received = None
        self.store = FactoryStore(directory or os.environ.get("RP1_FACTORY_DIR") or Path.home() / ".local/share/rp1-monitor/factory")
        self.board_profile_values = self.store.board_profile()
        self.client = self.tunnel = None
        self.identity = None
        self._motion_epoch = 0
        self._motion_worker = None
        self._motion_next_poll = 0.0
        self._motion_confirmed_at = None
        self._motion_confirmed_wall = None
        self._motion_problem = ""
        self._motion_active_id = None
        self._motion_pending = []
        self._motion_paused = False
        self.sn = ""
        self.remote_rows = []
        self.local_rows = []
        self.attachments = []
        self.busy = False
        self.closing = False
        self.closed = False
        self.fingerprint = ""
        self.action_buttons = []
        self.selected_stage = 1
        self.board_queue = []
        self.setWidgetResizable(True)
        self.setStyleSheet("""
            QPushButton#factorySecondaryAction { background:white; color:#45618b;
                border:1px solid #cbd8ec; font-weight:500; }
            QTabWidget#factoryTabs::pane { border:none; background:transparent; }
            QTabBar#factoryTabBar::tab { border:0; background:transparent; padding:12px 20px; }
            QTabBar#factoryTabBar::tab:selected { border-bottom:2px solid #3559e6; color:#3559e6; }
        """)
        page = QWidget()
        layout = QVBoxLayout(page)
        self.notice = _text("主控、标定、功能、运控分别测试。运控先开始记录，再到机器人端手动启动deploy，测试后读取数据。", label_type=_NoticeLabel)
        row = QHBoxLayout()
        self.sn_edit = QComboBox()
        self.sn_edit.setEditable(True)
        self.sn_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.sn_edit.setPlaceholderText("输入或扫描整机SN")
        self.sn_edit.setAccessibleName("整机SN")
        self.sn_edit.lineEdit().returnPressed.connect(self.open_sn)
        row.addWidget(self.sn_edit, 1)
        self._button(row, "打开 / 新建SN", self.open_sn)
        self.model = QComboBox()
        self.model.addItem("RP1 · 24轴", 24)
        self.model.addItem("RP1 · 28轴", 28)
        self.model.setAccessibleName("工厂SN机型")
        self.model.currentIndexChanged.connect(self.model_changed)
        row.addWidget(self.model)
        catalog_button = self._button(row, "编号目录", self.show_test_catalog)
        catalog_button.setObjectName("factorySecondaryAction")
        layout.addLayout(row)
        self.current_sn = _text("当前SN：未选择")
        layout.addWidget(self.current_sn)

        motion_connection = QWidget()
        connection_layout = QVBoxLayout(motion_connection)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_layout.addWidget(_text("运控记录自动复用主界面连接。"))
        self.policy = QLineEdit()
        self.policy.setPlaceholderText("本段记录标签（可选）")
        self.policy.setAccessibleName("本段记录标签")
        connection_layout.addWidget(self.policy)
        quick = QHBoxLayout()
        self._button(quick, "使用主界面连接", self.use_current)
        self.motion_refresh_button = self._button(quick, "立即核对", self.refresh_remote)
        self._button(quick, "断开本页连接", self.disconnect_connection)
        connection = QGroupBox("独立SSH：仅转发机器人已有监听")
        connection.setVisible(False)
        ssh_settings = QPushButton("独立 SSH 设置")
        ssh_settings.setCheckable(True)
        ssh_settings.toggled.connect(connection.setVisible)
        quick.addWidget(ssh_settings)
        self.action_buttons.append(ssh_settings)
        connection_layout.addLayout(quick)
        form = QFormLayout(connection)
        self.target = QLineEdit("robo@192.168.137.250")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(8891)
        self.trust = QLineEdit()
        self.trust.setPlaceholderText("首次连接请核对下面提示的指纹，再原样填入")
        form.addRow("SSH地址", self.target)
        form.addRow("密码（不保存）", self.password)
        form.addRow("监听端口", self.port)
        form.addRow("已核对的SSH指纹", self.trust)
        buttons = QHBoxLayout()
        self._button(buttons, "独立SSH连接", self.connect_ssh)
        form.addRow(buttons)
        connection_layout.addWidget(connection)
        self.work_tabs = QTabWidget()
        self.work_tabs.setObjectName("factoryTabs")
        self.work_tabs.tabBar().setObjectName("factoryTabBar")
        layout.addWidget(self.work_tabs)

        record = QGroupBox("机器人本地测试记录")
        record.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        body = QVBoxLayout(record)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        identity = QWidget()
        identity.setObjectName("factoryMotionIdentity")
        identity.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        identity_row = QHBoxLayout(identity)
        identity_row.setContentsMargins(14, 9, 14, 9)
        self.motion_sn_label = _text("未选择")
        self.motion_robot_label = _text("—")
        self.motion_connection_label = _text("未连接")
        self.motion_connection_label.setObjectName("factoryMotionConnection")
        for title, value in (("整机 SN", self.motion_sn_label), ("机器人", self.motion_robot_label)):
            group = QVBoxLayout()
            group.setSpacing(2)
            label = _text(title)
            label.setObjectName("factoryMotionCaption")
            value.setObjectName("factoryMotionIdentityValue")
            group.addWidget(label)
            group.addWidget(value)
            group.setAlignment(Qt.AlignmentFlag.AlignTop)
            identity_row.addLayout(group, 1)
        identity_row.addWidget(self.motion_connection_label)
        body.addWidget(identity)

        recording = QWidget()
        recording.setObjectName("factoryMotionCard")
        recording.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        recording_layout = QVBoxLayout(recording)
        recording_layout.setContentsMargins(14, 12, 14, 12)
        recording_layout.setSpacing(10)
        recording_header = QHBoxLayout()
        self.motion_record_heading = _text("本轮记录")
        self.motion_record_heading.setObjectName("factoryMotionHeading")
        self.motion_record_heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.motion_state_label = _text("状态待确认")
        self.motion_state_label.setObjectName("factoryMotionState")
        self.motion_state_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        recording_header.addWidget(self.motion_record_heading, 1)
        recording_header.addWidget(self.motion_state_label)
        recording_layout.addLayout(recording_header)

        self.connection_options = motion_connection
        motion_connection.hide()
        self.motion_type_options = QWidget()
        entry = QHBoxLayout(self.motion_type_options)
        entry.setContentsMargins(0, 0, 0, 0)
        self.test_type = QComboBox()
        for value, title in TYPES.items():
            self.test_type.addItem(f"{MOTION_CASE_CODES[value]} · {title}", value)
        self.test_type.currentIndexChanged.connect(self.motion_type_changed)
        self.test_type.setAccessibleName("运控动作类型")
        entry.addWidget(_text("动作类型"))
        entry.addWidget(self.test_type)
        entry.addStretch(1)
        recording_layout.addWidget(self.motion_type_options)
        self.motion_type_options.hide()

        self.duration_label = _text("—")
        self.motion_valid_points = _text("—")
        self.motion_health_points = _text("—")
        self.motion_duration_caption = _text("记录时长（约）")
        metrics = QHBoxLayout()
        for label, value in ((self.motion_duration_caption, self.duration_label),
                             (_text("运控有效点"), self.motion_valid_points),
                             (_text("健康有效点"), self.motion_health_points)):
            group = QVBoxLayout()
            group.setSpacing(2)
            label.setObjectName("factoryMotionCaption")
            value.setObjectName("factoryMotionValue")
            group.addWidget(label)
            group.addWidget(value)
            group.setAlignment(Qt.AlignmentFlag.AlignTop)
            metrics.addLayout(group, 1)
        recording_layout.addLayout(metrics)

        actions = QHBoxLayout()
        self.start_button = self._button(actions, "开始记录", self.start_segment)
        self.read_button = self._button(actions, "结束记录并取回", self.read_data)
        self.read_button.setToolTip("结束并取回当前样品、机器人及测试项目的活动记录。")
        actions.addStretch(1)
        advanced = QPushButton("高级设置")
        advanced.setCheckable(True)
        advanced.toggled.connect(motion_connection.setVisible)
        actions.addWidget(advanced)
        self.action_buttons.append(advanced)
        recording_layout.addLayout(actions)
        recording_layout.addWidget(motion_connection)

        self.motion_notice = _text("测试期间可接网线查看实时趋势。取回后在“测试数据”查看曲线和关键统计。")
        self.motion_notice.setObjectName("factoryMotionNotice")
        self.motion_notice.setAccessibleName("动作日志连接、开始记录与导入结果")
        self.motion_notice.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.motion_notice.setStyleSheet("color:#294f86;background:#edf3ff;border:1px solid #cbdcf5;border-radius:6px;padding:8px;")
        self.notice.text_changed.connect(self.motion_notice.setText)
        recording_layout.addWidget(self.motion_notice)
        self.motion_recovery = QWidget()
        recovery = QHBoxLayout(self.motion_recovery)
        recovery.setContentsMargins(0, 0, 0, 0)
        self.motion_retry_button = self._button(recovery, "重试本次开始", self.retry_start)
        self.motion_release_button = self._button(recovery, "放弃本次请求", self.release_pending)
        recovery.addStretch(1)
        recording_layout.addWidget(self.motion_recovery)
        self.motion_recovery.hide()
        body.addWidget(recording)

        self.duration_timer = QTimer(self)
        self.duration_timer.timeout.connect(self.update_duration)
        self.duration_timer.timeout.connect(self._poll_motion)
        self.duration_timer.start(1000)
        self.manual_deploy_guide = _text("确认样品 SN 与机器人后开始记录。现场人员在机器人端手动启动策略，站立期间可接网线查看实时趋势。站立记录达到2小时后取回；行走、跑步和特技无两小时时长要求。结束记录后在“测试数据”查看曲线和关键统计。24轴特技使用UFO，28轴不适用。结论由人员填写。")
        body.addWidget(self.manual_deploy_guide)
        self.functional_check_box = QGroupBox("整机功能必测 · 24轴 / 28轴")
        function_layout = QVBoxLayout(self.functional_check_box)
        function_layout.addWidget(_text("远程急停现场测试后点击确认正常或异常。热换电点击“开始换电 / 监听SOC”，保存换电开始后的SOC变化截图，再确认主控不断电、无重启及通信运行正常。允许全程连接网线；这两项操作不单独要求两小时。"))
        self.functional_stage_note = _text("远程急停：未测；热换电：未测。")
        function_layout.addWidget(self.functional_stage_note)
        function_buttons = QHBoxLayout()
        self.functional_buttons = {}
        for kind, title in FUNCTIONAL_CHECK_NAMES.items():
            self.functional_buttons[kind] = self._button(
                function_buttons, f"{FUNCTION_CASE_CODES[kind]} · " + ("确认远程急停结果" if kind == "remote_estop" else "热换电测试"),
                lambda _checked=False, value=kind: self.confirm_functional_check(value))
        function_layout.addLayout(function_buttons)
        body.addWidget(self.functional_check_box)
        history = QWidget()
        history.setObjectName("factoryMotionCard")
        history.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        history_layout = QVBoxLayout(history)
        history_layout.setContentsMargins(14, 12, 14, 12)
        history_header = QHBoxLayout()
        history_label = _text("历史片段")
        history_label.setObjectName("factoryMotionHeading")
        history_header.addWidget(history_label, 1)
        self.motion_history_button = self._button(history_header, "取回所选历史片段", self.import_motion_history)
        history_layout.addLayout(history_header)
        self.remote_table = _table(["SN / 类型", "机器人 / 来源", "状态 / 片段ID", "有效点（健康 / 运动）"])
        history_layout.addWidget(self.remote_table)
        body.addWidget(history)
        buttons = QHBoxLayout()
        self.motion_result_button = self._button(buttons, "保存到第4项结果", self.save_motion_evidence)
        buttons.addStretch(1)
        body.addLayout(buttons)
        self.work_tabs.addTab(self.build_workflow(record), "测试项目")

        evidence = QGroupBox("电脑已保存日志（拔插网线不影响这些文件）")
        body = QVBoxLayout(evidence)
        self.local_table = _table(["类型 / 来源", "时间 / 状态", "片段ID", "有效点 / 缺失"])
        body.addWidget(self.local_table)
        controls = QHBoxLayout()
        self.metric_selector = QComboBox()
        self.metric_selector.setEditable(True)
        self.metric_selector.setAccessibleName("日志曲线指标")
        controls.addWidget(self.metric_selector, 1)
        self._button(controls, "查看曲线", self.show_curve)
        self._button(controls, "从文件导入日志", self.import_file)
        body.addLayout(controls)
        self.key_data_note = _text("读取后显示记录时长及关键指标统计；缺失不补零，不自动判定性能合格。")
        body.addWidget(self.key_data_note)
        self.key_data_table = _table(["关键指标", "最小值", "最大值", "均值", "有效 / 观察次数", "最大数据年龄 / ms"])
        self.key_data_table.setMaximumHeight(210)
        self.key_data_table.hide()
        body.addWidget(self.key_data_table)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.setMinimumHeight(235)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "本段单调时间", units="s")
        body.addWidget(self.plot)
        self.curve_note = _text("选择已导入片段查看曲线；缺失值留空，图上断开。")
        body.addWidget(self.curve_note)
        self.local_table.itemSelectionChanged.connect(self.select_local)
        self.work_tabs.addTab(evidence, "测试数据")

        results = QGroupBox("本SN整机测试报告 · 当前最终版与原始证据")
        self.results_box = results
        body = QVBoxLayout(results)
        self.result_summary = ResultSummaryPanel(self.store, self)
        self.result_summary.busy_changed.connect(self._summary_busy_changed)
        self.result_summary.exported.connect(lambda path: self.notice.setText(f"当前最终版CSV已导出：{path}"))
        body.addWidget(self.result_summary)
        self.result_table = _table(["测试项", "已完成", "人工结论", "证据 / 备注"])
        self.result_table.setMinimumHeight(210)
        for column, width in ((0, 230), (1, 110), (2, 190)):
            self.result_table.setColumnWidth(column, width)
        self.result_table.itemChanged.connect(self.completion_changed)
        self.result_table.cellClicked.connect(lambda row, column: self.item.setCurrentIndex(row) if column != 1 else None)
        body.addWidget(self.result_table)
        pairing_results = QGroupBox(f"{ESTOP_PAIRING_CASE_CODE} · 远程急停开关对频测试")
        pairing_results_layout = QVBoxLayout(pairing_results)
        self.estop_pairing_history = _table(["确认时间", "是否通过（人工）"])
        self.estop_pairing_history.setMinimumHeight(120)
        self.estop_pairing_history.setMaximumHeight(220)
        pairing_results_layout.addWidget(self.estop_pairing_history)
        body.addWidget(pairing_results)
        self.functional_result_box = QGroupBox("整机功能 · 远程急停 / 热换电与SOC证据")
        function_results = QVBoxLayout(self.functional_result_box)
        function_results.addWidget(_text("以下是人工现场结论；遥测仅作证据，不自动判断急停生效或换电成功。人工记录时间不等于实际操作时间，操作时间请写入备注。"))
        self.functional_result_table = _table(["功能", "人工结论", "记录时间", "关联日志 / 现场观察"])
        function_results.addWidget(self.functional_result_table)
        self.soc_table = _table(["编号 / 记录时间", "电池阶段 / SOC", "机器人 / 来源", "连续性"])
        function_results.addWidget(self.soc_table)
        self._button(function_results, "查看选中SOC截图", self.show_soc_screenshot)
        self.functional_result_box.hide()
        body.addWidget(self.functional_result_box)
        self.board_evidence_note = _text("板卡结果：尚未关联。硬件自检页可直接保存现有结果，无需手工添加报告附件。")
        body.addWidget(self.board_evidence_note)
        self.board_table = _table(["板卡检查", "测试结果", "结果说明", "原始完成时间", "来源"])
        self.board_table.setMinimumHeight(190)
        for column, width in ((0, 250), (1, 90), (2, 360), (3, 200)):
            self.board_table.setColumnWidth(column, width)
        self.board_table.hide()
        body.addWidget(self.board_table)
        self.calibration_note = _text("标定检查：尚未关联。请先选SN；已有检查可导入本机JSON。")
        body.addWidget(self.calibration_note)
        self.calibration_table = _table(["关节", "检查位置", "参考 / °", "实测峰值 / °", "偏差 / °", "结果"])
        self.calibration_table.setMinimumHeight(190)
        self.calibration_table.hide()
        body.addWidget(self.calibration_table)
        self.calibration_import = QPushButton("导入已完成的标定检查 JSON")
        self.calibration_import.clicked.connect(self.import_calibration_check)
        body.addWidget(self.calibration_import)
        controls = QHBoxLayout()
        self.item = QComboBox()
        for number, title in ITEM_NAMES.items():
            self.item.addItem(f"{number}. {title}", number)
        self.status = QComboBox()
        for value, title in STATUS_NAMES.items():
            if value == "not_applicable":
                continue
            self.status.addItem(title, value)
        self.status.addItem(STATUS_NAMES["not_applicable"], "not_applicable")
        self.status.model().item(self.status.findData("not_applicable")).setEnabled(False)
        self.status.setCurrentIndex(self.status.findData("incomplete"))
        self.status.setItemText(self.status.findData("incomplete"), "未填写结论 / 待复核")
        controls.addWidget(self.item, 1)
        controls.addWidget(self.status)
        self.completed = QCheckBox("本项已完成")
        self.completed.setToolTip("人员确认本项测试已做完；不改变板卡失败、待复核或缺失证据。")
        self.status.activated.connect(lambda _: self.completed.setChecked(self.status.currentData() != "incomplete"))
        controls.addWidget(self.completed)
        body.addLayout(controls)
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("实际测试过程、缺失项、异常或复测说明；软件不自动判通过")
        self.notes.setMaximumHeight(95)
        body.addWidget(self.notes)
        controls = QHBoxLayout()
        self._button(controls, "添加本项附件", self.add_attachment)
        self.save_result_button = self._button(controls, "保存本项结果", self.save_result)
        self._button(controls, "导出原始归档 HTML / 明细CSV", self.export_reports)
        self._button(controls, "打开本地目录", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.directory))))
        body.addLayout(controls)
        self.attachment_note = _text("可附现有工具导出的报告、标定记录和照片。")
        body.addWidget(self.attachment_note)
        self.item.currentIndexChanged.connect(self.select_item)
        self.work_tabs.addTab(results, "测试结果与报告")
        layout.addWidget(self.notice)
        layout.addWidget(_text(f"本地目录：{self.store.directory}；数据保存在本机，中央工厂报告同步暂未接入。"))
        self.setWidget(page)
        self.select_stage(1)
        self.refresh_local()

    def build_workflow(self, record):
        from .desktop_factory_workbench import FactoryWorkbench

        self.workbench = FactoryWorkbench(self, record)
        return self.workbench

    def select_case(self, code):
        return self.workbench.select_case(code)

    def select_category(self, category):
        return self.workbench.select_category(category)

    def select_stage(self, number):
        # Legacy item numbers remain the archive keys, not a second navigation bar.
        return self.workbench.select_stage(number)

    def motion_type_changed(self):
        if hasattr(self, "workbench"):
            self.workbench.motion_type_changed()

    def show_case_result(self, code):
        from .factory_test_catalog import test_catalog

        entry = next((row for row in test_catalog(self.model.currentData()) if row["code"] == code), None)
        if entry is None:
            return
        self.item.setCurrentIndex(self.item.findData(entry["item"]))
        self.work_tabs.setCurrentIndex(2)
        table = self.result_summary.table
        for index in range(table.rowCount()):
            if table.item(index, 0).text() == code:
                table.setCurrentCell(index, 0)
                table.selectRow(index)
                table.scrollToItem(table.item(index, 0))
                break
        table.setFocus()
        self.ensureWidgetVisible(self.result_summary)
        self.notice.setText(f"当前SN：{self.sn or '未选择，请先打开SN'} · {code} · {entry['name']}。"
                            "已有结果与测试规则已带入，可编辑当前最终版、添加附件并导出。")

    def confirm_motor_check(self, code, verdict):
        """Save the operator's final conclusion for this local motor check only."""
        summary = self.result_summary
        if self.busy or self.closing or summary.busy:
            return
        if code not in ("SYS-003", "SYS-004") or verdict not in ("通过", "不通过"):
            return
        sn = self.sn
        row = next((row for row in summary._rows if row["row_key"] == code), None)
        if not sn or not row or not row.get("evidence"):
            self.notice.setText("请先打开对应SN，并读取电机固件 / 参数检查结果，再确认本项结论。")
            return
        edit = deepcopy(summary._drafts.get(sn, {}).get(code, {
            "row_key": code, "source_token": row["source_token"], "fields": {},
        }))
        edit.setdefault("edit_token", row.get("edit_token", 0))
        if row.get("verdict") in {"通过", "不通过", "中断"} and row["verdict"] != verdict:
            from PySide6.QtWidgets import QInputDialog

            reason, accepted = QInputDialog.getText(self, "记录改判原因", "修改已有结论的原因：")
            if not accepted or not reason.strip():
                self.notice.setText("改判未保存；正常采集不受影响。")
                return
            edit["reason"] = reason.strip()
        edit["fields"]["verdict"] = verdict
        notes = edit["fields"].get("notes", row.get("notes", ""))
        notes = "\n".join(line for line in notes.splitlines() if not line.startswith("本项人工确认："))
        edit["fields"]["notes"] = (notes + f"\n本项人工确认：{verdict}。").lstrip("\n")
        station = summary._station_saved

        def saved(_):
            summary._drafts.get(sn, {}).pop(code, None)
            self.refresh_local()
            self.notice.setText(f"SN：{sn} · {code} 已人工确认{verdict}，左侧状态与报告已更新，可直接导出。")

        self._job(lambda: self.store.save_report_edits(sn, [edit], station_id=station), saved,
                  progress=f"正在保存SN：{sn} · {code} 的人工结论：{verdict}…")

    def show_item_result(self, number):
        self.item.setCurrentIndex(self.item.findData(number))
        self.work_tabs.setCurrentIndex(2)
        code = {1: "SYS-001", 2: "SYS-005", 3: "SYS-006", 5: "SYS-011"}.get(number)
        if number == 4:
            code = MOTION_CASE_CODES[self.test_type.currentData()]
        table = self.result_summary.table
        for index in range(table.rowCount()):
            if table.item(index, 0).text() == code:
                table.selectRow(index)
                break
        table.setFocus()
        self.ensureWidgetVisible(self.result_summary)
        if number == 5 and self.model.currentData() == 28:
            self.notice.setText(f"当前SN：{self.sn or '未选择'} · 28轴不进行特技测试，第5项自动注明不适用，无需勾选完成或填写通过。")
            return
        self.notice.setText(f"当前SN：{self.sn or '未选择，请先打开SN'} · 第{number}项：{ITEM_NAMES[number]}。可在表格勾选“已完成”；板卡逐项结论自动保留，人工结论和备注可另行保存。")

    def _button(self, layout, text, action):
        button = QPushButton(text)
        button.clicked.connect(action)
        layout.addWidget(button)
        self.action_buttons.append(button)
        return button

    def _job(self, operation, complete, on_error=None, allow_closing=False, *, progress=None, after_success=None):
        if self.busy or getattr(getattr(self, "result_summary", None), "busy", False) or (self.closing and not allow_closing):
            return
        if progress:
            self.notice.setText(progress)
        self.busy = True
        self.workbench.set_busy(True)
        self.widget().setEnabled(False)
        for button in self.action_buttons:
            button.setEnabled(False)
        worker = FactoryJob(operation)
        _WORKERS.add(worker)
        succeeded = False
        def success(value):
            nonlocal succeeded
            if not self.closing:
                try:
                    complete(value)
                    succeeded = True
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    failure(exc)
            elif isinstance(value, tuple) and len(value) == 4 and value[-1] is not None:
                value[-1].close()
        def failure(exc):
            if not self.closing:
                from .ssh_transport import HostKeyRequired
                if isinstance(exc, SegmentImportError):
                    for index, row in enumerate(self.remote_rows):
                        if row["segment_id"] == exc.identifier:
                            if exc.sealed_info:
                                self.remote_rows[index] = exc.sealed_info
                                if self._motion_active_id == exc.identifier:
                                    self._motion_active_id = None
                            self.update_motion_controls()
                    if exc.sealed_info:
                        self.notice.setText(f"片段{exc.identifier}的记录已结束（{exc.sealed_info['state']}）；导入失败：{exc}。机器人原件保留，可重新导入；后续测试需新开记录段。电脑已有文件保留。")
                    else:
                        self.notice.setText(f"片段{exc.identifier}的结束结果待确认：{exc}。请接线刷新核对，不应假定仍在记录或已经结束；电脑已有文件保留。")
                elif isinstance(exc, HostKeyRequired):
                    self.notice.setText(f"首次连接 {exc.host}，观察到主机指纹：{exc.fingerprint}。请与机器人实际指纹核对后填入“已核对的SSH指纹”，再连接。")
                else:
                    self.notice.setText(f"操作未确认成功：{exc}。电脑已有文件保留；开始状态不明时请刷新核对或重试同一片段。")
                if on_error:
                    on_error(exc)
        def finished():
            self.busy = False
            self.workbench.set_busy(False)
            self.widget().setEnabled(not self.closing)
            for button in self.action_buttons:
                button.setEnabled(not self.closing)
            self.update_model_controls()
            _WORKERS.discard(worker)
            worker.deleteLater()
            if succeeded and after_success and not self.closing:
                after_success()
            QTimer.singleShot(0, self._drain_board_sync)
        worker.completed.connect(success)
        worker.failed.connect(failure)
        worker.finished.connect(finished)
        worker.start()

    def open_sn(self):
        if self.result_summary.busy:
            self.notice.setText("正在保存或导出当前结果，请完成后再切换SN。")
            return
        sn = self.sn_edit.currentText().strip()
        try:
            if sn not in {row["sn"] for row in self.store.roster()}:
                self.store.set_model_dof(sn, self.model.currentData())
            rows = self.store.results(sn)
            if not any(row["history"] for row in rows) and not self.store.list_segments(sn):
                self.store.save_result(sn, 1, "incomplete", "创建SN，待填写测试结果")
        except ValueError as exc:
            self.notice.setText(str(exc))
            return
        self.activate_sn(sn)

    def activate_sn(self, sn):
        if not self.result_summary.set_sn(sn):
            self.notice.setText("正在保存或导出当前结果，请完成后再切换SN。")
            return
        if self.sn != sn:
            self.plot.clear()
            self.metric_selector.clear()
            self.key_data_table.hide()
            self.key_data_note.setText("已切换SN，请选择本SN的记录查看关键统计。")
            self.curve_note.setText("已切换SN，请选择对应日志查看曲线。电脑原文件保留。")
        self.sn = sn
        self.model.blockSignals(True)
        self.model.setCurrentIndex(self.model.findData(self.store.model_dof(sn)))
        self.model.blockSignals(False)
        self.sn_edit.setEditText(sn)
        self.current_sn.setText(f"样品 SN：{sn} · RP1 / {self.store.model_dof(sn)}轴 · 本地测试工作台")
        self.refresh_local()
        self.select_item()
        self.sn_changed.emit(sn)

    def model_changed(self):
        if self.sn:
            try:
                self.store.set_model_dof(self.sn, self.model.currentData())
            except ValueError as error:
                self.notice.setText(str(error))
                self.model.blockSignals(True)
                self.model.setCurrentIndex(self.model.findData(self.store.model_dof(self.sn)))
                self.model.blockSignals(False)
            self.refresh_local()
            self.select_item()
        self.update_model_controls()

    def update_model_controls(self):
        if not hasattr(self, "workbench") or not hasattr(self, "completed"):
            return
        supported = self.model.currentData() == 24
        self.test_type.model().item(self.test_type.findData("stunt")).setEnabled(supported)
        applicable = not (not supported and self.item.currentData() == 5)
        self.completed.setEnabled(applicable)
        self.completed.setText("本项已完成" if applicable else "本项不适用（28轴）")
        self.status.setEnabled(applicable)
        self.save_result_button.setEnabled(applicable and not self.busy)
        for button in self.functional_buttons.values():
            button.setEnabled(bool(self.sn) and not self.busy and not self.closing)
        self.workbench.update_model_controls()

    def refresh_local(self):
        text = self.sn_edit.currentText()
        self.sn_edit.clear()
        self.sn_edit.addItems([row["sn"] for row in self.store.roster()])
        self.sn_edit.setEditText(text)
        if not self.sn:
            if hasattr(self, "workbench"):
                self.workbench.refresh_status([])
            return
        self.current_sn.setText(f"样品 SN：{self.sn} · RP1 / {self.model.currentData()}轴 · 本地测试工作台")
        self.local_rows = self.store.list_segments(self.sn)
        self.local_table.setRowCount(len(self.local_rows))
        for index, row in enumerate(self.local_rows):
            valid = row["valid_counts"]
            values = [f"{TYPES[row['test_type']]} / {'模拟数据·非实机' if row['source_kind']=='simulated' else 'ROS2·实机待验收'}", f"{row['started_at']} / {row['state']}", row["segment_id"], f"{valid.get('health',0)} / {valid.get('motion',0)}；缺失{len(row['missing_counts'])}项"]
            self._fill(self.local_table, index, values)
        self.result_rows = self.store.results(self.sn)
        pairing = self.result_rows[0]["estop_pairing"]
        labels = {"pass": "通过（人工）", "fail": "不通过（人工）", "incomplete": "未测试"}
        latest_pairing = pairing["latest"] or {}
        self.estop_pairing_note.setText(f"当前SN：{self.sn} · {labels[pairing['status']]}"
            + (f" · {latest_pairing['observed_at']} · 共{len(pairing['history'])}次记录" if latest_pairing else ""))
        self.estop_pairing_history.setRowCount(len(pairing["history"]))
        for index, entry in enumerate(pairing["history"]):
            self._fill(self.estop_pairing_history, index, [entry["observed_at"], labels[entry["status"]]])
        self.result_table.blockSignals(True)
        self.result_table.setRowCount(5)
        for index, row in enumerate(self.result_rows):
            self._fill(self.result_table, index, [f"{row['item']}. {row['name']}", "已完成" if row["completed"] else "未完成", STATUS_NAMES[row["status"]] if row["status"] != "incomplete" else "未填写结论 / 待复核", (row["latest"] or {}).get("notes", "")])
            check = self.result_table.item(index, 1)
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked if row["completed"] else Qt.CheckState.Unchecked)
            if row["status"] == "not_applicable":
                check.setText("不适用")
                check.setFlags(Qt.ItemFlag.ItemIsSelectable)
        self.result_table.blockSignals(False)
        self.render_functional_checks()
        self.update_model_controls()
        board = self.result_rows[0].get("board")
        self.board_table.setVisible(bool(board))
        if board:
            counts = board["counts"]
            assessment = board.get("assessment") or {}
            source = "模拟数据 · 非实机" if board["source_kind"] == "simulated" else "ROS2 板端观测"
            unfinished = counts['error'] + counts['needs_review'] + counts['skip']
            summary = f"板卡已有结果 {board['executed']}/{board['total']}：通过 {counts['pass']}，不通过 {counts['fail']}，未完成 {unfinished}，未检查 {counts['unchecked']}"
            if assessment:
                total = board.get("display_counts") or assessment["counts"]
                summary = f"板卡已有结果 {board['executed']}/{board['total']}：通过 {total['通过']}，不通过 {total['不通过']}，未完成 {total['未完成']}，未检查 {total['未检查']}"
            self.board_evidence_note.setText(f"{summary} · {board['robot_id']} · {source}。逐项快照已在电脑保存；灯板实际亮灯、电机参数仍按原流程确认。")
            self.result_table.item(0, 3).setText(summary + "；" + (self.result_rows[0]["latest"] or {}).get("notes", ""))
            self.board_table.setRowCount(len(board["checks"]))
            for index, check in enumerate(board["checks"]):
                latest = check.get("latest") or {}
                saved_view = assessment.get("checks", {}).get(check["id"])
                view = BoardCheckView(**saved_view) if saved_view else board_check_view(check["id"], latest)
                self._fill(self.board_table, index, [f"{BOARD_CASE_CODES[check['id']]} · {check['label']}（{check['id']}）", view.label, view.summary, latest.get("created_at", "—"), source])
                for column in (1, 2):
                    self.board_table.item(index, column).setToolTip(view.text())
        else:
            self.board_evidence_note.setText("板卡结果：尚未关联。硬件自检页可直接保存现有结果，无需手工添加报告附件。")
        lights = self.result_rows[0]["lights"]
        if lights:
            last = lights[-1]
            light_summary = f"灯板人工确认：{'亮了' if last['lit'] else '没亮'} · {last['observed_at']}"
            self.light_stage_note.setText(light_summary)
            self.board_evidence_note.setText(self.board_evidence_note.text() + "\n" + light_summary)
            self.result_table.item(0, 3).setText(self.result_table.item(0, 3).text() + "；" + light_summary)
        else:
            self.light_stage_note.setText("灯板现场确认：尚未记录。")
        confirmations = self.result_rows[1]["calibration_confirmations"]
        if confirmations:
            last = confirmations[-1]
            summary = f"整机标定人工确认：{'已完成' if last['completed'] else '中断 / 未完成'} · {last['observed_at']}；逐电机结果未知"
            self.calibration_confirmation_note.setText(summary)
            self.result_table.item(1, 3).setText(summary + "；" + (self.result_rows[1]["latest"] or {}).get("notes", ""))
        else:
            self.calibration_confirmation_note.setText("整机标定：尚未确认。")
        check = self.result_rows[2].get("calibration")
        angle_check = self.result_rows[2].get("angle_calibration")
        encoder = bool(angle_check and not check)
        check = check or angle_check
        self.calibration_table.setVisible(bool(check))
        if check:
            rows = check["report"]["results"]
            labels = {"passed": "合格", "failed": "不合格", "skipped": "已跳过", "pending": "未测", "active": "检查中", "interrupted": "已中断"}
            if encoder:
                labels["passed"] = "角度达标"
            counts = {status: sum(row["status"] == status for row in rows) for status in labels}
            title = "只读编码器角度检查" if encoder else "标定检查"
            summary = f"{title}：{labels['passed']} {counts['passed']}/{len(rows)}，跳过 {counts['skipped']}，不合格 {counts['failed']}，未完成 {counts['pending'] + counts['active'] + counts['interrupted']}"
            saved_summary = f"{summary} · 机器人 {check['robot_id']} · 本机已保存。"
            if angle_check and not encoder:
                saved_summary += " 只读编码器记录见「查看结果」。"
            self.calibration_note.setText(saved_summary + " 逐项结果与人工结论分别保留。")
            self.calibration_stage_note.setText(saved_summary)
            self.result_table.item(2, 3).setText(summary + "；" + (self.result_rows[2]["latest"] or {}).get("notes", ""))
            self.calibration_table.horizontalHeaderItem(3).setText("实测 / °" if encoder else "实测峰值 / °")
            self.calibration_table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                attempt = next((a for a in reversed(row["attempts"]) if a["attempt_id"] == row["effective_attempt_id"]), {})
                angle = attempt.get("current_angle_deg" if encoder and row["status"] != "failed" else "peak_angle_deg")
                measured = f"{angle:.2f}" if isinstance(angle, (int, float)) else "未知"
                error = f"{angle - row['reference_deg']:+.2f}" if isinstance(angle, (int, float)) else "未知"
                self._fill(self.calibration_table, index, [row["joint"], {"lower": "下限", "upper": "上限", "center": "插销零位" if row.get("check_mode") == "pin_zero" else "零位"}.get(row["direction"], row["direction"]), f"{row['reference_deg']:.2f}", measured, error, labels[row["status"]]])
        else:
            self.calibration_note.setText("标定检查：尚未关联。请先选SN；已有检查可导入本机JSON。")
            self.calibration_stage_note.setText("标定检查：尚未关联；先选SN，已有结果可导入。")
        zero_check = self.result_rows[2].get("zero_reference_check")
        if zero_check:
            rows = zero_check["report"]["results"]
            passed = sum(row["status"] == "passed" for row in rows)
            summary = f"标零位复查：达标 {passed}/{len(rows)} 项 · 机器人 {zero_check['robot_id']} · 详见「查看结果」。"
            if check:
                summary = self.calibration_stage_note.text() + "\n" + summary
            self.calibration_stage_note.setText(summary)

        if self.result_summary.sn != self.sn:
            self.result_summary.set_sn(self.sn)
        else:
            self.result_summary.refresh()
        self.workbench.refresh_status(self.result_summary._rows)

    def _summary_busy_changed(self, busy):
        enabled = not busy and not self.busy and not self.closing
        self.sn_edit.setEnabled(enabled)
        self.model.setEnabled(enabled)
        self.result_table.setEnabled(enabled)
        for button in self.action_buttons:
            button.setEnabled(enabled)
        self.workbench.set_busy(not enabled)
        if not busy:
            self.update_model_controls()
            self.workbench.refresh_status(self.result_summary._rows)
            QTimer.singleShot(0, self._drain_board_sync)

    def import_calibration_check(self):
        if not self.sn:
            self.notice.setText("请先打开或新建整机SN，再导入这台机器人的标定检查结果。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择本机标定检查结果", str(self.store.directory / "calibration-checks"), "JSON (*.json)")
        if path:
            self.sync_calibration_check(self.sn, path)

    def render_functional_checks(self):
        checks = self.result_rows[3]["functional_checks"]
        labels = {"pass": "通过（人工）", "fail": "不通过（人工）", "incomplete": "未完成"}
        summary = "；".join(f"{row['name']}：{labels[row['status']]}" for row in checks.values())
        self.functional_stage_note.setText(summary)
        self.functional_result_table.setRowCount(len(checks))
        for index, row in enumerate(checks.values()):
            latest = row["latest"] or {}
            self._fill(self.functional_result_table, index, [f"{FUNCTION_CASE_CODES[row['kind']]} · {row['name']}", labels[row["status"]],
                latest.get("observed_at", "尚未记录"),
                f"{len(latest.get('segment_ids', []))}段 / {len(latest.get('soc_observation_ids', []))}张SOC截图；{latest.get('notes', '')}"])
        observations = self.store.soc_observations(self.sn) if self.sn else []
        self.soc_table.setRowCount(len(observations))
        for index, entry in enumerate(observations):
            stage = entry.get("stage")
            previous = entry.get("previous_soc")
            recorded_at = entry["sampled_at"]
            if stage == "backup":
                change = "小电池 · 无SOC"
                if entry["soc"] is not None:
                    change += "（历史BMS读数未归属）"
                recorded_at = entry.get("created_at") or recorded_at
            elif stage in BATTERY_SWAP_STAGES:
                change = f"{BATTERY_SWAP_STAGES[stage]} · {entry['soc']:g}%"
            else:
                event = {"baseline": "起始", "change": "变化", "final": "结束快照"}.get(entry["event"], "观测")
                reading = f"{entry['soc']:g}%" if previous is None else f"{previous:g}% → {entry['soc']:g}%"
                change = f"BMS {event} · {reading}（阶段未标记）"
            source = "SIMULATED · 模拟数据" if entry["source_kind"] == "simulated" else "ROS2观测"
            self._fill(self.soc_table, index, [f"{FUNCTION_CASE_CODES['battery_hot_swap']} · {recorded_at}",
                change, f"{entry['robot_id']} / {source}", entry["continuity"]])
            self.soc_table.item(index, 0).setData(Qt.ItemDataRole.UserRole, entry)
        current = self.result_table.item(3, 3)
        current.setText(summary + "；" + current.text())

    def show_test_catalog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("测试编号目录 · 整机与独立测试")
        dialog.resize(860, 650)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_text("整机为SYS-001～012，其中SYS-001统一包含17项基础自检，SYS-010合并行走／跑步，SYS-012为远程急停开关对频测试，原始记录类型与证据保留。单元老化为UNIT-AGING-001～007；串联为CHAIN-001，独立主控为CTRL-001，硬件通电为HW-POWER-001。各体系分别编号。"))
        table = _table(["整机测试编号", "分类", "测试项目", "底层来源", "适用状态"])
        rows = test_catalog(self.model.currentData())
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            children = row.get("subchecks", [])
            self._fill(table, index, [row["code"], CATEGORY_NAMES[row["category"]], row["name"],
                f"{len(children)}个检查子项" if children else row["source_id"], "适用" if row["applicable"] else "28轴不适用"])
            if children:
                table.item(index, 3).setToolTip("\n".join(f"{child['source_id']} · {child['name']}" for child in children))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        layout.addWidget(_text("独立测试编号参考：本页仅列编号，整机结果仍按整机SN归档。"))
        standalone = _table(["测试编号", "测试体系", "单元 / 项目"])
        standalone.setMaximumHeight(265)
        independent_rows = standalone_catalog()
        standalone.setRowCount(len(independent_rows))
        for index, row in enumerate(independent_rows):
            self._fill(standalone, index, [row["code"], TEST_FAMILIES[row["family"]], row["name"]])
        standalone.resizeColumnsToContents()
        layout.addWidget(standalone)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def show_soc_screenshot(self):
        row = self.soc_table.currentRow()
        if row < 0:
            self.notice.setText("请先选择一条SOC观测记录。")
            return
        entry = self.soc_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        path = self.store.attachments / entry["attachment_id"]
        try:
            if path.is_symlink() or path.stat().st_size > 4 * 1024**2:
                raise ValueError("截图文件类型或大小无效")
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["screenshot"]["sha256"]:
                raise ValueError("截图校验不一致")
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                raise ValueError("截图无法显示")
        except (OSError, ValueError) as exc:
            self.notice.setText(f"SOC截图读取失败：{exc}；原记录保留。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{FUNCTION_CASE_CODES['battery_hot_swap']} · SOC截图 · {self.sn}")
        layout = QVBoxLayout(dialog)
        image = QLabel()
        image.setPixmap(pixmap.scaled(900, 650, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(image)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def confirm_functional_check(self, kind):
        if self.busy or self.closing:
            return
        sn = self.sn
        if not sn:
            self.notice.setText("请先打开对应整机SN，再记录急停或热换电测试结果。")
            return
        if kind == "remote_estop":
            self.confirm_remote_estop(sn)
            return
        from .desktop_battery_swap import BatterySwapDialog
        monitor = BatterySwapDialog(sn, self.store, self)
        self.battery_swap_dialog = monitor
        if self.live_frame is not None and self.live_frame_received is not None and time.monotonic() - self.live_frame_received < 2:
            monitor.set_frame(self.live_frame, received_monotonic=self.live_frame_received)
        try:
            accepted = monitor.exec() == QDialog.DialogCode.Accepted
        finally:
            self.battery_swap_dialog = None
        observation_ids = list(monitor.observation_ids)
        monitor.deleteLater()
        if self.closing or self.closed:
            return
        if monitor.target_error:
            self.refresh_local()
            self.notice.setText(monitor.target_error)
            return
        if not accepted:
            self.refresh_local()
            self.notice.setText(f"SOC监听已关闭；已保存的{len(observation_ids)}张截图保留在SN：{sn}，未填写换电结论。")
            return
        if self.sn != sn:
            self.notice.setText(f"SN已变化，SOC截图仍保存在原SN：{sn}；换电结论未保存。")
            return
        title = FUNCTIONAL_CHECK_NAMES[kind]
        guidance = "热换电允许全程连接网线，实时观察主控运行和已有反馈；无需为换电断开网线。要求主控全程不断电、无重启，并核对换电后供电、BMS通信及运行状态正常。主控断电或重启不符合本项要求；请记录实际换电时间和异常。主控或监听重启时，原记录段中断，停机时间不计入两小时。"
        criterion = "已实际换电，主控全程不断电、无重启，通信及运行状态正常"
        segment_ids = [row["segment_id"] for row in self.store.list_segments(sn) if row["test_type"] != "stunt"]
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{title} · 人工测试记录")
        dialog.setObjectName("factoryFunctionalDialog")
        dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        palette = dialog.palette()
        for role, color in ((QPalette.ColorRole.Window, "#eef2fa"), (QPalette.ColorRole.Base, "#ffffff"),
                            (QPalette.ColorRole.WindowText, "#142441"), (QPalette.ColorRole.Text, "#142441")):
            palette.setColor(role, QColor(color))
        dialog.setPalette(palette)
        dialog.setAutoFillBackground(True)
        dialog.setStyleSheet("""
            QDialog#factoryFunctionalDialog { background:#eef2fa; color:#142441; }
            QDialog#factoryFunctionalDialog QLabel { background:transparent; color:#142441; }
            QDialog#factoryFunctionalDialog QTextEdit, QDialog#factoryFunctionalDialog QComboBox {
                background:#ffffff; color:#142441; }
        """)
        dialog.setMinimumWidth(540)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_text(f"SN：{sn} · 第4项 · {title}"))
        layout.addWidget(_text(guidance))
        layout.addWidget(_text(f"本次已保存{len(observation_ids)}张SOC截图。SOC变化只作为证据，热换电通过条件保持不变。"))
        layout.addWidget(_text("本窗口只保存人工记录，不发送急停、电源、电机或策略命令。"))
        result = QComboBox()
        result.setObjectName("functionalCheckStatus")
        result.setAccessibleName("本次功能测试人工结论")
        for value, label in (("incomplete", "未完成 / 待复核"), ("pass", "通过（人工确认）"), ("fail", "不通过（人工确认）")):
            result.addItem(label, value)
        form = QFormLayout()
        form.addRow("本次结论", result)
        layout.addLayout(form)
        notes = QTextEdit()
        notes.setObjectName("functionalCheckNotes")
        notes.setAccessibleName("功能测试现场观察与备注")
        notes.setPlaceholderText("实际操作时间、触发装置或换电方式、观察结果、异常及复测说明")
        notes.setMaximumHeight(110)
        layout.addWidget(_text("现场观察 / 备注"))
        layout.addWidget(notes)
        confirmation = QCheckBox(criterion)
        confirmation.setObjectName("functionalCheckObservationConfirmed")
        layout.addWidget(confirmation)
        validation = _text("")
        validation.setStyleSheet("color:#a52b35;")
        validation.hide()
        layout.addWidget(validation)
        link = QCheckBox(f"关联本SN第4项已读取日志（{len(segment_ids)}段）")
        link.setObjectName("functionalCheckLinkEvidence")
        link.setChecked(bool(segment_ids))
        link.setEnabled(bool(segment_ids))
        layout.addWidget(link)
        layout.addWidget(_text("关联日志不代表自动识别测试事件；尚未读取日志时，可先保存观察，读取后再补充记录。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存测试记录")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        def accept():
            if result.currentData() == "pass" and not confirmation.isChecked():
                validation.setText("选择“通过”前，请确认上述实际观察；未确认可保存为未完成或不通过。")
                validation.show()
                return
            dialog.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self.sn != sn:
            self.notice.setText("SN已变化，本次功能测试记录未保存，请核对后重试。")
            return
        status = result.currentData()
        if status == "pass" and not confirmation.isChecked():
            self.notice.setText(f"{title}尚未确认关键现场表现，未保存为通过。")
            return
        text = f"现场要求：{criterion}。人工确认：{'已确认' if confirmation.isChecked() else '未确认'}。\n" + notes.toPlainText()
        ids = segment_ids if link.isChecked() else []
        def saved(_):
            self.refresh_local()
            self.show_case_result(FUNCTION_CASE_CODES[kind])
            self.notice.setText(f"已保存SN：{sn} 的{title}人工结论：{STATUS_NAMES[status]}；完整复测历史进入第4项报告。")
        if monitor.interrupted or monitor.reboot_observed:
            text += "\nSOC监听备注：" + ("观察到开机时长回退。" if monitor.reboot_observed else "曾断流或采集流切换。")
        if not observation_ids:
            text += "\n未取得有效SOC截图，以上为人员现场结论。"
        if monitor.error:
            text += f"\nSOC截图保存异常：{monitor.error}。"
        self._job(lambda: self.store.save_functional_check(sn, kind, status, text, ids,
                  soc_observation_ids=observation_ids), saved)

    def record_estop_pairing(self, status):
        sn = self.sn
        if not sn:
            self.notice.setText("请先打开对应整机SN，再记录远程急停开关对频测试结果。")
            return
        def saved(_):
            self.refresh_local()
            label = "通过" if status == "pass" else "不通过"
            self.notice.setText(f"已保存SN：{sn} 的{ESTOP_PAIRING_CASE_CODE}远程急停开关对频测试：{label}（人工）。复测历史保留。")
        self._job(lambda: self.store.save_estop_pairing(sn, status), saved)

    def confirm_remote_estop(self, sn):
        box = QMessageBox(QMessageBox.Icon.Question, "确认远程急停结果",
            f"SN：{sn}\n请根据已完成的现场急停测试确认结果。\n急停切断电机电源，主控保持供电。", parent=self)
        normal = box.addButton("正常", QMessageBox.ButtonRole.YesRole)
        abnormal = box.addButton("异常", QMessageBox.ButtonRole.NoRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        answer = box.clickedButton()
        if answer not in (normal, abnormal):
            return
        if self.sn != sn:
            self.notice.setText("SN已变化，本次急停确认未保存，请核对后重试。")
            return
        status = "pass" if answer == normal else "fail"
        def saved(_):
            self.refresh_local()
            self.notice.setText(f"已保存SN：{sn} 的远程急停人工确认：{'正常' if status == 'pass' else '异常'}。")
        self._job(lambda: self.store.save_functional_check(sn, "remote_estop", status,
            "人员点击确认现场急停测试结果；急停切断电机电源，主控保持供电。"), saved)

    def set_frame(self, frame):
        if self.closing or self.closed:
            return
        self.live_frame = frame
        self.live_frame_received = time.monotonic()
        if hasattr(self, "workbench"):
            self.workbench.refresh_status()
        if self.battery_swap_dialog:
            self.battery_swap_dialog.set_frame(frame)

    def mark_disconnected(self):
        self.live_frame = None
        self.live_frame_received = None
        if self.tunnel is None:
            self._motion_epoch += 1
            self._motion_problem = "主界面连接已断开"
            self._motion_next_poll = 0.0
        if hasattr(self, "workbench"):
            self.workbench.refresh_status()
            self.update_motion_controls()
        if self.battery_swap_dialog:
            self.battery_swap_dialog.mark_disconnected()

    def test_light(self):
        sn = self.sn
        if not sn:
            self.notice.setText("请先打开对应整机SN，再测试灯板。")
            return
        context = self.deploy_context_provider()
        robot_id = context.get("robot_id")
        if (not context.get("connected") or not robot_id or not context.get("relay_url")
                or not context.get("station_url") or not context.get("station_token_file")):
            self.notice.setText("请先在设备设置通过「添加后台机器人」连接当前SN对应的实机。")
            return
        binding = self.store.sn_identity(sn)
        if binding and (binding["robot_id"], binding["source_kind"]) != (robot_id, "ros2"):
            self.notice.setText("当前SN与连接的实机身份不一致，未发送点灯命令。")
            return
        target_keys = ("robot_id", "relay_url", "generation", "station_url", "station_token_file")

        def same_target():
            current = self.deploy_context_provider()
            return (self.sn == sn and current.get("connected")
                    and all(current.get(key) == context.get(key) for key in target_keys))

        def dispatch(password=None):
            status = None

            def completed(value):
                nonlocal status
                status = value

            def confirm():
                if not same_target():
                    self.notice.setText("SN或机器人连接已变化，本次灯板结果未记录。")
                    return
                if status == "password_required":
                    entered, ok = QInputDialog.getText(
                        self, "授权灯板测试", f"{robot_id} 需要 sudo 密码。密码仅用于本次点灯。",
                        QLineEdit.EchoMode.Password,
                    )
                    if ok and entered and same_target():
                        dispatch(entered)
                    else:
                        self.notice.setText("本次点灯授权未完成，未保存灯板结论。")
                    return
                if status == "command_sent":
                    self.confirm_light(sn, robot_id, same_target)

            self._job(lambda: _send_light_test(context, password), completed,
                      on_error=lambda exc: self.notice.setText(f"点灯未确认：{exc}。本次未保存灯板结论。"),
                      progress=f"正在给 {robot_id} 的灯板发送点灯命令…", after_success=confirm)

        dispatch()

    def confirm_light(self, sn, robot_id, same_target):
        box = QMessageBox(QMessageBox.Icon.Question, "确认灯板亮灯",
                          f"SN：{sn}\n机器人：{robot_id}\n已收到点灯命令的成功回执。请现场观察，灯板实际亮了吗？",
                          parent=self)
        yes = box.addButton("亮了", QMessageBox.ButtonRole.YesRole)
        no = box.addButton("没亮", QMessageBox.ButtonRole.NoRole)
        cancel = box.addButton("暂不确认", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        answer = box.clickedButton()
        if answer not in (yes, no):
            return
        if not same_target():
            self.notice.setText("SN或机器人连接已变化，本次灯板观察未保存。")
            return
        lit = answer == yes
        def complete(_):
            if self.sn == sn:
                self.refresh_local()
            self.notice.setText(f"已保存SN：{sn} 的灯板现场确认：{'亮了' if lit else '没亮'}。")
        self._job(lambda: self.store.save_light_observation(sn, lit), complete)

    def confirm_calibration(self):
        sn = self.sn
        if not sn:
            self.notice.setText("请先打开对应整机SN，再确认标定状态。")
            return
        box = QMessageBox(QMessageBox.Icon.Question, "确认整机标定状态", f"SN：{sn}\n请根据现场标定操作确认流程状态。\n逐电机写零结果未知，需要后续检查；本次确认不会判定通过。", parent=self)
        yes = box.addButton("已完成", QMessageBox.ButtonRole.YesRole)
        no = box.addButton("中断 / 未完成", QMessageBox.ButtonRole.NoRole)
        cancel = box.addButton("暂不确认", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        answer = box.clickedButton()
        if answer not in (yes, no):
            return
        if self.sn != sn:
            self.notice.setText("SN已变化，本次标定确认未保存，请核对后重新确认。")
            return
        completed = answer == yes
        def complete(_):
            if self.sn == sn:
                self.refresh_local()
                if self.item.currentData() == 2:
                    self.completed.setChecked(completed)
                    self.status.setCurrentIndex(self.status.findData("incomplete"))
            self.notice.setText(f"已保存SN：{sn} 的整机标定人工确认：{'已完成' if completed else '中断 / 未完成'}；逐电机结果未知。")
        self._job(lambda: self.store.save_calibration_confirmation(sn, completed), complete)

    def sync_calibration_check(self, sn, path, expected_robot=None, done=None):
        if self.busy or self.closing:
            self.notice.setText("本机正在处理其他任务；标定检查原文件已保留，可稍后导入。")
            if done:
                done(False, self.notice.text())
            return
        def success(result):
            if self.sn == sn:
                self.refresh_local()
            self.notice.setText(f"标定检查已保存到SN：{sn} · {result['robot_id']}，可在第3项查看逐关节结果。")
            if done:
                done(True, self.notice.text())
        def failure(exc):
            message = f"标定检查未关联：{exc}。原始JSON保留，可核对后导入。"
            self.notice.setText(message)
            if done:
                done(False, message)
        self._job(lambda: self.store.save_calibration_check(sn, path, expected_robot), success, failure)

    def sync_angle_calibration_check(self, sn, data, expected_robot, done):
        if self.busy or self.closing or self.result_summary.busy:
            message = "本机正在处理其他任务，请稍后重试保存角度记录。"
            self.notice.setText(message)
            done(False, message)
            return

        def finish(ok, message):
            self.notice.setText(message)
            done(ok, message)

        def success(result):
            if self.sn == sn:
                self.refresh_local()
            finish(True, f"只读角度记录已保存到 SN：{sn} · {result['robot_id']}。可在 SYS-006 的「查看结果」查看。")

        def failure(exc):
            finish(False, f"角度记录未能保存：{exc}")

        self._job(lambda: self.store.save_angle_calibration_check(sn, data, expected_robot), success, failure)

    def save_zero_reference_check(self, sn, data, expected_robot):
        if self.busy or self.closing or self.result_summary.busy:
            raise ValueError("本机正在处理其他任务，请稍后重试保存标零位复查记录")
        result = self.store.save_zero_reference_check(sn, data, expected_robot)
        if self.sn == sn:
            self.refresh_local()
        self.notice.setText(f"标零位复查已保存到 SN：{sn} · {result['robot_id']}。可在 SYS-006 的「查看结果」查看。")
        return True

    def completion_changed(self, changed):
        if changed.column() != 1 or not self.sn or self.busy or self.closing:
            return
        sn, item = self.sn, changed.row() + 1
        completed = changed.checkState() == Qt.CheckState.Checked
        def saved(_):
            self.refresh_local()
            if self.sn == sn and self.item.currentData() == item:
                self.completed.setChecked(completed)
            self.notice.setText(f"{sn} 第{item}项已保存为{'已完成' if completed else '未完成'}；实际检查结论保持，缺失项仍可继续。")
        def operation():
            if completed and item >= 4 and not self.store.item_duration_satisfied(sn, item):
                raise ValueError("本项记录尚未全部超过2小时，不能标记完成；短记录和历史结论仍保留")
            return self.store.set_completed(sn, item, completed)
        self._job(operation, saved, lambda _: self.refresh_local())

    def save_board_profile(self, values):
        if self.busy or self.closing:
            raise ValueError("本地日志正在读写，请完成后再保存测试标准")
        self.board_profile_values = self.store.save_board_profile(values)
        return self.board_profile_values

    def sync_board_snapshot(self, sn, robot_id, source_kind, reports, profile=None):
        """Keep the SN captured by the caller even when another local job is running."""
        if self.closing or not sn or not reports:
            return
        self.board_queue.append((sn, robot_id, source_kind, json.loads(json.dumps(reports)), json.loads(json.dumps(profile or self.board_profile_values))))
        self._drain_board_sync()

    def _drain_board_sync(self):
        if self.busy or self.result_summary.busy or not self.board_queue or self.closed:
            return
        sn, robot_id, source_kind, reports, profile = self.board_queue.pop(0)
        # shutdown waits for queued evidence writes, without making a robot request.
        def saved(board):
            if self.sn == sn:
                self.refresh_local()
            self.notice.setText(f"{sn} 的板卡结果已保存到电脑；原始时间、通过、不符合和待复核结论全部保留。")
            self.board_saved.emit(sn, board)
        def failed(exc):
            self.board_saved.emit(sn, {"error": str(exc)})
        self._job(lambda: self.store.save_board_snapshot(sn, robot_id, source_kind, reports, profile), saved, failed, allow_closing=True)

    @staticmethod
    def _fill(table, row, values):
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(str(value)))

    def select_item(self):
        self.attachments = []
        self.functional_result_box.setVisible(self.item.currentData() == 4)
        self.attachment_note.setText("可附现有工具导出的报告、标定记录和照片。")
        if self.sn:
            self.results_box.setTitle(f"SN：{self.sn} · 整机测试结果与报告 · 当前最终版与原始证据")
            row = self.store.results(self.sn)[self.item.currentData()-1]
            self.status.setCurrentIndex(self.status.findData(row["status"]))
            self.completed.setChecked(row["completed"])
            self.notes.setPlainText((row["latest"] or {}).get("notes", ""))
            self.update_model_controls()

    def use_current(self):
        self._motion_paused = False
        self._poll_motion(force=True, main_only=True)

    def connect_ssh(self):
        from .factory_client import FactoryClient
        from .ssh_transport import SshTunnel
        target, password, port, fingerprint = self.target.text(), self.password.text(), self.port.value(), self.trust.text().strip()
        self.password.clear()
        self._motion_paused = False
        self._motion_epoch += 1
        def operation():
            tunnel = SshTunnel.connect(target, password, self.store.directory / "known_hosts", collector_port=port, confirmed_fingerprint=fingerprint)
            try:
                client = FactoryClient(tunnel.url)
                tunnel.deploy_binding = dict(target=target, known_hosts=self.store.directory / "known_hosts", ssh_port=22)
                return client, client.probe(), client.list_segments(), tunnel
            except Exception:
                tunnel.close()
                raise
        self._job(operation, self.connected, progress="正在通过独立SSH连接机器人动作日志服务，请稍候…")

    def open_motor_parameters(self, section="parameters"):
        if self.busy or self.closing:
            return
        if not self.sn:
            self.notice.setText("先打开整机SN，再维护对应机器人的电机。")
        self.tool_requested.emit(f"motor_{section}", 1)

    def motor_maintenance_context(self):
        context = self.deploy_context_provider()
        if self.tunnel and self.identity and hasattr(self.tunnel, "deploy_binding"):
            context = {**self.tunnel.deploy_binding, "connected": True, "robot_id": self.identity["robot_id"],
                       "relay_url": self.client.url, "tunnel": self.tunnel}
        return context

    def validate_motor_maintenance_context(self, context):
        sn = self.sn
        frame = self.live_frame or {}
        if not sn:
            raise ValueError("请先在整机测试打开本次样品的SN。")
        if (not context.get("connected") or not context.get("robot_id") or not context.get("target")
                or context.get("relay_url", "").rstrip("/") != self.source_provider().rstrip("/")):
            raise ValueError("请先在设备设置连接当前SN对应的机器人。")
        if (self.live_frame_received is None or time.monotonic() - self.live_frame_received > 2
                or frame.get("upload", {}).get("collector_connected") is False):
            raise ValueError("等待机器人采集端的实时数据，请核对设备连接。")
        if frame.get("robot_id") != context["robot_id"] or frame.get("source_kind") != "ros2":
            raise ValueError("采集端的机器人身份或来源与维护目标不一致。")
        bound = self.store.sn_identity(sn)
        if bound and (bound["robot_id"] != context["robot_id"] or bound["source_kind"] != "ros2"):
            raise ValueError("此SN绑定的机器人与当前连接不一致。")

    def show_motor_parameter_report(self, sn, section="parameters"):
        """Open the already-archived check; never query hardware or duplicate evidence."""
        if sn != self.sn or self.result_summary.busy or self.closing:
            self.notice.setText("请先打开检查对应的SN并等待当前报告操作结束。")
            return
        self.refresh_local()
        if section == "firmware":
            self.show_case_result("SYS-003")
            return
        self.show_case_result(MOTOR_PARAMETERS_CASE_CODE)
        self.notice.setText(f"SN：{sn} · 电机检查已自动加入SYS-003固件与SYS-004参数报告。"
                            "可编辑当前最终版后导出CSV，或导出原始HTML / 明细CSV查看逐电机参数。")

    def save_motor_firmware_report(self, sn, request, result, *, board_test_profile=None):
        profile = self.board_profile_values if board_test_profile is None else board_test_profile
        path = self._write_motor_receipt(
            "motor-firmware", f"motor-firmware-{uuid4()}",
            dict(sn=sn, request=request, result=result, board_test_profile=profile),
        )
        self._attach_motor_receipt(sn, path)
        self.notice.setText(f"SN：{sn} · 电机固件维护回执已保存到第1项附件。")

    def save_motor_parameter_report(self, sn, request, result, *, board_test_profile=None):
        profile = self.board_profile_values if board_test_profile is None else board_test_profile
        path = self._write_motor_receipt(
            "motor-parameters", f"{request['request_id']}-{uuid4()}",
            dict(sn=sn, request=request, result=result, board_test_profile=profile),
        )
        status = completed = None
        note = ""
        if result["state"] == "checked":
            rows = result["rows"]
            different = sum(row["status"] == "different" for row in rows)
            unreadable = sum(row["status"] == "unreadable" for row in rows)
            note = f"电机MIT量程检查：一致{len(rows)-different-unreadable}，差异{different}，未完成{unreadable}；固件版本与其他Pn参数未判定合规。"
            if different or unreadable:
                status, completed = ("fail" if different else "incomplete"), False
        elif request["operation"] in {"update", "rewrite"}:
            status, completed = "incomplete", False
            note = f"电机量程更新：{result['state']}；需重新检查，未自动判定第1项通过。"
        self._attach_motor_receipt(sn, path, note=note, status=status, completed=completed)
        self.notice.setText(f"SN：{sn} · 电机参数记录已保存；" + (note or "原值先保存在电脑。"))

    def _write_motor_receipt(self, subdirectory, filename, payload):
        directory = self.store.directory / subdirectory
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / f"{filename}.json"
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path

    def _attach_motor_receipt(self, sn, path, *, note="", status=None, completed=None):
        attachment = self.store.add_attachment(sn, 1, path)
        previous = self.store.results(sn)[0]["latest"] or {}
        if status is None:
            status = previous.get("status", "incomplete")
        if completed is None:
            completed = previous.get("completed", False)
        notes = previous.get("notes", "") + ("\n" + note if note else "")
        self.store.save_result(sn, 1, status, notes, previous.get("segment_ids", []),
                               [*previous.get("attachment_ids", []), attachment["attachment_id"]], completed=completed)
        self.refresh_local()

    def connected(self, value):
        client, identity, rows, tunnel = value
        if self.tunnel and self.tunnel is not tunnel:
            self.tunnel.close()
        self.client, self.identity, self.tunnel = client, identity, tunnel
        self.render_remote(rows)

    def refresh_remote(self):
        self._poll_motion(force=True)

    def _motion_target(self, *, main_only=False):
        if self.tunnel is not None and not main_only:
            return (self.client.url, self.identity["robot_id"], self.identity["source_kind"], self.tunnel)
        frame = self.live_frame
        received = self.live_frame_received
        if frame is None or received is None or time.monotonic() - received > 3:
            return None
        if not isinstance(frame, dict):
            frame = frame.model_dump()
        if frame.get("upload", {}).get("collector_connected") is False:
            return None
        url = self.source_provider().rstrip("/")
        if url and frame.get("robot_id") and frame.get("source_kind"):
            return (url, frame["robot_id"], frame["source_kind"], None)
        return None

    def _poll_motion(self, *, force=False, main_only=False):
        from .factory_client import FactoryClient

        if self.busy or self.closing or self._motion_worker is not None:
            return
        if not force and (self._motion_paused or not self.motion_record_box.isVisible()
                          or time.monotonic() < self._motion_next_poll):
            return
        target = self._motion_target(main_only=main_only)
        if target is None:
            if force:
                self.notice.setText("请在设备设置连接机器人，或使用高级设置中的独立 SSH。")
            return
        self._motion_next_poll = time.monotonic() + 3
        epoch = self._motion_epoch
        url, robot_id, source_kind, tunnel = target
        client = self.client if self.client and self.client.url == url else None
        identity = self.identity or {}
        if (identity.get("robot_id"), identity.get("source_kind")) != (robot_id, source_kind):
            client = None

        def operation():
            connection = client or FactoryClient(url)
            listing = connection.list_segments()
            if (connection.robot_id, connection.source_kind) != (robot_id, source_kind):
                raise ValueError("动作记录服务与主界面的机器人身份不一致，请核对设备连接。")
            return connection, {"robot_id": robot_id, "source_kind": source_kind}, listing, tunnel

        def current():
            return (not self.closing and not self.busy and self._motion_epoch == epoch
                    and self._motion_target(main_only=main_only) == target)

        def received(value):
            if current():
                try:
                    self.connected(value)
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    failed(exc)

        def failed(exc):
            if current():
                self._motion_problem = str(exc)
                self.motion_connection_label.setToolTip(str(exc))
                self.update_motion_controls()
                if force:
                    self.notice.setText(f"记录状态待确认：{exc}")

        worker = FactoryJob(operation)
        self._motion_worker = worker
        _WORKERS.add(worker)

        def finished():
            self._motion_worker = None
            _WORKERS.discard(worker)
            worker.deleteLater()

        worker.completed.connect(received)
        worker.failed.connect(failed)
        worker.finished.connect(finished)
        worker.start()

    def _motion_ready(self):
        target = self._motion_target()
        if self._motion_paused or self._motion_problem or self._motion_confirmed_at is None or target is None:
            return False
        identity = self.identity or {}
        return (self.client is not None and time.monotonic() - self._motion_confirmed_at <= 8
                and target[:3] == (self.client.url, identity.get("robot_id"), identity.get("source_kind")))

    @staticmethod
    def _motion_row_key(row):
        return tuple(row[key] for key in ("sn", "test_type", "robot_id", "source_kind"))

    def _motion_selection(self):
        identity = self.identity or {}
        robot_id, source_kind = identity.get("robot_id"), identity.get("source_kind")
        if self.tunnel is None and self.live_frame is not None:
            frame = self.live_frame
            if isinstance(frame, dict):
                robot_id, source_kind = frame.get("robot_id"), frame.get("source_kind")
            else:
                robot_id, source_kind = frame.robot_id, frame.source_kind
        return self.sn, self.test_type.currentData(), robot_id, source_kind

    def _active_motion_row(self):
        return next((row for row in self.remote_rows if row["segment_id"] == self._motion_active_id), None)

    def _motion_pending_here(self):
        selection = self._motion_selection()
        return [request for request in self._motion_pending
                if (request.sn, request.test_type, request.robot_id, request.source_kind) == selection]

    def render_remote(self, result):
        self.remote_rows = result["segments"]
        self._motion_active_id = result.get("active_segment_id")
        self._motion_pending = []
        known = {row["segment_id"]: self._motion_row_key(row) for row in self.remote_rows}
        for request in self._pending():
            if known.get(str(request.segment_id)) == (request.sn, request.test_type, request.robot_id, request.source_kind):
                self._write_intent(request, True)
            else:
                self._motion_pending.append(request)
        self._motion_confirmed_at = time.monotonic()
        self._motion_confirmed_wall = datetime.now(timezone.utc)
        self._motion_problem = ""
        self.motion_connection_label.setToolTip(result.get("fault", ""))
        self.update_motion_controls()

    def _motion_history(self):
        selection = self._motion_selection()
        return [row for row in self.remote_rows
                if row["state"] != "recording" and self._motion_row_key(row) == selection]

    def _render_remote_rows(self):
        rows = self._motion_history()
        shown = getattr(self, "_motion_history_shown", [])
        if rows == shown:
            return
        index = self.remote_table.currentRow()
        selected = shown[index]["segment_id"] if 0 <= index < len(shown) else None
        self._motion_history_shown = rows
        self.remote_table.setRowCount(len(rows))
        states = {"sealed": "已结束", "interrupted": "已中断", "fault": "记录故障"}
        for index, row in enumerate(rows):
            valid = row["valid_counts"]
            state = states.get(row["state"], row["state"])
            self._fill(self.remote_table, index, [f"{row['sn']} / {TYPES[row['test_type']]}", f"{row['robot_id']} / {row['source_kind']}", f"{state} / {row['segment_id']}", f"{valid.get('health',0)} / {valid.get('motion',0)}"])
            if row["segment_id"] == selected:
                self.remote_table.selectRow(index)

    def update_duration(self):
        if hasattr(self, "workbench"):
            self.workbench.refresh_status()
        self.update_motion_controls()

    def update_motion_controls(self):
        ready = self._motion_ready()
        idle = not self.busy and not self.closing and not self.result_summary.busy
        selection = self._motion_selection()
        active = self._active_motion_row()
        ours = active is not None and self._motion_row_key(active) == selection
        pending = self._motion_pending_here()
        bound = self.store.sn_identity(self.sn) if self.sn and idle and ready else None
        wrong_robot = bool(bound and (bound["robot_id"], bound["source_kind"]) != selection[2:])
        supported = selection[1] != "stunt" or self.model.currentData() == 24
        if not ready:
            status = "状态待确认"
        elif active and not ours:
            status = f"{active['sn']} · {TYPES[active['test_type']]}录制中"
        elif ours:
            status = "录制中"
        elif not self.sn:
            status = "请先打开整机 SN"
        elif wrong_robot:
            status = "SN 与机器人不匹配"
        elif self._motion_pending:
            request = self._motion_pending[0]
            status = "开始请求待确认" if pending else f"{request.sn} · {TYPES[request.test_type]}待确认"
        else:
            status = "待开始" if supported else "28 轴不适用"
        robot, source = selection[2:]
        self.motion_robot_label.setText((robot or "—") + (" · SIMULATED 模拟数据" if source == "simulated" else ""))
        self.motion_sn_label.setText(self.sn or "未选择")
        self.motion_connection_label.setText("已连接 · 独立 SSH" if ready and self.tunnel else
                                             "已连接" if ready else "连接待确认" if self.identity else "等待连接")
        self.motion_state_label.setText(status)
        duration = "—"
        counts = {}
        if ours and self._motion_confirmed_wall is not None:
            now = datetime.now(timezone.utc) if ready else self._motion_confirmed_wall
            start = datetime.fromisoformat(active["started_at"].replace("Z", "+00:00"))
            hours, rest = divmod(max(0, int((now - start).total_seconds())), 3600)
            minutes, seconds = divmod(rest, 60)
            duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            counts = active["valid_counts"]
        requirement = "站立要求达到 02:00:00" if selection[1] == "standing" else "无两小时时长要求"
        duration_caption = "记录时长（约）"
        if ours and not ready and self._motion_confirmed_wall is not None:
            last_check = self._motion_confirmed_wall.astimezone().strftime("%H:%M:%S")
            duration_caption = f"最后确认 {last_check}（约）"
        self.motion_duration_caption.setText(f"{duration_caption} · {requirement}")
        self.duration_label.setText(duration)
        self.motion_valid_points.setText(f"{counts['motion']:,}" if "motion" in counts else "—")
        self.motion_health_points.setText(f"{counts['health']:,}" if "health" in counts else "—")
        self.start_button.setEnabled(idle and ready and bool(self.sn) and supported and not wrong_robot
                                     and not active and not self._motion_pending)
        self.read_button.setEnabled(idle and ready and ours)
        self.motion_recovery.setVisible(bool(pending) and ready and not active)
        self.motion_retry_button.setEnabled(idle and supported and not wrong_robot)
        self.motion_release_button.setEnabled(idle)
        self.motion_refresh_button.setEnabled(idle and self._motion_target() is not None)
        self._render_remote_rows()
        self.motion_history_button.setEnabled(idle and ready and bool(self._motion_history()))
        for button in self.motion_export_buttons:
            button.setEnabled(idle and bool(self.sn))

    def disconnect_connection(self):
        tunnel, self.tunnel = self.tunnel, None
        self._motion_paused = True
        self._motion_epoch += 1
        self.client = None
        self.notice.setText("本页已断开；机器人记录继续。电脑文件、SN结果和曲线保留。远端表格为最后确认状态。")
        self.update_motion_controls()
        if tunnel:
            self._job(tunnel.close, lambda _: None)

    def _intent_path(self, identifier):
        directory = self.store.directory / "start-intents"
        directory.mkdir(exist_ok=True, mode=0o700)
        return directory / f"{identifier}.json"

    def _write_intent(self, request, confirmed=False):
        path = self._intent_path(request.segment_id)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump({"request": request.model_dump(mode="json"), "confirmed": confirmed}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _pending(self):
        if not self.identity:
            return []
        rows = []
        for path in sorted((self.store.directory / "start-intents").glob("*.json")):
            data = json.loads(path.read_text())
            request = FactorySegmentStart.model_validate(data["request"])
            if not data["confirmed"] and not data.get("abandoned", False) and request.robot_id == self.identity["robot_id"] and request.source_kind == self.identity["source_kind"]:
                rows.append(request)
        return rows

    def start_segment(self):
        if self.busy or self.closing:
            self.notice.setText("正在处理上一项操作，请等待结果后再开始记录。")
            return
        if not self.sn:
            self.notice.setText("尚未选择SN：先打开整机SN，再开始记录。")
            return
        if self.model.currentData() == 28 and self.test_type.currentData() == "stunt":
            self.notice.setText("28轴不进行特技测试，未开始记录。")
            return
        if not self._motion_ready():
            self.notice.setText("正在等待当前机器人记录状态核对。确认后即可开始。")
            self.refresh_remote()
            return
        if self._motion_active_id:
            self.notice.setText("机器人已有活动段，请按页面显示的 SN 和测试项目处理本段。")
            return
        try:
            bound = self.store.sn_identity(self.sn)
            if bound and (bound["robot_id"], bound["source_kind"]) != (self.identity["robot_id"], self.identity["source_kind"]):
                self.notice.setText("此SN已绑定另一机器人身份或数据来源，请核对SN与连接；未发送开始请求。模拟数据需使用单独的SN。")
                return
            if self._pending():
                self.notice.setText("存在开始状态待确认的片段，请先刷新核对或点击重试待确认开始，避免新建重复段。")
                return
            request = FactorySegmentStart(segment_id=uuid4(), sn=self.sn, robot_id=self.identity["robot_id"], source_kind=self.identity["source_kind"], test_type=self.test_type.currentData(), policy=self.policy.text())
        except (ValueError, OSError) as exc:
            self.notice.setText(f"未发送开始请求：{exc}")
            return
        self._start(request)

    def retry_start(self):
        pending = self._motion_pending_here()
        if not pending or not self._motion_ready() or self._motion_active_id:
            self.notice.setText("当前机器人没有待确认的开始请求；请刷新核对活动段。")
            return
        if self.motion_retry_button.isEnabled():
            self._start(pending[0], retry=True)

    def release_pending(self):
        pending = self._motion_pending_here()
        if not pending or not self._motion_ready() or self._motion_active_id:
            self.notice.setText("接回网线并核对机器人无活动段后，才能解除待确认请求。")
            return
        request = pending[0]
        client = self.client
        self._motion_epoch += 1
        epoch = self._motion_epoch

        def operation():
            listing = client.list_segments()
            self._check_motion_epoch(epoch)
            if any(row["segment_id"] == str(request.segment_id) for row in listing["segments"]):
                return listing, False
            if listing.get("active_segment_id"):
                raise ValueError("机器人仍有活动段，请先核对并结束/导入，不能解除")
            path = self._intent_path(request.segment_id)
            data = json.loads(path.read_text())
            data["abandoned"] = True
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return listing, True

        def complete(value):
            listing, abandoned = value
            if self._motion_epoch == epoch:
                self.render_remote(listing)
            self.notice.setText("已放弃本次开始请求，历史记录保留。" if abandoned
                                else "已核对机器人上的原片段，待确认状态已更新。")

        self._job(operation, complete, on_error=self._motion_action_failed)

    def _check_motion_epoch(self, epoch):
        if epoch != self._motion_epoch:
            raise ValueError("机器人连接已变化，请核对当前记录状态后重试。")

    def _motion_action_failed(self, exc):
        self._motion_problem = str(exc)
        self._motion_next_poll = 0.0

    def _start(self, request, *, retry=False):
        client = self.client
        self._motion_epoch += 1
        epoch = self._motion_epoch

        def operation():
            listing = client.list_segments()
            self._check_motion_epoch(epoch)
            if retry and any(row["segment_id"] == str(request.segment_id) for row in listing["segments"]):
                return None, listing, "已找到原片段，请查看当前记录状态。"
            if listing.get("active_segment_id"):
                return None, listing, "机器人已有活动段，请按页面显示的 SN 和测试项目处理本段。"
            if not retry:
                self._write_intent(request)
            info = client.start(request)
            self._write_intent(request, True)
            try:
                listing = client.list_segments()
            except (ValueError, OSError):
                listing = None
            return info, listing, ""

        def complete(value):
            info, listing, notice = value
            current = self._motion_epoch == epoch
            if current and listing is not None:
                self.render_remote(listing)
            elif current and info is not None:
                self.remote_rows = [info, *(row for row in self.remote_rows if row["segment_id"] != info["segment_id"])]
                if info["state"] == "recording":
                    self._motion_active_id = info["segment_id"]
                self._motion_confirmed_wall = datetime.now(timezone.utc)
                self._motion_problem = "开始回执已收到，等待重新核对活动段"
                self._motion_next_poll = 0.0
            if info is not None:
                notice = (f"{info['sn']} · {TYPES[info['test_type']]}已开始记录，请在机器人端现场操作。"
                          if info["state"] == "recording" else "原片段已结束，可在历史片段中取回。")
            self.notice.setText(notice)

        self._job(operation, complete, on_error=self._motion_action_failed,
                  progress=f"正在确认 {request.sn} 的{TYPES[request.test_type]}记录…")

    def _selected_remote(self):
        index = self.remote_table.currentRow()
        rows = getattr(self, "_motion_history_shown", [])
        return rows[index] if 0 <= index < len(rows) else None

    def save_motion_evidence(self):
        if self.busy or self.closing:
            return
        if not self.sn or self.selected_stage not in (4, 5):
            self.notice.setText("先打开SN并选择第4项或第5项，再保存对应测试数据。")
            return
        sn, item = self.sn, self.selected_stage
        def complete(value):
            self.refresh_local()
            self.show_item_result(item)
            self.notice.setText(f"{sn} · 已将{value['segment_count']}段测试数据保存到第{item}项结果，关键统计和时长已附上。" +
                                (("站立时长要求已满足；请填写实际结论。" if item == 4 else "特技无两小时时长要求；请填写实际结论。") if value["duration_satisfied"] else "站立记录不足2小时或未完成，保持未完成。") +
                                "保存数据不等于测试通过。")
        self._job(lambda: self.store.save_motion_evidence(sn, item), complete,
                  progress=f"正在保存到{sn}的第{item}项结果，请稍候…")

    def read_data(self):
        if self.busy or self.closing:
            return
        row = self._active_motion_row()
        selection = self._motion_selection()
        if not self._motion_ready() or not row or self._motion_row_key(row) != selection:
            self.notice.setText("当前样品和测试项目没有已核对的活动记录。可在历史片段中取回已结束的记录。")
            return
        identifier = row["segment_id"]
        client = self.client
        answer = QMessageBox.question(
            self, "结束记录并取回",
            f"结束 {row['sn']} · {row['robot_id']} 的{TYPES[row['test_type']]}记录并取回电脑？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if (not self._motion_ready() or self._motion_selection() != selection or self.client is not client
                or self._motion_active_id != identifier):
            self.notice.setText("连接或活动片段已变化，请核对后重试。")
            return
        self._motion_epoch += 1
        epoch = self._motion_epoch

        def operation():
            listing = client.list_segments()
            self._check_motion_epoch(epoch)
            current = next((item for item in listing["segments"] if item["segment_id"] == identifier), None)
            if (listing.get("active_segment_id") != identifier or not current or current["state"] != "recording"
                    or self._motion_row_key(current) != selection):
                raise ValueError("活动记录已变化，请重新核对本段状态。")
            return self._download_segment(client, current, True)

        self._job(operation, lambda value: self._motion_imported(value, epoch), on_error=self._motion_action_failed,
                  progress=f"正在结束并取回 {row['sn']} 的{TYPES[row['test_type']]}记录…",
                  after_success=self.select_local)

    def import_motion_history(self):
        if self.busy or self.closing or not self._motion_ready():
            return
        history = self._motion_history()
        row = self._selected_remote() or max(history, key=lambda item: item["started_at"], default=None)
        if row is None:
            self.notice.setText("当前样品和测试项目没有已结束的记录。")
            return
        client = self.client
        self._motion_epoch += 1
        epoch = self._motion_epoch
        self._job(lambda: self._download_segment(client, row, False),
                  lambda value: self._motion_imported(value, epoch), on_error=self._motion_action_failed,
                  progress="正在取回选中的历史片段…", after_success=self.select_local)

    def _motion_imported(self, value, epoch):
        imported, listing = value
        if self._motion_epoch != epoch:
            listing = None
        elif listing is None:
            self._motion_problem = "数据已取回，等待核对机器人记录状态"
            self._motion_next_poll = 0.0
        self._import_complete((imported, listing))

    def _download_segment(self, client, row, seal):
        identifier = row["segment_id"]
        sealed_info = None
        if seal:
            try:
                sealed_info = client.seal(identifier)
            except Exception as exc:
                raise SegmentImportError(str(exc), identifier) from exc
        try:
            imported = client.import_segment(identifier, self.store)
        except Exception as exc:
            if sealed_info:
                raise SegmentImportError(str(exc), identifier, sealed_info) from exc
            raise
        try:
            listing = client.list_segments()
        except (ValueError, OSError):
            listing = None
        return imported, listing

    def _import_complete(self, value):
        imported, listing = value
        self.activate_sn(imported["sn"])
        if listing:
            self.render_remote(listing)
        summary = self.store.segment_summary(imported["segment_id"])
        self.render_key_data(summary)
        self.work_tabs.setCurrentIndex(1)
        for index, row in enumerate(self.local_rows):
            if row["segment_id"] == imported["segment_id"]:
                self.local_table.selectRow(index)
                break
        duration = summary["duration_seconds"]
        duration_text = "时长未知" if duration is None else f"记录{duration / 60:.2f}分钟"
        self.notice.setText(f"已校验并保存电脑副本；{duration_text}，" + duration_status(summary) + "。机器人原件保留，性能结论仍需核对关键数据。" +
                            (" 重复导入，沿用已有文件。" if imported.get("duplicate") else "") +
                            (" 远端列表刷新未完成，本地保存成功。" if listing is None else ""))

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择机器人片段manifest.json", str(self.store.directory), "JSON (*.json)")
        if not path:
            return
        manifest = Path(path)
        def complete(result):
            self.activate_sn(result["sn"])
            self.notice.setText("日志已校验并导入电脑；本地操作不依赖中央服务。")
        self._job(lambda: self.store.import_segment(manifest, manifest.parent / "segment.sqlite"), complete)

    def select_local(self):
        if self.busy:
            return
        index = self.local_table.currentRow()
        self.plot.clear()
        self.metric_selector.clear()
        if not 0 <= index < len(self.local_rows):
            return
        row = self.local_rows[index]
        def complete(metrics):
            metrics, summary = metrics
            # Prefer recorded feedback over a missing temperature field.
            metrics = sorted(metrics, key=lambda spec: (
                not spec["available"], not spec["key"].endswith(".position"), spec["key"]
            ))
            for spec in metrics:
                unavailable = " · 本段无有效数据" if not spec["available"] else ""
                self.metric_selector.addItem(f"{spec['label']} ({spec['key']}){unavailable}", spec["key"])
            self.curve_note.setText(f"{row['sn']} · {row['segment_id']} · 缺失项：" + ("、".join(row["missing_counts"]) or "清单未记录缺失"))
            self.render_key_data(summary)
        self._job(lambda: (self.store.metrics(row["segment_id"]), self.store.segment_summary(row["segment_id"])), complete)

    def render_key_data(self, summary):
        duration = summary["duration_seconds"]
        measured = "未知" if duration is None else f"{duration / 60:.2f}分钟"
        mode = "关键数据：1Hz趋势及完整观察统计" if summary["recording_mode"] == "key_data" else "历史原始记录"
        self.key_data_note.setText(f"{mode} · 记录时长{measured} · " +
                                  duration_status(summary) +
                                  f" · 异常观察{summary.get('exception_count', '未统计')}次。记录时长不等于动作时长，数据不构成自动合格判定。")
        metrics = summary.get("metrics", {})
        self.key_data_table.setVisible(bool(metrics))
        self.key_data_table.setRowCount(len(metrics))
        def number(value):
            return "缺失" if value is None else f"{value:.4g}"
        for index, (key, value) in enumerate(sorted(metrics.items())):
            self._fill(self.key_data_table, index, [f"{key} / {value['unit']}", number(value['minimum']),
                number(value['maximum']), number(value['mean']), f"{value['valid']} / {value['observations']}",
                number(value['maximum_age_ms'])])

    def show_curve(self):
        index = self.local_table.currentRow()
        if not 0 <= index < len(self.local_rows):
            self.notice.setText("请先选择一段电脑已保存日志。")
            return
        identifier = self.local_rows[index]["segment_id"]
        key = self.metric_selector.currentData() if self.metric_selector.currentText() == self.metric_selector.itemText(self.metric_selector.currentIndex()) else self.metric_selector.currentText().strip()
        def complete(data):
            self.plot.clear()
            points = data["points"]
            xs, ys, previous_stream = [], [], None
            for point in points:
                value = point["values"][key]
                if point.get("gap_before") or (previous_stream is not None and previous_stream != point["stream_id"]):
                    xs.append(point["elapsed_seconds"])
                    ys.append(np.nan)
                xs.append(point["elapsed_seconds"])
                ys.append(value["value"] if value["value"] is not None and value["valid"] == value["total"] else np.nan)
                previous_stream = point["stream_id"]
            self.plot.setLabel("left", data["metrics"][0]["label"], units=data["metrics"][0]["unit"])
            valid = sum(point["values"][key]["valid"] for point in points)
            total = sum(point["values"][key]["total"] for point in points)
            source = '模拟数据·非实机' if data['source_kind'] == 'simulated' else 'ROS2来源·实机待验收'
            if not valid:
                self.curve_note.setText(f"{source} · {data['metrics'][0]['label']}：本段无有效数据（0/{total}）。请选择有数据的反馈位置、力矩或电池指标；日志总行数{data['raw_count']}不代表该指标有数据。")
                return
            self.plot.plot(xs, ys, pen=pg.mkPen("#2b59e8", width=2), connect="finite", symbol="o", symbolSize=3)
            self.plot.enableAutoRange()
            self.curve_note.setText(f"{source} · 本指标有效样本{valid}/{total}，显示{len(points)}桶；日志总行数{data['raw_count']}。曲线为桶内代表值，完整数值见CSV。缺失、来源时间与单位保存在原始日志。")
        self._job(lambda: self.store.curves(identifier, [key]), complete)

    def add_attachment(self):
        if not self.sn:
            self.notice.setText("先打开SN。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "添加本项证据附件（本地复制）")
        if not path:
            return
        sn, item = self.sn, self.item.currentData()
        def complete(result):
            self.attachments.append(result["attachment_id"])
            self.attachment_note.setText(f"本项待保存附件{len(self.attachments)}个；已复制到本地，请保存人工结果关联。")
        self._job(lambda: self.store.add_attachment(sn, item, path), complete)

    def save_result(self):
        if not self.sn:
            self.notice.setText("先打开SN。")
            return
        sn, item = self.sn, self.item.currentData()
        # A result references all currently imported segments of this item, while
        # earlier results keep their original references and conclusions.
        segment_ids = [row["segment_id"] for row in self.local_rows if (5 if row["test_type"] == "stunt" else 4) == item]
        previous = self.store.results(sn)[item-1]["latest"] or {}
        attachments = list(dict.fromkeys([*previous.get("attachment_ids", []), *self.attachments]))
        status, notes, completed = self.status.currentData(), self.notes.toPlainText(), self.completed.isChecked()
        def operation():
            enough_time = item < 4 or self.store.item_duration_satisfied(sn, item)
            return self.store.save_result(sn, item, status, notes, segment_ids, attachments,
                                          completed=completed and enough_time)
        def complete(saved):
            self.attachments = []
            self.refresh_local()
            self.notice.setText("人工结果已追加保存，历史结果保留。" +
                                ("站立时长不足、状态未完成或功能子项未测，未标记完成。" if completed and not saved["completed"] else "缺失和未验证项在报告中保留。"))
        self._job(operation, complete)

    def export_reports(self):
        if not self.sn:
            self.notice.setText("先打开SN。")
            return
        sn = self.sn
        directory = self.store.directory / "reports"
        def complete(paths):
            self.notice.setText(f"已生成{sn}的五份HTML报告和五份CSV；未测项保留未完成，旧版本保留。目录：{directory}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(paths[0]))))
        self._job(lambda: self.store.export_reports(sn, directory), complete)

    def shutdown(self):
        if self.closed:
            return True
        if not self.result_summary.shutdown():
            return False
        self.closing = True
        if self.battery_swap_dialog:
            self.battery_swap_dialog.reject()
        self.duration_timer.stop()
        if self.busy or self.board_queue or self._motion_worker is not None:
            self._drain_board_sync()
            return False
        if self.tunnel:
            self.tunnel.close()
            self.tunnel = None
        self.store.close()
        self.closed = True
        return True
