"""Native Qt monitor. Closing the window only closes its read connection."""

import json
import socket
import time
from collections import defaultdict, deque
from datetime import datetime
from math import isfinite
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QCoreApplication, QEvent, QProcess, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from websockets.sync.client import connect

from .desktop_connection import (
    ConnectionProbe,
    StationConnector,
    StationDisconnector,
    StationStateProbe,
    local_endpoint,
    ssh_arguments,
)
from .health_view import build_health
from .models import metric_max_age_ms

_PROBES = set()
_RETIRED_READERS = set()


def pending_desktop_workers():
    """Threads owning only this computer's outstanding preview/HTTP reads."""
    from . import desktop_factory, desktop_operations, desktop_recordings, desktop_thermal

    workers = set(_PROBES) | set(_RETIRED_READERS)
    for module in (desktop_factory, desktop_operations, desktop_recordings, desktop_thermal):
        workers.update(module._WORKERS)
    return [worker for worker in workers if isinstance(worker, QThread) and worker.isRunning()]


STYLE = """
QWidget { background:transparent; color:#142441; font-size:14px; }
QMainWindow { background:#eef5ff; }
QFrame#panel { background:#ffffff; border:1px solid #c8d8ef; border-radius:10px; }
QFrame#brand { background:#f2f7ff; border:1px solid #d9e5f8; border-radius:8px; }
QLabel#title { font-size:28px; font-weight:700; }
QLabel#subtitle { color:#576c88; font-size:13px; }
QLabel#eyebrow { color:#2b59e8; font-size:11px; font-weight:700; letter-spacing:2px; }
QLabel#brandName { font-size:19px; font-weight:700; letter-spacing:2px; }
QLabel#sectionTitle { color:#142441; font-size:14px; font-weight:700; padding:3px 0; }
QLabel#healthValue { color:#142441; font-size:30px; font-weight:700; }
QLabel#healthOverall { color:#7d5206; font-size:21px; font-weight:700; }
QFrame#healthCard { background:#ffffff; border:1px solid #c8d8ef; border-radius:8px; }
QFrame#healthBanner { background:#f2f7ff; border:1px solid #c8d8ef; border-radius:8px; }
QTabWidget::pane { background:#ffffff; border:1px solid #c8d8ef; border-radius:8px; }
QTabBar::tab { background:#eaf1fc; color:#576c88; padding:12px 22px; border:1px solid #c8d8ef; margin-right:3px; border-top-left-radius:6px; border-top-right-radius:6px; }
QTabBar::tab:selected { background:#ffffff; color:#2b59e8; font-weight:700; border-bottom-color:#ffffff; }
QTabBar::tab:focus { border:2px solid #2b59e8; }
QLabel#source { background:#fff2d7; color:#7d5206; padding:8px 12px; border:1px solid #e5c992; border-radius:6px; font-weight:600; }
QLabel#connection { color:#576c88; }
QLineEdit, QComboBox { background:#ffffff; border:1px solid #a7bcd8; border-radius:5px; padding:7px; selection-background-color:#2b59e8; }
QLineEdit:focus, QComboBox:focus { border:2px solid #2b59e8; }
QComboBox QAbstractItemView { background:#ffffff; selection-background-color:#e2ecff; selection-color:#142441; }
QPushButton { background:#2b59e8; color:#ffffff; border:1px solid #244cca; border-radius:5px; padding:9px 13px; font-weight:600; }
QPushButton:hover { background:#244cca; }
QPushButton:pressed { background:#1d3da3; }
QPushButton:focus { border:2px solid #142441; }
QPushButton#secondary { background:#ffffff; color:#2b59e8; border-color:#9fb7ed; }
QPushButton#secondary:hover { background:#edf3ff; }
QPushButton:disabled { background:#e4ebf5; color:#65778e; border-color:#c7d3e3; }
QCheckBox { spacing:9px; min-height:30px; }
QCheckBox::indicator { width:18px; height:18px; border:1px solid #7d94b5; border-radius:4px; background:#ffffff; }
QCheckBox::indicator:checked { background:#4165a0; border:2px solid #263451; }
QCheckBox::indicator:focus { border:2px solid #2b59e8; }
QListWidget, QTableWidget { background:#ffffff; border:1px solid #c8d8ef; border-radius:6px; alternate-background-color:#f3f7fd; selection-background-color:#e2ecff; selection-color:#142441; }
QTableWidget { gridline-color:#e2eaf5; }
QHeaderView::section { background:#eaf1fc; color:#405774; padding:8px; border:0; font-weight:600; }
QListWidget::item { padding:7px 3px; }
QListWidget::item:selected { background:#e2ecff; color:#142441; }
QListWidget::item:hover { background:#f1f6ff; }
QScrollBar:vertical { background:#edf3fb; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#b6c7df; min-height:28px; border-radius:5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QSplitter::handle { background:#d6e3f5; }
"""


class GridBackground(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#eef5ff"))
        painter.setPen(QPen(QColor("#e1ebfb"), 1))
        for x in range(0, self.width(), 40):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 40):
            painter.drawLine(0, y, self.width(), y)
        painter.end()


class Reader(QThread):
    frame_received = Signal(dict)
    status = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.socket = None
        self.finished.connect(self._finished)

    def _finished(self):
        _RETIRED_READERS.discard(self)
        self.deleteLater()

    def run(self):
        while not self.isInterruptionRequested():
            try:
                with connect(
                    self.url,
                    open_timeout=3,
                    close_timeout=0.5,
                    max_size=2 * 1024 * 1024,
                    proxy=None,
                ) as websocket:
                    self.socket = websocket
                    while not self.isInterruptionRequested():
                        try:
                            raw = websocket.recv(timeout=0.5)
                        except TimeoutError:
                            continue
                        self.frame_received.emit(json.loads(raw))
            except Exception:
                if self.isInterruptionRequested():
                    break
                self.status.emit("连接中断，正在重连；机器人采集与上位机转发独立运行")
                for _ in range(10):
                    if self.isInterruptionRequested():
                        break
                    self.msleep(100)
            finally:
                self.socket = None

    def stop(self):
        # A timed-out wait followed by releasing the last reference can abort Qt.
        # Keep the thread alive until finished, without blocking the GUI on I/O.
        if self.isRunning():
            _RETIRED_READERS.add(self)
        self.requestInterruption()
        transport = getattr(self.socket, "socket", None)
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class MonitorWindow(QMainWindow):
    def __init__(self, url):
        super().__init__()
        self.setWindowTitle("RP1 整机健康监测")
        self.resize(1380, 860)
        self.setMinimumSize(1000, 680)
        self.reader = None
        self.ssh = None
        self.probe = None
        self._closing = False
        self._close_connections_stopped = False
        self.connection_generation = 0
        self.active_source_url = ""
        self.station_url = ""
        self.station_token_file = None
        self.station_fingerprint = ""
        self.station_connections = {}
        self.station_robot_id = None
        self.station_request_generation = 0
        self.catalog = []
        self.buffers = defaultdict(lambda: deque(maxlen=600))
        self.buffer_robot_id = None
        self._buffer_resume = False
        self.curves = {}
        self.last_received = 0.0
        self.last_frame = None
        self.health_expired = False
        self.setStyleSheet(STYLE)
        root = GridBackground()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        eyebrow = QLabel("ROBO PARTY  /  LIVE TELEMETRY")
        eyebrow.setObjectName("eyebrow")
        title_box.addWidget(eyebrow)
        title = QLabel("RP1 整机健康监测")
        title.setObjectName("title")
        title_box.addWidget(title)
        subtitle = QLabel("电池 · 主机 · 电机 · 只读采集与持续入库")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.source_label = QLabel("来源待确认")
        self.source_label.setObjectName("source")
        header.addWidget(self.source_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)
        self.connection_label = QLabel("尚未连接")
        self.connection_label.setObjectName("connection")
        self.connection_label.setWordWrap(True)
        self.upload_label = QLabel("中央上报：待确认")
        self.upload_label.setWordWrap(True)
        self.upload_label.setStyleSheet("color:#576c88;")
        self.upload_label.setWordWrap(True)
        self.upload_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_bar = QHBoxLayout()
        status_bar.addWidget(self.connection_label, 1)
        status_bar.addWidget(self.upload_label, 1)
        layout.addLayout(status_bar)
        split = QSplitter()
        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(8)
        brand = QFrame()
        brand.setObjectName("brand")
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(14, 10, 12, 10)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(4)
        brand_assets = Path(__file__).parent / "web" / "brand"
        brand_name = QLabel()
        logo_pixmap = QPixmap(str(brand_assets / "logo.png"))
        if logo_pixmap.isNull():
            brand_name.setText("ROBO\nPARTY")
            brand_name.setObjectName("brandName")
        else:
            brand_name.setPixmap(
                logo_pixmap.scaled(
                    155,
                    58,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            brand_name.setAccessibleName("ROBO PARTY")
        brand_text.addWidget(brand_name)
        brand_caption = QLabel("RP1 · 现场观察")
        brand_caption.setObjectName("subtitle")
        brand_text.addWidget(brand_caption)
        brand_layout.addLayout(brand_text)
        brand_layout.addStretch()
        robot_pixmap = QPixmap(str(brand_assets / "robot-wave.webp"))
        if robot_pixmap.isNull():
            robot_pixmap = QPixmap(str(brand_assets / "robot.png"))
        if not robot_pixmap.isNull():
            robot = QLabel()
            robot.setPixmap(
                robot_pixmap.scaled(
                    70,
                    82,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            robot.setAccessibleName("RP1机器人")
            brand_layout.addWidget(robot)
        left_layout.addWidget(brand)
        form = QFormLayout()
        form.setSpacing(8)
        self.url = QLineEdit(url)
        self.url.setMinimumHeight(34)
        self.url.setPlaceholderText("http://127.0.0.1:8892（以后台relay端口为准）")
        self.connection_mode = QComboBox()
        self.connection_mode.addItem("上位机后台 relay（推荐）", "relay")
        self.connection_mode.addItem("机器人采集端临时SSH直读", "collector")
        self.connection_mode.addItem("SSH地址与密码（电脑后台接入）", "ssh_station")
        self.connection_mode.setAccessibleName("监控连接方式")
        form.addRow("连接方式", self.connection_mode)
        form.addRow("上位机服务", self.url)
        self.station_robots = QComboBox()
        self.station_robots.setAccessibleName("本窗口预览机器人")
        self.station_robots.currentIndexChanged.connect(self.select_station_robot)
        form.addRow("后台机器人", self.station_robots)
        self.station_refresh = QPushButton("只读刷新后台连接")
        self.station_refresh.clicked.connect(lambda: self.restore_station_connection(False))
        form.addRow(self.station_refresh)
        self.station_disconnect = QPushButton("断开所选后台机器人")
        self.station_disconnect.setEnabled(False)
        self.station_disconnect.clicked.connect(self.disconnect_station_robot)
        form.addRow(self.station_disconnect)
        self.ssh_target = QLineEdit()
        self.ssh_target.setMinimumHeight(34)
        self.ssh_target.setPlaceholderText("user@机器人IP，仅临时SSH直读时填写")
        form.addRow("SSH目标", self.ssh_target)
        self.ssh_password = QLineEdit()
        self.ssh_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ssh_password.setAccessibleName("机器人SSH密码")
        self.ssh_password.setPlaceholderText("仅用于本次认证，不保存密码")
        form.addRow("SSH密码", self.ssh_password)
        self.ssh_fingerprint = QLabel("首次连接会显示主机指纹，请核对后确认")
        self.ssh_fingerprint.setWordWrap(True)
        self.ssh_fingerprint.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("主机指纹", self.ssh_fingerprint)
        self.ssh_trust = QCheckBox("已核对上方指纹，允许首次连接")
        form.addRow("身份确认", self.ssh_trust)
        self.ssh_target.textChanged.connect(self.clear_station_fingerprint)
        self.ssh_key = QLineEdit()
        self.ssh_key.setMinimumHeight(34)
        self.ssh_key.setPlaceholderText("留空使用系统 ssh-agent")
        form.addRow("SSH密钥", self.ssh_key)
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(8891)
        form.addRow("机器人采集端口", self.ssh_port)
        self.connection_form = form
        self.connection_help = QLabel()
        self.connection_help.setWordWrap(True)
        self.connection_help.setObjectName("subtitle")
        self.connection_mode.currentIndexChanged.connect(self.update_connection_mode)
        self.update_connection_mode()
        left_layout.addLayout(form)
        left_layout.addWidget(self.connection_help)
        controls = QHBoxLayout()
        self.connect_button = QPushButton("连接监控")
        self.connect_button.clicked.connect(self.connect_source)
        self.disconnect_button = QPushButton("停止本窗口预览")
        self.disconnect_button.setObjectName("secondary")
        self.disconnect_button.clicked.connect(self.disconnect_source)
        controls.addWidget(self.connect_button)
        controls.addWidget(self.disconnect_button)
        left_layout.addLayout(controls)
        self.curve_controls = QWidget()
        curve_layout = QVBoxLayout(self.curve_controls)
        curve_layout.setContentsMargins(0, 0, 0, 0)
        curve_layout.setSpacing(8)
        parameter_title = QLabel("曲线参数 · 最多12条，同一单位")
        parameter_title.setObjectName("sectionTitle")
        curve_layout.addWidget(parameter_title)
        self.group = QComboBox()
        self.group.addItems(
            [
                "位置与策略目标 / rad",
                "关节速度 / rad/s",
                "上报力矩 / N·m",
                "IMU角速度 / rad/s",
                "IMU四元数",
                "温度 / °C",
                "电池电压 / V",
                "电池电流 / A",
                "电量与主机负载 / %",
                "进程CPU / %（单核100%）",
                "进程RSS / MiB（近似）",
                "进程线程 / 个",
                "进程观察重启 / 次",
                "进程存活 / 0或1",
            ]
        )
        self.group.currentIndexChanged.connect(self.populate_metrics)
        curve_layout.addWidget(self.group)
        self.metric_list = QListWidget()
        self.metric_list.setMinimumHeight(100)
        self.metric_list.itemChanged.connect(self.redraw)
        curve_layout.addWidget(self.metric_list, 1)
        note = QLabel(
            "关节编号为ROS数组索引，物理映射待实机核对。\n策略目标不代表最终电机指令。\nSSH首次连接前请通过系统ssh确认主机指纹。"
        )
        note.setWordWrap(True)
        note.setObjectName("subtitle")
        curve_layout.addWidget(note)
        left_layout.addWidget(self.curve_controls)
        left_layout.addStretch()
        collection_note = QLabel(
            "监控连接与运控独立。\n关闭窗口后，机器人采集与上位机后台转发继续运行。\n未接入、缺失与过期数据不会显示为正常。"
        )
        collection_note.setWordWrap(True)
        collection_note.setObjectName("subtitle")
        left_layout.addWidget(collection_note)
        left_scroll = QScrollArea()
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(left)
        left_scroll.setMinimumWidth(320)
        split.addWidget(left_scroll)
        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 12, 16, 12)
        right_layout.setSpacing(8)
        chart_header = QHBoxLayout()
        chart_title = QLabel("实时曲线")
        chart_title.setObjectName("sectionTitle")
        chart_header.addWidget(chart_title)
        chart_header.addStretch()
        chart_caption = QLabel("60秒滚动窗口 · 缺失数据保留断点")
        chart_caption.setObjectName("subtitle")
        chart_header.addWidget(chart_caption)
        right_layout.addLayout(chart_header)
        pg.setConfigOptions(antialias=False, background="#ffffff", foreground="#576c88")
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("bottom", "相对采集时间", units="s")
        self.plot.addLegend(offset=(12, 10))
        right_layout.addWidget(self.plot, 3)
        table_title = QLabel("关节反馈")
        table_title.setObjectName("sectionTitle")
        right_layout.addWidget(table_title)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["关节索引", "位置 / rad", "速度 / rad/s", "上报力矩 / N·m", "策略目标 / rad"]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.table, 2)
        self.quality_label = QLabel("数据新鲜度待确认；不展示未提供的线加速度或实际行走速度")
        self.quality_label.setWordWrap(True)
        self.quality_label.setObjectName("subtitle")
        right_layout.addWidget(self.quality_label)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_health_page(robot_pixmap), "整机健康")
        self.tabs.addTab(right, "关节与 IMU")
        from .desktop_operations import OperationsPanel

        self.operations_panel = OperationsPanel(lambda: self.active_source_url or self.url.text())
        self.tabs.addTab(self.operations_panel, "设备与诊断")
        from .desktop_joints import JointInspectionPanel

        self.joint_inspection_panel = JointInspectionPanel(self.calibration_connection)
        self.tabs.addTab(self.joint_inspection_panel, "角度与标定")
        self.tabs.currentChanged.connect(lambda index: self.curve_controls.setVisible(index == 1))
        self.curve_controls.hide()
        split.addWidget(self.tabs)
        split.setSizes([340, 1000])
        layout.addWidget(split, 1)
        self.setCentralWidget(root)
        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self.check_stale)
        self.watchdog.start(500)
        self.render_health(None)
        if hasattr(self, "operations_panel"):
            self.operations_panel.mark_disconnected()

    def build_health_page(self, robot_pixmap):
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(16, 14, 16, 14)
        page_layout.setSpacing(12)
        banner = QFrame()
        banner.setObjectName("healthBanner")
        banner_layout = QVBoxLayout(banner)
        self.health_overall = QLabel()
        self.health_overall.setObjectName("healthOverall")
        self.health_overall.setTextFormat(Qt.TextFormat.PlainText)
        self.health_overall.setWordWrap(True)
        self.health_detail = QLabel()
        self.health_detail.setObjectName("subtitle")
        self.health_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.health_detail.setWordWrap(True)
        banner_layout.addWidget(self.health_overall)
        banner_layout.addWidget(self.health_detail)
        page_layout.addWidget(banner)
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.health_status = {}
        card_layouts = {}
        for key, title in (
            ("battery", "电池与供电"),
            ("host", "主机与散热"),
            ("motors", "电机与驱动"),
        ):
            card = QFrame()
            card.setObjectName("healthCard")
            body = QVBoxLayout(card)
            body.setContentsMargins(12, 12, 12, 12)
            body.setSpacing(7)
            heading = QLabel(title)
            heading.setObjectName("sectionTitle")
            body.addWidget(heading)
            status = QLabel()
            status.setTextFormat(Qt.TextFormat.PlainText)
            status.setWordWrap(True)
            body.addWidget(status)
            self.health_status[key] = status
            card_layouts[key] = body
            cards.addWidget(card, 1)
        self.battery_soc = QLabel("—")
        self.battery_soc.setObjectName("healthValue")
        card_layouts["battery"].addWidget(self.battery_soc)
        self.battery_metrics = QLabel()
        self.battery_metrics.setWordWrap(True)
        self.battery_metrics.setTextFormat(Qt.TextFormat.PlainText)
        self.battery_metrics.setStyleSheet("font-size:13px;")
        card_layouts["battery"].addWidget(self.battery_metrics)
        self.battery_age = QLabel()
        self.battery_age.setWordWrap(True)
        self.battery_age.setObjectName("subtitle")
        card_layouts["battery"].addWidget(self.battery_age)
        card_layouts["battery"].addStretch()
        self.host_temperature = QLabel("—")
        self.host_temperature.setObjectName("healthValue")
        card_layouts["host"].addWidget(self.host_temperature)
        self.host_usage = QLabel()
        self.host_usage.setWordWrap(True)
        self.host_usage.setTextFormat(Qt.TextFormat.PlainText)
        self.host_usage.setStyleSheet("font-size:13px;")
        card_layouts["host"].addWidget(self.host_usage)
        self.host_core_note = QLabel("采集端尚未提供每核数据")
        self.host_core_note.setWordWrap(True)
        self.host_core_note.setTextFormat(Qt.TextFormat.PlainText)
        self.host_core_note.setObjectName("subtitle")
        card_layouts["host"].addWidget(self.host_core_note)
        self.host_core_table = QTableWidget(0, 3)
        self.host_core_table.setAccessibleName("机器人各CPU核心使用率")
        self.host_core_table.setHorizontalHeaderLabels(["CPU编号", "核心分组", "使用率 / %"])
        self.host_core_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.host_core_table.setAlternatingRowColors(True)
        self.host_core_table.verticalHeader().hide()
        self.host_core_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.host_core_table.setMinimumHeight(100)
        self.host_core_table.setMaximumHeight(180)
        card_layouts["host"].addWidget(self.host_core_table)
        self.host_sensors = QListWidget()
        self.host_sensors.setMinimumHeight(50)
        self.host_sensors.setMaximumHeight(76)
        card_layouts["host"].addWidget(self.host_sensors, 1)
        self.motor_coverage = QLabel("—")
        self.motor_coverage.setObjectName("healthValue")
        card_layouts["motors"].addWidget(self.motor_coverage)
        self.motor_metrics = QLabel()
        self.motor_metrics.setWordWrap(True)
        self.motor_metrics.setTextFormat(Qt.TextFormat.PlainText)
        self.motor_metrics.setStyleSheet("font-size:13px;")
        card_layouts["motors"].addWidget(self.motor_metrics)
        card_layouts["motors"].addStretch()
        page_layout.addLayout(cards)
        lower = QHBoxLayout()
        lower.setSpacing(10)
        coverage = QFrame()
        coverage.setObjectName("healthCard")
        coverage_layout = QVBoxLayout(coverage)
        coverage_layout.setContentsMargins(12, 12, 12, 12)
        coverage_title = QLabel("数据源与监测覆盖")
        coverage_title.setObjectName("sectionTitle")
        coverage_layout.addWidget(coverage_title)
        coverage_body = QHBoxLayout()
        self.health_sources_label = QLabel()
        self.health_sources_label.setTextFormat(Qt.TextFormat.PlainText)
        self.health_sources_label.setWordWrap(True)
        self.health_sources_label.setObjectName("subtitle")
        coverage_body.addWidget(self.health_sources_label, 1)
        if not robot_pixmap.isNull():
            robot = QLabel()
            robot.setPixmap(
                robot_pixmap.scaled(
                    100,
                    180,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            robot.setAccessibleName("RP1机器人外观")
            coverage_body.addWidget(robot)
        coverage_layout.addLayout(coverage_body, 1)
        lower.addWidget(coverage, 2)
        motor_panel = QFrame()
        motor_panel.setObjectName("healthCard")
        motor_layout = QVBoxLayout(motor_panel)
        motor_layout.setContentsMargins(12, 12, 12, 12)
        motor_title = QLabel("电机温度与原始故障码")
        motor_title.setObjectName("sectionTitle")
        motor_layout.addWidget(motor_title)
        self.motor_table = QTableWidget(0, 4)
        self.motor_table.setHorizontalHeaderLabels(
            ["电机 / 关节名", "电机 / °C", "MOS / °C", "故障码"]
        )
        self.motor_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.motor_table.setAlternatingRowColors(True)
        self.motor_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.motor_table.setMinimumHeight(130)
        self.motor_table.setMaximumHeight(160)
        motor_layout.addWidget(self.motor_table, 1)
        self.motor_note = QLabel()
        self.motor_note.setTextFormat(Qt.TextFormat.PlainText)
        self.motor_note.setObjectName("subtitle")
        self.motor_note.setWordWrap(True)
        motor_layout.addWidget(self.motor_note)
        lower.addWidget(motor_panel, 3)
        page_layout.addLayout(lower, 1)
        scroll.setWidget(page)
        return scroll

    def render_health(self, frame, stale=False):
        values = frame or self.last_frame or {}
        health = values.get("health") if frame else None
        if not health:
            health = build_health(
                values.get("values", {}),
                values.get("ages_ms", {}),
                values.get("health_sources", {}),
                values.get("health_sensors", {}),
                values.get("joint_names", []),
                available=bool(frame),
            )
        colors = {"fault": "#ae283a", "incomplete": "#7d5206", "observing": "#2b59e8"}
        overall = health["overall"]
        self.health_overall.setText("预览已过期 · 健康状态未知" if stale else overall["label"])
        self.health_overall.setStyleSheet(f"color:{colors[overall['state']]};")
        self.health_detail.setText(
            "当前连接未收到新预览，所有健康数值已隐藏；机器人连接与上位机后台转发状态需另行确认。"
            if stale
            else overall["detail"]
        )
        for key, label in self.health_status.items():
            section = health[key]
            label.setText("预览已过期" if stale else section["label"])
            label.setStyleSheet(f"color:{colors[section['state']]};")
            label.setToolTip(section["detail"])

        def number(value, unit="", digits=1):
            return "—" if value is None else f"{value:.{digits}f}{unit}"

        def raw_code(value):
            return (
                "—"
                if value is None
                else str(int(value))
                if float(value).is_integer()
                else str(value)
            )

        battery = health["battery"]
        self.battery_soc.setText(number(battery["soc"], "%"))
        switch = battery["power_enabled"]
        switch_text = "ON" if switch == 1 else "OFF" if switch == 0 else "—"
        power = battery["derived_power"]
        power_text = "—" if power is None else f"{power:+.1f} W"
        rails_text = ""
        if any(battery.get(key) is not None for key in ("rail48_enabled", "rail5_enabled")):

            def rail(value):
                return "—" if value is None else "ON" if value == 1 else "OFF"

            rails_text = f"48V轨：{rail(battery.get('rail48_enabled'))} · 5V轨：{rail(battery.get('rail5_enabled'))}\n"
        battery_temp = number(battery["temperature"], " °C")
        protection = battery.get("protection")
        protection_text = raw_code(protection)
        self.battery_metrics.setText(
            f"电压：{number(battery['voltage'], ' V')}  ·  电流：{number(battery['current'], ' A')}\n"
            f"供电开关：{switch_text}\n" + rails_text + f"原始保护位：{protection_text}\n"
            f"计算功率：{power_text}（V × A）\n"
            f"电池温度：{battery_temp}"
            + (" · 源未提供" if battery["temperature"] is None and frame else "")
        )
        self.battery_age.setText(
            f"{battery.get('age_label', '观测年龄')}：{number(battery['age_ms'], ' ms', 0)}"
        )
        self.battery_age.setToolTip(battery["detail"])
        host = health["host"]
        self.host_temperature.setText(number(host["max_temperature"], " °C"))
        self.host_usage.setText(
            f"最高热区温度\nCPU：{number(host['cpu_usage'], '%')}  ·  内存：{number(host['memory_usage'], '%')}"
        )
        core_host = host
        if not frame or stale:
            previous_host = values.get("health", {}).get("host", {})
            if previous_host.get("cores"):
                core_host = previous_host
        self.render_host_cores(
            core_host,
            available=bool(frame)
            and not stale
            and values.get("upload", {}).get("collector_connected", True),
        )
        self.host_sensors.clear()
        for sensor in host["sensors"]:
            self.host_sensors.addItem(f"{sensor['label']}：{number(sensor['temperature'], ' °C')}")
        if not host["sensors"]:
            self.host_sensors.addItem("暂无机器人主机温度传感器")
        motors = health["motors"]
        self.motor_coverage.setText(
            f"{motors['available']} / {motors['total']}" if frame and motors["total"] else "—"
        )
        self.motor_metrics.setText(
            "温度 + 故障码反馈覆盖\n"
            f"最高电机温度：{number(motors['max_temperature'], ' °C')}\n"
            f"最高MOS温度：{number(motors['max_mos_temperature'], ' °C')}\n"
            + (
                f"非零故障上报：{len(motors['faults'])} 项"
                if motors["faults"]
                or any(item["fault_code"] is not None for item in motors["items"])
                else "故障码：尚未提供 / 已过期"
            )
        )
        motor_rows = list(motors["items"]) + [
            {
                "name": fault["joint"],
                "temperature": None,
                "mos_temperature": None,
                "fault_code": fault["code"],
            }
            for fault in motors["faults"]
            if isinstance(fault["joint"], str)
        ]
        motor_rows.sort(key=lambda motor: not bool(motor["fault_code"]))
        self.motor_table.setRowCount(len(motor_rows))
        for row, motor in enumerate(motor_rows):
            cells = [
                str(motor["name"]),
                number(motor["temperature"]),
                number(motor["mos_temperature"]),
                raw_code(motor["fault_code"]),
            ]
            for column, text in enumerate(cells):
                item = self.motor_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.motor_table.setItem(row, column, item)
                item.setText(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, motor.get("joint"))
                item.setToolTip(text)
                if column == 3:
                    item.setForeground(QColor("#ae283a" if motor["fault_code"] else "#142441"))
        self.motor_note.setText("原始故障码直接显示；温度限值尚未配置。")
        self.motor_note.setToolTip(motors["detail"])
        source_names = {"bms": "电池来源", "host": "机器人主机", "motors": "电机与驱动"}
        state_names = {
            "live": "来源已接入",
            "waiting": "等待数据",
            "error": "采集异常",
            "unsupported": "尚未接入",
        }
        sources = health["sources"]
        if "rp_server" in sources:
            source_names["rp_server"] = "rp_server缓存"
        self.health_sources_label.setText(
            "\n\n".join(
                f"{name} · "
                + (
                    "预览过期，当前状态未知"
                    if stale
                    else "等待连接"
                    if not frame
                    else state_names.get(sources.get(key, {}).get("state"), "尚未接入")
                )
                for key, name in source_names.items()
            )
        )
        self.health_sources_label.setToolTip(
            "\n\n".join(
                f"{source_names.get(key, key)}：{source['detail']}"
                for key, source in sources.items()
            )
        )

    def render_host_cores(self, host, *, available):
        groups = {"big": "大核", "little": "小核", "unknown": "分组未知"}
        rows = []
        elapsed_ms = (
            max(0, time.monotonic() - self.last_received) * 1000 if self.last_received else 0
        )
        for core in host.get("cores", []):
            usage, age = core.get("usage_percent"), core.get("age_ms")
            current = (
                available
                and isinstance(usage, (int, float))
                and not isinstance(usage, bool)
                and isfinite(usage)
                and 0 <= usage <= 100
                and isinstance(age, (int, float))
                and not isinstance(age, bool)
                and isfinite(age)
                and 0 <= age
                and age + elapsed_ms <= metric_max_age_ms("host.cpu.usage")
            )
            rows.append(
                {
                    **core,
                    "usage_percent": usage if current else None,
                    "display_age_ms": age + elapsed_ms if isinstance(age, (int, float)) else None,
                    "group": core.get("group") if core.get("group") in groups else "unknown",
                }
            )
        rows.sort(key=lambda core: core["index"])
        self._host_cpu_rows = rows
        topology = host.get("cpu_topology_note", "")
        self.host_core_note.setText(
            f"每核使用率 · {len(rows)} 个逻辑CPU · 每核0–100%\n{topology or '大小核分组由采集端提供；未知分组不推断。'}"
            if rows
            else "采集端尚未提供每核数据；不根据总CPU占用推算。"
        )
        self.host_core_table.setRowCount(len(rows))
        for row, core in enumerate(rows):
            group = groups.get(core.get("group"), groups["unknown"])
            usage = core["usage_percent"]
            cells = [f"CPU{core['index']}", group, "未知" if usage is None else f"{usage:.1f}%"]
            for column, text in enumerate(cells):
                item = self.host_core_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.host_core_table.setItem(row, column, item)
                item.setText(text)
                item.setToolTip(
                    f"{core.get('label', cells[0])} · {group}\n观测年龄：{core.get('display_age_ms')} ms\n使用率未知表示缺失、过期或核心离线，不等于0%。"
                )
                item.setForeground(QColor("#65778e" if usage is None else "#142441"))

    def update_connection_mode(self):
        relay = self.connection_mode.currentData() == "relay"
        station = self.connection_mode.currentData() == "ssh_station"
        self.url.setEnabled(relay)
        for control in (self.ssh_target, self.ssh_key, self.ssh_port):
            control.setEnabled(not relay)
        self.ssh_key.setEnabled(not relay and not station)
        for control in (self.ssh_password, self.ssh_fingerprint, self.ssh_trust):
            control.setVisible(station)
            control.setEnabled(station)
        for field, visible in ((self.url, relay), (self.ssh_target, not relay),
                               (self.ssh_key, not relay and not station),
                               (self.ssh_port, not relay), (self.ssh_password, station),
                               (self.ssh_fingerprint, station),
                               (self.ssh_trust, station and bool(self.station_fingerprint))):
            field.setVisible(visible)
            label = self.connection_form.labelForField(field)
            if label:
                label.setVisible(visible)
        if hasattr(self, "quick_web_button"):
            self.quick_web_button.setVisible(station)
        if hasattr(self, "connect_button"):
            self.connect_button.setText("添加后台机器人" if station else "连接监控")
        if hasattr(self, "connection_summary"):
            self.connection_summary.setText(
                "密码仅用于本次认证。关闭窗口后电脑后台继续接收；连接数据，不操作机器人。"
                if station else "上位机服务填写relay根地址。后台独立运行；临时SSH直读随窗口退出，不承担中央上传。缺失和过期数据保持未知。"
            )
        self.connection_help.setText(
            "输入 ssh robo@192.168.137.250 和密码；机器人监听需已开启。只转发端口，不执行机器人命令。关闭窗口后电脑后台继续接收。"
            if station else "填写这台电脑后台 relay 的本机地址。网线SSH隧道与转发应由独立后台维护；关闭本窗口不影响后台。"
            if relay
            else "临时直读机器人已有采集端：只用于排查，不负责中央上传；本窗口创建的SSH隧道随窗口关闭。需系统密钥或 ssh-agent，首次指纹请在终端确认。"
        )

    def clear_station_fingerprint(self):
        self.station_fingerprint = ""
        self.ssh_trust.setChecked(False)
        self.ssh_fingerprint.setText("首次连接会显示主机指纹，请核对后确认")
        if hasattr(self, "connection_form"):
            self.update_connection_mode()

    def update_station_connections(self, state, preferred=None):
        connections = state.get("connections")
        if connections is None:
            connections = [state] if state.get("connected") else []
        self.station_connections = {
            row["robot"]["robot_id"]: row for row in connections
            if isinstance(row.get("robot"), dict) and row["robot"].get("robot_id")
        }
        selected = preferred or self.station_robots.currentData() or self.station_robot_id
        self.station_robots.blockSignals(True)
        self.station_robots.clear()
        for robot_id, row in self.station_connections.items():
            source = row["robot"].get("source_kind")
            note = "模拟数据，非实机" if source == "simulated" else "ROS2，仍待实机验收"
            self.station_robots.addItem(f"{robot_id} · {note}" + (" · 已断开" if not row.get("connected") else ""), robot_id)
        index = self.station_robots.findData(selected)
        if index >= 0:
            self.station_robots.setCurrentIndex(index)
        self.station_robots.blockSignals(False)
        self.station_disconnect.setEnabled(bool(self.station_robots.currentData()))

    def select_station_robot(self):
        robot_id = self.station_robots.currentData()
        self.station_disconnect.setEnabled(bool(robot_id))
        row = self.station_connections.get(robot_id)
        if row and row.get("connected"):
            self.attach_station_connection(row)
        elif row:
            self.connection_label.setText("所选后台机器人已断开；可通过SSH表单重新添加连接")

    def attach_station_connection(self, state):
        try:
            relay_url = local_endpoint(state["relay_url"])
            token = Path(self.station_token_file).read_text().strip()
        except (KeyError, OSError, ValueError, TypeError, AttributeError):
            self.connect_button.setEnabled(True)
            self.connection_label.setText("电脑后台连接信息无效，可填写SSH地址与密码后重试")
            return
        self.operations_panel.server.setText(self.station_url)
        self.operations_panel.token.setText(token)
        self.url.setText(relay_url)
        self.connection_mode.setCurrentIndex(0)
        self.begin_preview(relay_url, "relay")

    def restore_station_connection(self, attach=True):
        """Read the background list; refreshing never changes background connections."""
        if self._closing or not self.station_url or not self.station_token_file:
            return
        self.station_request_generation += 1
        request_generation = self.station_request_generation
        generation = self.connection_generation
        if attach:
            self.connect_button.setEnabled(False)
        self.connection_label.setText("正在只读检查电脑后台已有连接…")
        worker = StationStateProbe(self.station_url, self.station_token_file)
        self.probe = worker

        def current():
            from shiboken6 import isValid

            return (isValid(self) and not self._closing and generation == self.connection_generation
                    and request_generation == self.station_request_generation)

        def complete(state):
            if not current():
                return
            self.update_station_connections(state)
            self.connect_button.setEnabled(True)
            if not attach:
                self.connection_label.setText("后台连接列表已只读刷新；本窗口预览保持不变")
                return
            row = self.station_connections.get(self.station_robots.currentData(), state)
            if not row.get("connected"):
                self.connection_label.setText("电脑后台尚未连接，请填写机器人SSH地址与密码")
                self.ssh_password.setFocus()
                return
            self.attach_station_connection(row)

        def fail(message):
            if current():
                self.connect_button.setEnabled(True)
                self.connection_label.setText(message)
                self.ssh_password.setFocus()

        def finish():
            _PROBES.discard(worker)
            if current() and self.probe is worker:
                self.probe = None

        worker.done.connect(complete)
        worker.failed.connect(fail)
        worker.finished.connect(finish)
        _PROBES.add(worker)
        worker.start()

    def disconnect_station_robot(self):
        robot_id = self.station_robots.currentData()
        if not robot_id or not self.station_url or not self.station_token_file:
            return
        self.station_request_generation += 1
        generation = self.station_request_generation
        self.station_disconnect.setEnabled(False)
        worker = StationDisconnector(self.station_url, self.station_token_file, robot_id)

        def current():
            from shiboken6 import isValid

            return isValid(self) and not self._closing and generation == self.station_request_generation

        def complete(state):
            if not current():
                return
            row = self.station_connections.get(robot_id, {})
            if self.probe and getattr(self.probe, "url", None) == row.get("relay_url"):
                self.connection_generation += 1
                self.probe.requestInterruption()
                self.probe = None
                self.connect_button.setEnabled(True)
                if self.station_robot_id and self.station_robot_id != robot_id:
                    self.station_robots.blockSignals(True)
                    self.station_robots.setCurrentIndex(self.station_robots.findData(self.station_robot_id))
                    self.station_robots.blockSignals(False)
            if self.station_robot_id == robot_id:
                self.clear_preview()
            self.update_station_connections(state, self.station_robots.currentData())
            self.connection_label.setText(f"后台机器人 {robot_id} 已断开；其它后台连接继续运行")
            if hasattr(self, "recordings_panel"):
                self.recordings_panel.refresh_context(False)

        def fail(message):
            if current():
                self.station_disconnect.setEnabled(True)
                self.connection_label.setText(message)

        worker.done.connect(complete)
        worker.failed.connect(fail)
        worker.finished.connect(lambda: _PROBES.discard(worker))
        _PROBES.add(worker)
        worker.start()

    def begin_preview(self, url, mode):
        # Keep the existing reader usable until the replacement passes its preflight.
        self.connection_generation += 1
        if self.probe:
            self.probe.requestInterruption()
            self.probe = None
        self.connect_button.setEnabled(False)
        self.connection_label.setText("正在只读预检服务身份与机器人来源…")
        self.preflight_source(url, mode, self.connection_generation)

    def connect_station(self):
        if not self.station_url or not self.station_token_file:
            self.connection_label.setText("请用 start-monitor.sh 或 desktop --quick-connect 启动密码连接后台")
            return
        if not self.ssh_password.text():
            self.connection_label.setText("请填写机器人SSH密码；密码不会保存")
            self.ssh_password.setFocus()
            return
        body = {"target": self.ssh_target.text(), "password": self.ssh_password.text(),
                "collector_port": self.ssh_port.value(),
                "confirmed_fingerprint": self.station_fingerprint if self.ssh_trust.isChecked() else ""}
        self.connection_generation += 1
        if self.probe:
            self.probe.requestInterruption()
            self.probe = None
        generation = self.connection_generation
        self.connect_button.setEnabled(False)
        self.connection_label.setText("正在建立SSH、核对监听身份并接入电脑relay…")
        worker = StationConnector(self.station_url, self.station_token_file, body)
        self.probe = worker

        def current():
            from shiboken6 import isValid

            return isValid(self) and not self._closing and generation == self.connection_generation

        def complete(result):
            if not current():
                return
            if result.get("status") == "host_key_required":
                self.station_fingerprint = result["fingerprint"]
                self.ssh_fingerprint.setText(result["fingerprint"])
                self.ssh_trust.setChecked(False)
                self.update_connection_mode()
                self.connect_button.setEnabled(True)
                self.connection_label.setText("请核对主机指纹，勾选确认后再次点击连接；尚未发送密码")
                return
            try:
                relay_url = local_endpoint(result["relay_url"])
                token = Path(self.station_token_file).read_text().strip()
            except (KeyError, OSError, ValueError, TypeError, AttributeError):
                fail("电脑后台连接信息无效，请刷新后台连接后重试")
                return
            self.ssh_password.clear()
            self.update_station_connections(result, result.get("robot", {}).get("robot_id"))
            self.operations_panel.server.setText(self.station_url)
            self.operations_panel.token.setText(token)
            self.url.setText(relay_url)
            self.preflight_source(relay_url, "relay", generation)

        def fail(message):
            if current():
                self.connect_button.setEnabled(True)
                self.connection_label.setText(message)

        worker.done.connect(complete)
        worker.failed.connect(fail)
        worker.finished.connect(lambda: _PROBES.discard(worker))
        _PROBES.add(worker)
        worker.start()

    def connect_source(self):
        mode = self.connection_mode.currentData()
        if mode == "ssh_station":
            self.connect_station()
            return
        try:
            if mode == "relay":
                url, args = local_endpoint(self.url.text()), None
            else:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    local_port = sock.getsockname()[1]
                args = ssh_arguments(
                    self.ssh_target.text(), self.ssh_key.text(), local_port, self.ssh_port.value()
                )
                url = f"http://127.0.0.1:{local_port}"
        except (ValueError, OSError) as exc:
            self.connection_label.setText(f"连接设置无效：{exc}")
            return
        if not args:
            self.begin_preview(url, mode)
            return
        self.disconnect_source()
        generation = self.connection_generation
        self.connect_button.setEnabled(False)
        self.connection_label.setText("正在只读预检服务身份与机器人来源…")
        if args:
            self.ssh = QProcess(self)
            ssh = self.ssh
            ssh.setProgram("ssh")
            ssh.setArguments(args)
            ssh.started.connect(lambda: self.preflight_source(url, mode, generation))

            def ssh_failed(*_):
                if self.ssh is ssh and self.connection_generation == generation:
                    self.disconnect_source()
                    self.connection_label.setText(
                        "临时SSH隧道未建立或已退出；请在终端核对主机指纹、密钥和机器人采集端口后重试"
                    )

            ssh.errorOccurred.connect(ssh_failed)
            ssh.finished.connect(ssh_failed)
            ssh.start()
        else:
            self.preflight_source(url, mode, generation)

    def preflight_source(self, url, mode, generation):
        if generation != self.connection_generation:
            return
        worker = ConnectionProbe(url, mode)
        self.probe = worker

        def current():
            from shiboken6 import isValid

            return isValid(self) and not self._closing and generation == self.connection_generation

        def complete(identity):
            if not current():
                return
            if self.reader:
                reader, self.reader = self.reader, None
                reader.stop()
            if mode == "relay" and self.ssh:
                ssh, self.ssh = self.ssh, None
                ssh.terminate()
                ssh.waitForFinished(1500)
            self.active_source_url = url
            self.station_robot_id = identity["robot_id"] if identity["robot_id"] in self.station_connections else None
            if self.station_robot_id:
                self.station_robots.blockSignals(True)
                self.station_robots.setCurrentIndex(self.station_robots.findData(self.station_robot_id))
                self.station_robots.blockSignals(False)
            if hasattr(self, "recordings_panel"):
                self.recordings_panel.set_robot(identity["robot_id"])
            ws_url = (
                url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                + "/api/v1/telemetry"
            )
            if self.buffer_robot_id != identity["robot_id"]:
                self.buffers.clear()
            self.buffer_robot_id = identity["robot_id"]
            self._buffer_resume = True
            if getattr(self, "thermal_dialog", None):
                self.thermal_dialog.close()
            self.last_frame = None
            self.last_received = 0
            self.health_expired = False
            self.render_health(None)
            self.joint_inspection_panel.mark_disconnected()
            self.operations_panel.mark_disconnected()
            factory = getattr(self, "factory_panel", None)
            if factory and hasattr(factory, "mark_disconnected"):
                factory.mark_disconnected()
            self.source_label.setText("模拟数据 · 非实机" if identity["source_kind"] == "simulated" else "ROS2来源 · 实机待验收")
            self.upload_label.setText("中央上报：等待所选机器人实时数据")
            self.quality_label.setText("所选机器人数据待接入；新鲜度未知")
            self.reader = Reader(ws_url)
            reader = self.reader

            def reader_current():
                from shiboken6 import isValid

                return isValid(self) and not self._closing and self.reader is reader

            def receive(frame):
                if reader_current():
                    relay = frame.get("upload", {}).get("stage") == "upper_computer"
                    if (
                        frame.get("robot_id") != identity["robot_id"]
                        or frame.get("source_kind") != identity["source_kind"]
                        or relay != (mode == "relay")
                    ):
                        self.disconnect_source()
                        self.connection_label.setText("实时数据的机器人身份或来源与预检不一致，已断开；请核对地址后重连")
                        return
                    self.on_frame(frame)

            self.reader.frame_received.connect(receive)
            self.reader.status.connect(
                lambda status: self.connection_label.setText(status) if reader_current() else None
            )
            self.reader.start()
            self.connect_button.setEnabled(True)
            self.connection_label.setText("服务身份已核对，正在等待只读实时数据…")

        def fail(message):
            if not current():
                return
            self.connect_button.setEnabled(True)
            if not self.reader:
                self.disconnect_source()
            elif self.station_robot_id:
                self.station_robots.blockSignals(True)
                self.station_robots.setCurrentIndex(self.station_robots.findData(self.station_robot_id))
                self.station_robots.blockSignals(False)
            self.connection_label.setText(message)

        def finish():
            _PROBES.discard(worker)
            if current() and self.probe is worker:
                self.probe = None

        worker.done.connect(complete)
        worker.failed.connect(fail)
        worker.finished.connect(finish)
        _PROBES.add(worker)
        worker.start()

    def disconnect_source(self):
        self.connection_generation += 1
        self.station_request_generation += 1
        if self.probe:
            self.probe.requestInterruption()
            self.probe = None
        self.clear_preview()

    def clear_preview(self):
        """Release this window's reader without cancelling a different pending selection."""
        if self.reader:
            reader, self.reader = self.reader, None
            reader.stop()
        if self.ssh:
            ssh, self.ssh = self.ssh, None
            ssh.terminate()
            ssh.waitForFinished(1500)
        self.active_source_url = ""
        self.station_robot_id = None
        self.ssh_password.clear()
        self.last_received = 0
        self.last_frame = None
        self._buffer_resume = True
        self.health_expired = False
        self.connect_button.setEnabled(True)
        self.source_label.setText("来源待确认")
        self.connection_label.setText("监控读取已断开；独立后台采集与转发不受影响")
        self.upload_label.setText("中央上报：未连接，后台状态未知")
        self.upload_label.setStyleSheet("color:#576c88;")
        self.quality_label.setText("尚未连接；机器人数据新鲜度未知")
        self.render_health(None)
        self.joint_inspection_panel.mark_disconnected()
        if hasattr(self, "operations_panel"):
            self.operations_panel.mark_disconnected()
        factory = getattr(self, "factory_panel", None)
        if factory and hasattr(factory, "mark_disconnected"):
            factory.mark_disconnected()

    def calibration_connection(self):
        """Use only the selected station's existing binding, never the editable SSH field."""
        robot_id = (self.last_frame or {}).get("robot_id")
        row = self.station_connections.get(robot_id, {})
        if not row or row.get("relay_url", "").rstrip("/") != self.active_source_url.rstrip("/"):
            return {}
        if not self.station_token_file:
            return {}
        return {
            "robot_id": robot_id,
            "connected": row.get("connected") is True,
            "target": row.get("target", ""),
            "ssh_port": row.get("ssh_port", 22),
            "known_hosts": Path(self.station_token_file).with_name("known_hosts"),
            "relay_url": self.active_source_url,
            "generation": self.connection_generation,
        }

    def populate_metrics(self):
        self.metric_list.blockSignals(True)
        self.metric_list.clear()
        group = self.group.currentIndex()
        candidates = []
        for entry in self.catalog:
            key = entry["key"]
            match = (
                (
                    group == 0
                    and key.startswith("joint.")
                    and key.endswith((".position", ".policy_target"))
                )
                or (group == 1 and key.endswith(".velocity"))
                or (group == 2 and key.endswith(".torque"))
                or (group == 3 and key.startswith("imu.angular_velocity."))
                or (group == 4 and key.startswith("imu.quaternion."))
                or (
                    group == 5
                    and key.endswith((".temperature", ".motor_temperature", ".mos_temperature"))
                )
                or (group == 6 and key == "battery.voltage")
                or (group == 7 and key == "battery.current")
                or (group == 8 and key in {"battery.soc", "host.cpu.usage", "host.memory.usage"})
                or (group == 9 and key.startswith("process.") and key.endswith(".cpu_percent"))
                or (group == 10 and key.startswith("process.") and key.endswith(".rss_mib"))
                or (group == 11 and key.startswith("process.") and key.endswith(".threads"))
                or (
                    group == 12
                    and key.startswith("process.")
                    and key.endswith(".observed_restarts")
                )
                or (group == 13 and key.startswith("process.") and key.endswith(".alive"))
            )
            if match:
                candidates.append(entry)
        for index, entry in enumerate(candidates):
            item = QListWidgetItem(entry["label"])
            item.setData(Qt.ItemDataRole.UserRole, entry["key"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if index < 2 else Qt.CheckState.Unchecked)
            self.metric_list.addItem(item)
        self.metric_list.blockSignals(False)
        self.redraw()

    def on_frame(self, frame):
        self.joint_inspection_panel.set_frame(frame)
        self.operations_panel.set_frame(frame)
        factory = getattr(self, "factory_panel", None)
        if factory and hasattr(factory, "set_frame"):
            factory.set_frame(frame)
        self.last_received = time.monotonic()
        previous = self.last_frame
        if self.buffer_robot_id != frame["robot_id"]:
            self.buffers.clear()
            self.buffer_robot_id = frame["robot_id"]
        elif self._buffer_resume or (previous and previous["stream_id"] != frame["stream_id"]):
            for buffer in self.buffers.values():
                if buffer:
                    buffer.append((buffer[-1][0] + 0.001, np.nan))
        elif previous:
            gap = (
                datetime.fromisoformat(frame["sampled_at"])
                - datetime.fromisoformat(previous["sampled_at"])
            ).total_seconds()
            if gap > 0.3:
                before = datetime.fromisoformat(previous["sampled_at"]).timestamp() + 0.001
                for buffer in self.buffers.values():
                    buffer.append((before, np.nan))
        self.last_frame = frame
        self._buffer_resume = False
        self.health_expired = False
        self.render_health(frame)
        upload = frame.get("upload", {})
        if upload.get("stage") == "upper_computer":
            link = (
                "机器人网线链路已连接"
                if upload.get("collector_connected")
                else "机器人网线链路失联，当前数值未知"
            )
            self.connection_label.setText(f"上位机已连接 · {frame['robot_id']} · {link}")
        else:
            self.connection_label.setText(f"机器人采集端直读 · {frame['robot_id']} · 无中央上传")
        self.source_label.setText(
            "模拟数据 · 非实机" if frame["source_kind"] == "simulated" else "ROS2来源 · 实机待验收"
        )
        upload = frame.get("upload", {})
        error = (
            upload.get("collector_error")
            or upload.get("receiver_error")
            or upload.get("error")
            or upload.get("backfill_error")
        )
        warning = upload.get("warning")
        upload_state = (
            "异常" if error else "已确认入库" if upload.get("last_success_at") else "尚未确认"
        )
        if warning:
            upload_state = "已入历史，实时状态待核对"
        self.upload_label.setText(
            f"上位机上传：{upload_state} · 电脑待传 {upload.get('pending_windows', 0)} · 机器人待取 {upload.get('robot_pending_windows', 0)} · 机器人缓存丢弃 {upload.get('robot_discarded_windows', 0)}"
            if upload.get("stage") == "upper_computer"
            else f"机器人待上位机接收：{upload.get('pending_windows', 0)} 窗口；不直连中央"
        )
        self.upload_label.setStyleSheet(
            "color:#ae283a;"
            if error
            else "color:#7d5206;"
            if warning
            else "color:#176948;"
            if upload.get("last_success_at")
            else "color:#576c88;"
        )
        timestamp = datetime.fromisoformat(frame["sampled_at"]).timestamp()
        for key, value in frame["values"].items():
            self.buffers[key].append((timestamp, value if value is not None else np.nan))
        if [item["key"] for item in frame["catalog"]] != [item["key"] for item in self.catalog]:
            self.catalog = frame["catalog"]
            self.populate_metrics()
        joints = sorted(
            {int(key.split(".")[1]) for key in frame["values"] if key.startswith("joint.")}
        )
        self.table.setRowCount(len(joints))
        for row, joint in enumerate(joints):
            names = frame.get("joint_names", [])
            label = f"{joint} · {names[joint]}" if joint < len(names) else str(joint)
            cells = [label] + [
                "—"
                if frame["values"].get(f"joint.{joint}.{quantity}") is None
                else f"{frame['values'][f'joint.{joint}.{quantity}']:.4f}"
                for quantity in ("position", "velocity", "torque", "policy_target")
            ]
            for column, text in enumerate(cells):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(text)
        topics = frame.get("topic_ages_ms", {})
        status = " / ".join(
            f"{topic}: {'未收到' if age is None else f'{age:.0f}ms'}"
            for topic, age in topics.items()
        )
        offset = upload.get("clock_offset_seconds")
        clock_note = ""
        if (
            upload.get("clock_basis") == "relay_round_trip"
            and isinstance(offset, (int, float))
            and isfinite(offset)
        ):
            clock_note = f" · 机器人时钟偏差估计 {offset:+.2f}s；新鲜度按上位机往返观测估计，硬件采样时钟仍待核对"
        self.quality_label.setText(
            status
            + (
                f" · {error}"
                if error
                else f" · {warning}"
                if warning
                else " · 采集时间为主机接收时间"
            )
            + clock_note
            + (f" · {frame['mapping_note']}" if frame.get("mapping_note") else "")
        )
        self.quality_label.setStyleSheet(
            "color:#ae283a;" if error else "color:#7d5206;" if warning else "color:#576c88;"
        )
        self.redraw()

    def redraw(self):
        selected = [
            self.metric_list.item(index)
            for index in range(self.metric_list.count())
            if self.metric_list.item(index).checkState() == Qt.CheckState.Checked
        ]
        if len(selected) > 12:
            selected[-1].setCheckState(Qt.CheckState.Unchecked)
            return
        keys = {item.data(Qt.ItemDataRole.UserRole) for item in selected}
        for key in list(self.curves):
            if key not in keys:
                self.plot.removeItem(self.curves.pop(key))
        colors = ["#2b59e8", "#ac5a00", "#168064", "#9b3bb8", "#c33655", "#087f8c"]
        for index, item in enumerate(selected):
            key = item.data(Qt.ItemDataRole.UserRole)
            if key not in self.curves:
                self.curves[key] = self.plot.plot(
                    name=item.text(),
                    pen=pg.mkPen(colors[index % len(colors)], width=1.5),
                    connect="finite",
                )
            values = list(self.buffers[key])
            if values:
                array = np.asarray(values)
                self.curves[key].setData(array[:, 0] - values[-1][0], array[:, 1], connect="finite")
        self.plot.setXRange(-60, 0, padding=0)

    def check_stale(self):
        if self.reader and self.last_received and time.monotonic() - self.last_received > 2:
            self.connection_label.setText("预览已过期 · 当前健康数据不可用")
            if not self.health_expired:
                self.render_health(None, stale=True)
                self.joint_inspection_panel.mark_disconnected()
                self.operations_panel.mark_disconnected()
                factory = getattr(self, "factory_panel", None)
                if factory and hasattr(factory, "mark_disconnected"):
                    factory.mark_disconnected()
                self.health_expired = True
        elif self.reader and self.last_frame and not self.health_expired:
            self.render_host_cores(
                self.last_frame.get("health", {}).get("host", {}),
                available=self.last_frame.get("upload", {}).get("collector_connected", True),
            )

    def closeEvent(self, event):
        self._closing = True
        motor_control = getattr(self.joint_inspection_panel, "motor_control_dialog", None)
        if motor_control and not motor_control.shutdown():
            event.ignore()
            return
        postzero = getattr(self.joint_inspection_panel, "postzero_dialog", None)
        if postzero and not postzero.shutdown():
            event.ignore()
            return
        legacy_analysis = getattr(self.joint_inspection_panel, "limit_analysis", None)
        execution = getattr(legacy_analysis, "execution_panel", None)
        if execution and not execution.shutdown():
            event.ignore()
            return
        factory_panel = getattr(self, "factory_panel", None)
        if factory_panel and not factory_panel.shutdown():
            self.connection_label.setText("正在完成本机日志读写后关闭；机器人记录继续，已保存文件保留")
            QTimer.singleShot(100, self.close)
            event.ignore()
            return
        if not self._close_connections_stopped:
            self.disconnect_source()
            self.setEnabled(False)
            for timer in self.findChildren(QTimer):
                timer.stop()
            self._close_connections_stopped = True
        pending = pending_desktop_workers()
        if pending:
            for worker in pending:
                worker.requestInterruption()
            self.connection_label.setText("正在关闭本窗口预览连接并等待本机后台请求结束，请稍候；机器人采集不受影响")
            QTimer.singleShot(100, self.close)
            event.ignore()
            return
        event.accept()


def run(args):
    from .desktop_diagnostics import desktop_failure_diagnostics

    with desktop_failure_diagnostics(getattr(args, "station_dir", None) or Path("runtime/station")):
        _run_desktop(args)


def _run_desktop(args):
    from .app_shell import AppMonitorWindow
    from .central_policy import load_central_policy
    from .motor_map import MotorMapConfig

    station = None
    station_error = ""
    if getattr(args, "quick_connect", False) and not getattr(args, "factory", False):
        from .station import ensure_station

        try:
            station = ensure_station(args.station_dir, args.station_port)
        except Exception as exc:
            station_error = f"电脑后台暂不可用：{type(exc).__name__}；工厂本地导入、曲线和报告仍可使用。"
    app = QApplication.instance() or QApplication([])
    window = AppMonitorWindow(
        args.url,
        motor_map_config=MotorMapConfig.load(args.motor_map),
        thermal_policy=load_central_policy(args.policy),
        factory_directory=getattr(args, "factory_dir", None),
    )
    window.show()
    if getattr(args, "factory", False) or station_error:
        window.navigate(9)
        if station_error:
            window.factory_panel.notice.setText(station_error + "\n" + window.factory_panel.notice.text())
    elif station:
        window.station_url, window.station_token_file = station
        window.connection_mode.setCurrentIndex(2)
        window.ssh_target.setText("ssh robo@192.168.137.250")
        window.open_settings()
        window.ssh_password.setFocus()
        QTimer.singleShot(0, window.restore_station_connection)
    else:
        QTimer.singleShot(0, window.connect_source)
    if args.screenshot:

        def screenshot():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.screenshot))

        QTimer.singleShot(max(1500, int((args.quit_after or 3) * 1000) - 250), screenshot)
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), window.close)
    app.exec()
    # Normal close has already drained these asynchronously. Keep the safety net
    # for a direct QApplication.quit(), which bypasses the window's closeEvent.
    window._closing = True
    window.disconnect_source()
    workers = pending_desktop_workers()
    for worker in workers:
        worker.requestInterruption()
    for worker in workers:
        worker.wait()
    window.factory_panel.shutdown()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
