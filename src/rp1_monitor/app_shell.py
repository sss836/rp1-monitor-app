"""App-style presentation of the existing read-only Qt monitor."""

from collections import deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QByteArray, QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from .desktop import STYLE, GridBackground, MonitorWindow
from .health_view import build_health
from .motor_map import MotorMapConfig, attach_motor_map, build_motor_map
from .process_monitor import build_process_view

APP_STYLE = (
    STYLE
    + """
QFrame#sidebar { background:#152337; border:0; border-radius:8px; }
QLabel#navBrand { color:#ffffff; font-size:14px; font-weight:700; padding:4px 7px; }
QLabel#navGroup { color:#a7b9d2; font-size:12px; padding:5px 8px 1px; }
QPushButton#nav { background:transparent; color:#cad6e8; border:1px solid transparent; border-radius:5px; text-align:left; padding:5px 8px; font-size:14px; }
QPushButton#nav:hover { background:#213652; }
QPushButton#nav:checked { background:#28446e; color:#ffffff; font-weight:700; border-left:3px solid #77a8ff; }
QPushButton#nav:focus { border:1px solid #8cbbff; }
QFrame#healthCard, QFrame#robotOverview, QFrame#deviceContext { background:#ffffff; border:1px solid #dce5ef; border-radius:8px; }
QLabel#pageTitle { color:#142337; font-size:24px; font-weight:700; }
QLabel#deviceIdentity { color:#142337; font-size:14px; font-weight:700; }
QPushButton#textLink { background:transparent; color:#2b59e8; border:0; text-align:left; padding:4px 0; font-size:13px; }
QPushButton#textLink:focus { border:1px solid #2b59e8; }
QLabel#healthValue { color:#142441; font-size:27px; font-weight:700; }
QLabel#healthOverall { font-size:13px; }
QLabel#source { font-size:12px; padding:5px 8px; }
QLabel#cardCaption { color:#576c88; font-size:12px; }
QLabel#sshFingerprint { background:#f4f7fc; color:#263e61; border:1px solid #d4e2f5; border-radius:6px; padding:8px 10px; }
QLabel#heroId { color:#142441; font-size:23px; font-weight:700; }
QLabel#callout { background:#ffffff; border:1px solid #c2d7f6; border-radius:8px; color:#2853c6; font-size:13px; padding:8px; }
QProgressBar { background:#edf3ff; border:1px solid #d0e0fa; border-radius:5px; height:12px; }
QProgressBar::chunk { background:#3370ee; border-radius:4px; }
"""
)

MOTOR_MAINTENANCE_PAGE = 10

ICON_PATHS = {
    "home": '<path d="M3 11 12 3l9 8M5 10v11h5v-7h4v7h5V10"/>',
    "parts": '<path d="m12 3 9 5v9l-9 5-9-5V8zM3 8l9 5 9-5M12 13v9"/>',
    "trend": '<path d="M3 19V5M3 19h18M6 15l4-5 4 3 6-8"/>',
    "bell": '<path d="M5 17h14l-2-3V9a5 5 0 0 0-10 0v5zM10 21h4M12 2v2"/>',
    "settings": '<path d="M4 7h16M4 17h16M8 4v6M16 14v6"/>',
    "chat": '<path d="M4 4h16v12H9l-5 4zM8 8h8M8 12h5"/>',
    "checks": '<path d="M8 4h12v17H4V4h4M8 3h8v4H8zM8 12l2 2 5-5M8 18h8"/>',
    "battery": '<rect x="3" y="6" width="17" height="12" rx="2"/><path d="M23 10v4M7 10v4M11 10v4"/>',
    "temperature": '<path d="M9 14V5a3 3 0 0 1 6 0v9a5 5 0 1 1-6 0M12 8v10"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
}


def icon(name):
    result = QIcon()
    for checked, color in ((False, "#b3c5df"), (True, "#ffffff")):
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{ICON_PATHS[name]}</svg>'
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(QByteArray(svg.encode())).render(painter)
        painter.end()
        result.addPixmap(pixmap, QIcon.Mode.Normal, QIcon.State.On if checked else QIcon.State.Off)
    return result


def label(text="", name="subtitle"):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


def panel(title):
    widget = QFrame()
    widget.setObjectName("healthCard")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(14, 10, 14, 10)
    layout.setSpacing(7)
    if title:
        layout.addWidget(label(title, "sectionTitle"))
    return widget, layout


def _set_text(widget, text):
    if widget.text() != text:
        widget.setText(text)


def _set_style(widget, style):
    if widget.styleSheet() != style:
        widget.setStyleSheet(style)


class RobotOverview(QWidget):
    region_clicked = Signal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(176)
        assets = Path(__file__).parent / "web" / "brand"
        self.pixmap = QPixmap(str(assets / "robot-wave.webp"))
        self.setAccessibleName("RP1机型示意，非实时姿态，物理部位映射待确认")
        self.regions = []
        self.markers = []
        self._scaled_image = None
        self._scaled_image_size = None

    def set_heat_map(self, view):
        description = view["mapping_note"] + " · " + view["summary"]
        if self.regions == view["regions"] and self.accessibleDescription() == description:
            return
        old_keys = [region["key"] for region in self.regions]
        self.regions = view["regions"]
        if old_keys != [region["key"] for region in self.regions]:
            for marker in self.markers:
                marker.hide()
                marker.deleteLater()
            self.markers = [QPushButton("") for _ in self.regions]
            for marker, region in zip(self.markers, self.regions):
                marker.setParent(self)
                marker.clicked.connect(
                    lambda checked=False, key=region["key"]: self.region_clicked.emit(key)
                )
                marker.show()
        for region, marker in zip(self.regions, self.markers):
            _set_text(marker, "—" if region["value"] is None else f"{region['value']:.0f}°")
            marker.setToolTip(region["label"] + "\n" + region["detail"])
            marker.setAccessibleName(region["label"] + " · " + region["detail"])
            _set_style(marker,
                f"background:{region['color']}; color:{region['text_color']}; border:2px solid {'#ae283a' if region['state'] == 'hot' else '#65758c'}; border-radius:9px; padding:1px; font-size:11px;"
            )
        self.setAccessibleDescription(description)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f1f6ff"))
        painter.drawEllipse(5, 25, max(1, self.width() - 10), max(1, self.height() - 45))
        image_size = (max(1, self.width() - 30), max(1, self.height() - 18), self.pixmap.cacheKey())
        if self._scaled_image_size != image_size:
            self._scaled_image = self.pixmap.scaled(
                image_size[0], image_size[1], Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._scaled_image_size = image_size
        image = self._scaled_image
        left, top = max(0, (self.width() - image.width()) // 2), 8
        painter.drawPixmap(left, top, image)
        for region, marker in zip(self.regions, self.markers):
            point = QPointF(left + image.width() * region["x"], top + image.height() * region["y"])
            tint = QColor(region["color"])
            tint.setAlpha(160)
            painter.setBrush(tint)
            painter.setPen(QPen(QColor("#ae283a" if region["state"] == "hot" else "#65758c"), 2))
            painter.drawEllipse(point, image.width() * 0.075, image.height() * 0.034)
            marker.setGeometry(round(point.x() - 16), round(point.y() - 11), 32, 22)
        painter.end()


class AppMonitorWindow(MonitorWindow):
    """Reuse collection, SSH, freshness and rendering; replace presentation only."""

    def __init__(self, url, motor_map_config=None, thermal_policy=None, factory_directory=None):
        from .central_policy import CentralPolicy

        self.motor_map_config = motor_map_config or MotorMapConfig()
        self.thermal_policy = thermal_policy or CentralPolicy()
        self.thermal_dialog = None
        self._automatic_central_url = "http://127.0.0.1:8890"
        self._current_motor_map = {}
        self.shell_ready = False
        self.factory_guidance = None
        self.hardware_notes_dialog = None
        super().__init__(url)
        self.setStyleSheet(APP_STYLE)
        old_root = self.takeCentralWidget()
        trend_page = self.tabs.widget(1)
        self.tabs.removeTab(1)
        self.joint_feedback_title.hide()
        self.tabs.removeTab(self.tabs.indexOf(self.joint_inspection_panel))
        self.joint_inspection_panel.setParent(None)
        self.session_events = deque(maxlen=100)
        self.last_event = None
        root = GridBackground()
        root.setProperty("plainBackground", True)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(176)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(9, 13, 9, 10)
        side.setSpacing(5)
        brand = QLabel()
        logo = QPixmap(str(Path(__file__).parent / "web" / "brand" / "logo.png"))
        if not logo.isNull():
            mark = logo.scaled(112, 38, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter = QPainter(mark)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(mark.rect(), QColor("#ffffff"))
            painter.end()
            brand.setPixmap(mark)
        brand.setAccessibleName("ROBO PARTY")
        side.addWidget(brand, alignment=Qt.AlignmentFlag.AlignHCenter)
        side.addWidget(label("RP1 整机测试工作台", "navBrand"))
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        # Keep IDs stable for factory tools, startup and cross-page links.
        titles = ("整机概览", "部件监测", "实时趋势", "告警与状态", "设备设置", "诊断助手", "硬件自检", "监控记录", "角度与标定", "整机测试", "电机维护")
        glyphs = ("home", "parts", "trend", "bell", "settings", "chat", "checks", "trend", "settings", "checks", "settings")
        nav_body = QWidget()
        nav_layout = QVBoxLayout(nav_body)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(2)

        def nav_button(index):
            button = QPushButton(titles[index])
            button.setObjectName("nav")
            button.setIcon(icon(glyphs[index]))
            button.setIconSize(QSize(18, 18))
            button.setCheckable(True)
            button.setMinimumHeight(33)
            self.nav.addButton(button, index)
            return button

        for group, indices in (("监控", (0, 1, 2)), ("记录", (7,)), ("排查", (3, 5)), ("测试与维护", (9, 6, 8, MOTOR_MAINTENANCE_PAGE))):
            nav_layout.addWidget(label(group, "navGroup"))
            for index in indices:
                nav_layout.addWidget(nav_button(index))
        nav_layout.addStretch()
        nav_scroll = QScrollArea()
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setWidget(nav_body)
        side.addWidget(nav_scroll, 1)
        side.addWidget(nav_button(4))
        layout.addWidget(sidebar)
        body = QVBoxLayout()
        body.setSpacing(8)
        device_context = QFrame()
        device_context.setObjectName("deviceContext")
        context_row = QHBoxLayout(device_context)
        context_row.setContentsMargins(11, 5, 7, 5)
        self.device_id = label("尚未连接机器人", "deviceIdentity")
        context_row.addWidget(self.device_id)
        context_row.addWidget(self.source_label)
        context_row.addWidget(self.health_overall, 1)
        record_state = label("记录待核对", "cardCaption")
        record_state.setToolTip("最近一次后台记录状态，以记录页选择的机器人为准。")
        context_row.addWidget(record_state)
        switch = QPushButton("管理连接")
        switch.setObjectName("secondary")
        switch.clicked.connect(self.open_settings)
        context_row.addWidget(switch)
        body.addWidget(device_context)
        self.app_title = label("整机概览", "pageTitle")
        body.addWidget(self.app_title)
        self.factory_return_bar = QFrame()
        self.factory_return_bar.setObjectName("healthBanner")
        return_layout = QVBoxLayout(self.factory_return_bar)
        context = QHBoxLayout()
        self.factory_return_label = label("", "sectionTitle")
        self.factory_tool_hint = label()
        context.addWidget(self.factory_return_label, 1)
        self.factory_connection_button = QPushButton("前往设备连接")
        self.factory_connection_button.clicked.connect(self.open_settings)
        context.addWidget(self.factory_connection_button)
        self.factory_return_button = QPushButton("返回工厂保存结果")
        self.factory_return_button.clicked.connect(self.return_factory_result)
        context.addWidget(self.factory_return_button)
        return_layout.addLayout(context)
        self.factory_tool_hint.setMinimumHeight(54)
        return_layout.addWidget(self.factory_tool_hint)
        self.factory_return_bar.hide()
        body.addWidget(self.factory_return_bar)
        self.pages = QStackedWidget()
        self.pages.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        from .desktop_recordings import RecordingsPanel

        self.recordings_panel = RecordingsPanel(lambda: (self.station_url, self.station_token_file))
        self.recordings_panel.recording_status.connect(record_state.setText)
        self.pages.addWidget(self.home_page())
        self.pages.addWidget(self.components_page())
        trend, trend_layout = panel("")
        trend_body = QHBoxLayout()
        self.curve_controls.setMinimumWidth(235)
        self.curve_controls.setMaximumWidth(285)
        self.curve_controls.show()
        trend_body.addWidget(trend_page, 1)
        trend_body.addWidget(self.curve_controls)
        trend_page.show()
        trend_layout.addLayout(trend_body)
        self.pages.addWidget(trend)
        self.pages.addWidget(self.alert_page())
        self.pages.addWidget(self.settings_page())
        self.pages.addWidget(self.operations_panel)
        self.pages.addWidget(self.operations_panel.board_panel)
        self.pages.addWidget(self.recordings_panel)
        self.pages.addWidget(self.joint_inspection_panel)
        from .desktop_factory import create_factory_panel

        self.factory_panel = create_factory_panel(
            lambda: self.active_source_url or self.url.text(), factory_directory,
            lambda: {**self.calibration_connection(), "station_url": self.station_url,
                     "station_token_file": self.station_token_file},
        )
        self.pages.addWidget(self.factory_panel)
        from .desktop_motor_maintenance import MotorMaintenancePanel

        self.motor_maintenance_panel = MotorMaintenancePanel()
        self.pages.addWidget(self.motor_maintenance_panel)
        self._motor_maintenance_binding = None
        self.motor_maintenance_panel.report_requested.connect(self.open_motor_maintenance_report)
        self.motor_maintenance_panel.busy_changed.connect(self.refresh_motor_maintenance)
        if hasattr(self.factory_panel, "tool_requested"):
            self.factory_panel.tool_requested.connect(self.open_factory_tool)
            self.factory_panel.sn_changed.connect(self.factory_sn_changed)
            self.factory_panel.board_saved.connect(self.factory_board_saved)
            self.factory_panel.model.currentIndexChanged.connect(self.refresh_motor_maintenance)
        self.factory_board_runs = {}
        self.joint_inspection_panel.manual_dialog_created.connect(self.connect_factory_manual_dialog)
        board = self.operations_panel.board_panel
        board.native_context = lambda: {**self.calibration_connection(), "station_url": self.station_url,
                                        "station_token_file": self.station_token_file}
        if hasattr(self.factory_panel, "store"):
            board.set_board_profile(self.factory_panel.board_profile_values)
            board.profile_saver = self.factory_panel.save_board_profile
        board.factory_sync.clicked.connect(self.sync_existing_board_results)
        board.run_requested.connect(self.factory_board_run_requested)
        board.report_ready.connect(self.factory_board_report_ready)
        self.operations_panel.board_checks_requested.connect(lambda: self.navigate(6))
        body.addWidget(self.pages, 1)
        footer = QHBoxLayout()
        footer.addWidget(self.connection_label, 1)
        footer.addWidget(self.upload_label, 1)
        body.addLayout(footer)
        layout.addLayout(body, 1)
        self.nav.idClicked.connect(self.navigate)
        self.pages.currentChanged.connect(lambda index: self.render_temperature_curves() if index == 0 else None)
        self.nav.button(0).setChecked(True)
        self.setCentralWidget(root)
        # All state-bound controls above now belong to the new shell.
        old_root.deleteLater()
        self.tabs = self.pages
        self.shell_ready = True
        self.motor_table.cellActivated.connect(self.activate_motor_temperature)
        self.motor_table.setToolTip("双击关节或按Enter，查看电机/MOS曲线与告警时间")
        self.group.setCurrentIndex(5)
        self.render_health(None)

    def home_page(self):
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        page = QWidget()
        main = QVBoxLayout(page)
        main.setContentsMargins(0, 0, 3, 0)
        main.setSpacing(9)
        hero, hero_layout = panel("")
        hero.setObjectName("robotOverview")
        hero.setMinimumWidth(215)
        hero.setMaximumWidth(320)
        self.hero_id = label("RP1", "heroId")
        hero_layout.addWidget(self.hero_id)
        hero_layout.addWidget(label("机器人概览"))
        self.robot_overview = RobotOverview()
        self.robot_overview.setMaximumHeight(198)
        self.robot_overview.region_clicked.connect(self.open_temperature_region)
        hero_layout.addWidget(self.robot_overview, 1)
        self.motor_map_status = label("温度分布待接入", "cardCaption")
        hero_layout.addWidget(self.motor_map_status)
        scale = QFrame()
        scale.setFixedHeight(8)
        scale.setStyleSheet(
            "border:1px solid #dbb3b5; border-radius:4px; background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #fff5f0,stop:0.333 #ffcfb8,stop:0.667 #ef6b6e,stop:1 #8e1235);"
        )
        hero_layout.addWidget(scale)
        self.motor_map_legend = label(
            "浅色 → 深红：温度升高\n灰色：未知 · 数值单位 °C", "cardCaption"
        )
        hero_layout.addWidget(self.motor_map_legend)
        hero_layout.addWidget(label("机型示意 · 非实时姿态", "cardCaption"))
        right = QVBoxLayout()
        right.setSpacing(9)
        cards = QGridLayout()
        cards.setSpacing(9)
        battery, battery_layout = panel("动力电池")
        battery_layout.addWidget(self.battery_soc)
        self.battery_bar = QProgressBar()
        self.battery_bar.setRange(0, 1000)
        self.battery_bar.setTextVisible(False)
        self.battery_bar.setFixedHeight(13)
        battery_layout.addWidget(self.battery_bar)
        self.battery_summary = label()
        battery_layout.addWidget(self.battery_summary)
        battery_layout.addWidget(self.health_status["battery"])
        temperature, temperature_layout = panel("温度监测")
        self.temperature_value = label("—", "healthValue")
        temperature_layout.addWidget(self.temperature_value)
        temperature_layout.addWidget(label("最高电机温度 · 按已接入数据"))
        all_temps = QPushButton("查看全部温度 →")
        all_temps.setObjectName("textLink")
        all_temps.setMinimumHeight(36)
        all_temps.clicked.connect(lambda: self.open_components(0))
        temperature_layout.addWidget(all_temps)
        host, host_layout = panel("计算主机")
        host_layout.addWidget(self.host_temperature)
        self.host_summary = label()
        host_layout.addWidget(self.host_summary)
        self.process_summary = label("核心进程 · 等待观测", "cardCaption")
        host_layout.addWidget(self.process_summary)
        self.host_core_summary = label("每核使用率 · 采集端尚未提供", "cardCaption")
        host_layout.addWidget(self.health_status["host"])
        motors, motor_layout = panel("电机与通信")
        motor_layout.addWidget(self.motor_coverage)
        motor_layout.addWidget(label("温度与故障反馈覆盖"))
        self.motor_summary = label()
        motor_layout.addWidget(self.motor_summary)
        motor_layout.addWidget(self.health_status["motors"])
        for column, widget in enumerate((battery, host, temperature, motors)):
            cards.setColumnStretch(column, 1)
            cards.addWidget(widget, 0, column)
        main.addLayout(cards)
        banner, banner_layout = panel("")
        banner_layout.setContentsMargins(14, 10, 14, 10)
        self.health_detail.setStyleSheet("color:#7d5206; font-size:12px;")
        banner_row = QHBoxLayout()
        banner_row.addWidget(self.health_detail, 1)
        details = QPushButton("查看详情 →")
        details.setObjectName("textLink")
        details.clicked.connect(lambda: self.navigate(3))
        banner_row.addWidget(details)
        banner_layout.addLayout(banner_row)
        right.addWidget(banner)
        trend, trend_layout = panel("温度趋势 · 最近60秒")
        trend_layout.setContentsMargins(14, 10, 14, 10)
        self.health_plot = pg.PlotWidget()
        self.health_plot.setMinimumHeight(164)
        self.health_plot.setMaximumHeight(210)
        self.health_plot.showGrid(x=True, y=True, alpha=0.12)
        self.health_plot.setLabel("left", "°C")
        self.health_plot.setMouseEnabled(x=False, y=False)
        self.health_plot.setMenuEnabled(False)
        self.health_plot.addLegend(offset=(8, 0))
        self.temp_curves = [
            self.health_plot.plot(name=name, pen=pg.mkPen(color, width=1.7), connect="finite")
            for name, color in (("主机最高热区", "#2b59e8"), ("最高电机温度", "#ac5a00"))
        ]
        self.temp_buffers = [deque(maxlen=600), deque(maxlen=600)]
        self._temperature_curves_dirty = False
        trend_layout.addWidget(self.health_plot)
        right.addWidget(trend)
        right.addStretch()
        lower = QHBoxLayout()
        lower.addWidget(hero, 2)
        lower.addLayout(right, 5)
        main.addLayout(lower)
        main.addStretch()
        scroll.setWidget(page)
        return scroll

    def components_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        summaries = QHBoxLayout()
        battery, left = panel("电池与供电")
        left.addWidget(self.battery_metrics)
        left.addWidget(self.battery_age)
        summaries.addWidget(battery, 1)
        host_summary, host_summary_layout = panel("主机与进程")
        host_summary_layout.addWidget(self.host_usage)
        host_summary_layout.addWidget(self.host_core_summary)
        host_summary_layout.addStretch()
        summaries.addWidget(host_summary, 1)
        layout.addLayout(summaries)

        self.component_tabs = QTabWidget()
        layout.addWidget(self.component_tabs, 1)
        host, right = panel("主机热区与监测来源")
        right.addWidget(self.host_core_note)
        right.addWidget(self.host_core_table)
        self.host_sensors.setMaximumHeight(130)
        right.addWidget(self.host_sensors)
        right.addWidget(self.health_sources_label)
        right.addWidget(label("核心进程 · 只读 / 每2秒采样", "sectionTitle"))
        self.process_note = label("未配置进程名单", "cardCaption")
        right.addWidget(self.process_note)
        self.process_table = QTableWidget(0, 7)
        self.process_table.setHorizontalHeaderLabels(
            ["监测进程", "状态", "CPU / %", "RSS / MiB ≈", "线程", "观察重启", "年龄 / s"]
        )
        self.process_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.process_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.process_table.verticalHeader().hide()
        self.process_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.process_table.setMinimumHeight(135)
        self.process_table.setMaximumHeight(170)
        right.addWidget(self.process_table)
        right.addStretch()
        host_scroll = QScrollArea()
        host_scroll.setFrameShape(QFrame.Shape.NoFrame)
        host_scroll.setWidgetResizable(True)
        host_scroll.setWidget(host)
        temperatures, temperature_layout = panel("")
        temperature_layout.addWidget(self.motor_metrics)
        self.motor_table.verticalHeader().hide()
        self.motor_table.setMaximumHeight(16777215)
        temperature_layout.addWidget(self.motor_table, 1)
        temperature_layout.addWidget(self.motor_note)
        self.component_tabs.addTab(temperatures, "电机温度与故障")
        feedback, feedback_layout = panel("")
        feedback_layout.addWidget(self.table, 1)
        feedback_layout.addWidget(self.quality_label)
        self.component_tabs.addTab(feedback, "关节反馈")
        self.component_tabs.addTab(host_scroll, "主机热区与进程")
        return page

    def open_components(self, index):
        self.component_tabs.setCurrentIndex(index)
        self.navigate(1)

    def alert_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        current, current_layout = panel("当前需要关注")
        self.current_alerts = label()
        current_layout.addWidget(self.current_alerts)
        layout.addWidget(current)
        history, history_layout = panel("本次窗口状态变化")
        history_layout.addWidget(label("最多100条。关闭窗口后不保存。"))
        self.event_list = QListWidget()
        self.event_list.setFixedHeight(70)
        history_layout.addWidget(self.event_list)
        layout.addWidget(history)
        layout.addStretch()
        return page

    def settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 4, 0)
        connections, connection_layout = panel("后台机器人与本窗口预览")
        preview = QHBoxLayout()
        preview.addWidget(self.station_robots, 1)
        for button in (self.station_refresh, self.disconnect_button, self.station_disconnect):
            button.setObjectName("secondary")
            preview.addWidget(button)
        connection_layout.addLayout(preview)
        connection_layout.addWidget(label("停止本窗口预览只影响此窗口。断开后台机器人会停止接收该机器人的数据。"))
        layout.addWidget(connections)

        connection, fields_layout = panel("添加连接")
        field_box = QWidget()
        field_box.setMaximumWidth(760)
        form = QFormLayout(field_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        for title, widget in (
            ("连接方式", self.connection_mode),
            ("上位机服务", self.url),
            ("SSH目标", self.ssh_target),
            ("SSH密码", self.ssh_password),
            ("主机指纹", self.ssh_fingerprint),
            ("", self.ssh_fingerprint_help),
            ("身份确认", self.ssh_trust),
            ("SSH密钥", self.ssh_key),
            ("机器人采集端口", self.ssh_port),
        ):
            form.addRow(title, widget)
        self.connection_form = form
        fields_layout.addWidget(field_box)
        fields_layout.addWidget(self.connection_help)
        buttons = QHBoxLayout()
        buttons.addWidget(self.connect_button)
        buttons.addStretch()
        fields_layout.addLayout(buttons)
        layout.addWidget(connection)

        management = QHBoxLayout()
        storage, storage_layout = panel("长期历史库")
        storage_layout.addWidget(self.recordings_panel.database_status)
        self.recordings_panel.configure_database.setObjectName("secondary")
        storage_layout.addWidget(self.recordings_panel.configure_database, alignment=Qt.AlignmentFlag.AlignLeft)
        storage_layout.addStretch()
        management.addWidget(storage, 1)
        tools, tools_layout = panel("监控网页与采集端维护")
        web = QPushButton("打开监控网页")
        web.setObjectName("secondary")
        self.quick_web_button = web
        def open_web():
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            if self.station_url:
                QDesktopServices.openUrl(QUrl(self.station_url + "/recordings"))
        web.clicked.connect(open_web)
        self.connection_summary = label(
            "上位机服务填写 relay 的根地址，不是中央网页地址。缺失、过期数据保持未知。"
        )
        tools_layout.addWidget(self.connection_summary)
        self.collector_manager_button = QPushButton("机器人采集端管理…")
        self.collector_manager_button.setObjectName("secondary")
        self.collector_manager_button.setAccessibleName("机器人采集端安装更新启动停止卸载")
        self.collector_manager_button.clicked.connect(self.open_collector_manager)
        maintenance = QHBoxLayout()
        maintenance.addWidget(web)
        maintenance.addWidget(self.collector_manager_button)
        maintenance.addStretch()
        tools_layout.addLayout(maintenance)
        tools_layout.addWidget(label("卸载采集端会清除机器人上的采集工具及其测试数据。", "cardCaption"))
        management.addWidget(tools, 2)
        layout.addLayout(management)
        layout.addStretch()
        self.update_connection_mode()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    def navigate(self, index):
        if index == 7:
            self.recordings_panel.refresh_context()
        if self.pages.currentIndex() == MOTOR_MAINTENANCE_PAGE and index != MOTOR_MAINTENANCE_PAGE:
            self.clear_motor_maintenance_session()
        self.pages.setCurrentIndex(index)
        self.nav.button(index).setChecked(True)
        self.app_title.setText(self.nav.button(index).text())
        if index == MOTOR_MAINTENANCE_PAGE:
            self.refresh_motor_maintenance()
        self.update_factory_guidance()

    def clear_motor_maintenance_session(self):
        self._motor_maintenance_binding = None
        self.motor_maintenance_panel.clear_credentials()

    def refresh_motor_maintenance(self, *_args):
        if not self.shell_ready or self.pages.currentIndex() != MOTOR_MAINTENANCE_PAGE:
            return
        panel = self.motor_maintenance_panel
        if panel.busy:
            return
        factory = self.factory_panel
        if not hasattr(factory, "motor_maintenance_context"):
            panel.configure({}, "", None, None, error="工厂本地目录暂不可用。")
            return
        context = factory.motor_maintenance_context()
        sn, dof = factory.sn, factory.model.currentData()
        frame = factory.live_frame or {}
        profile = deepcopy(factory.board_profile_values)
        session_key = (frame.get("stream_id"), profile)
        binding = (sn, dof, context, frame.get("robot_id"), frame.get("source_kind"), session_key,
                   frame.get("upload", {}).get("collector_connected"))
        if self._motor_maintenance_binding == binding:
            return
        self._motor_maintenance_binding = binding
        panel.clear_credentials()
        try:
            factory.validate_motor_maintenance_context(context)
        except ValueError as error:
            panel.configure({}, sn, dof, None, error=str(error))
            return
        def save_parameters(bound_sn, request, result):
            factory.save_motor_parameter_report(bound_sn, request, result, board_test_profile=profile)

        def save_firmware(bound_sn, request, result):
            factory.save_motor_firmware_report(bound_sn, request, result, board_test_profile=profile)

        panel.configure(context, sn, dof, save_parameters, save_firmware_report=save_firmware, session_key=session_key)

    def open_motor_maintenance_report(self, sn, section):
        factory = self.factory_panel
        if sn and hasattr(factory, "show_motor_parameter_report") and sn == factory.sn:
            self.navigate(9)
            factory.show_motor_parameter_report(sn, section)

    def open_factory_tool(self, target, item):
        if target == "hardware_notes":
            self.open_factory_hardware_notes()
            return
        if target in ("motor_firmware", "motor_parameters"):
            section = "firmware" if target == "motor_firmware" else "parameters"
            self.factory_guidance = {
                "sn": self.factory_panel.sn, "item": item,
                "case_code": "SYS-003" if section == "firmware" else "SYS-004",
                "hint": "使用当前SN对应的连接，维护记录保存在本机。",
            }
            self.navigate(MOTOR_MAINTENANCE_PAGE)
            self.motor_maintenance_panel.show_section(section)
            return
        if target == "zero_reference_check":
            factory = self.factory_panel
            sn = factory.sn
            if not sn:
                factory.notice.setText("请先选择当前样品SN，再打开标零位复查。")
                return
            panel = self.joint_inspection_panel
            context = panel.calibration_context()
            if context.get("can_preflight") is not True:
                reason = "；".join(context.get("issues") or [])
                factory.notice.setText("请先在设备设置连接本SN对应的机器人，再打开标零位复查。"
                                       + (f" {reason}。" if reason else ""))
                return
            robot, stream = context.get("robot_id"), context.get("stream_id")

            def save_report(report):
                if (not robot or not stream or report.get("robot_id") != robot
                        or report.get("stream_id") != stream):
                    raise ValueError("标零位复查记录与打开时的机器人或采集会话不一致")
                return factory.save_zero_reference_check(sn, report, robot)

            self.factory_guidance = {
                "sn": sn, "item": item, "case_code": "SYS-006",
                "hint": "标零位复查读取编码器角度，报告保存在本轮SN。对侧行程检查使用独立入口。",
            }
            self.navigate(8)
            panel.open_zero_reference_check(sn, save_report)
            return
        routes = {
            "board": (6, self.operations_panel.board_panel.refresh, "新运行的单项自检将自动保存到发起时选定的SN；已经做完的自检可点击“将当前自检结果保存到此SN”，无需重测。"),
            "calibration": (8, self.joint_inspection_panel.calibrate, "点击“标定（写入零位）”，按原流程操作；结束后返回SYS-005确认流程状态。"),
            "calibration_check": (8, self.joint_inspection_panel.motor_control, "打开“标定检查”，在原窗口逐项检查对侧行程。"),
            "postzero": (8, self.joint_inspection_panel.monitor_source, "选择角度数据来源即可查看或采样；读数不代替标定检查结果。"),
        }
        if target not in routes:
            return
        index, control, hint = routes[target]
        code = {"board": "SYS-001", "calibration": "SYS-005", "calibration_check": "SYS-006", "postzero": "SYS-006"}[target]
        self.factory_guidance = {"sn": self.factory_panel.sn, "item": item, "case_code": code, "hint": hint}
        self.navigate(index)
        if index == 8:
            self.joint_inspection_panel.maintenance_toggle.setChecked(target != "postzero")
            scroll = self.joint_inspection_panel.findChild(QScrollArea)
            QTimer.singleShot(0, control, lambda: scroll.ensureWidgetVisible(control, 20, 30))
        control.setFocus()

    def update_factory_guidance(self):
        guidance = self.factory_guidance
        self.factory_return_bar.setVisible(bool(guidance) and self.pages.currentIndex() != 9)
        if not guidance:
            return
        item, sn = guidance["item"], guidance["sn"]
        case = guidance.get("case_code") or f"第{item}项"
        frame = self.last_frame or {}
        robot = frame.get("robot_id") if self.active_source_url else None
        connected = bool(robot) and frame.get("upload", {}).get("collector_connected") is not False
        identity = getattr(self.factory_panel, "identity", None)
        mismatch = bool(robot and identity and (robot, frame.get("source_kind")) != (identity["robot_id"], identity["source_kind"]))
        _set_text(self.factory_return_label, f"工厂SN：{sn or '未选择'} · {case} · 当前工具主连接：{robot or '未连接'}")
        connection = "请先到“设备设置”连接本SN对应机器人；工厂独立SSH仅用于动作日志。" if not connected else "请核对工具中的机器人身份与本SN对应；已完成与是否通过分别保存。"
        if mismatch:
            connection = f"当前工具主连接与动作日志连接 {identity['robot_id']} / {identity['source_kind']} 不一致，请先核对机器人。"
        _set_text(self.factory_tool_hint, guidance["hint"] + "\n" + connection)
        _set_style(self.factory_tool_hint, "color:#9a4f11;" if mismatch or not connected else "")
        self.factory_return_button.setText(f"返回整机测试 · 查看{case}结果")

    def factory_sn_changed(self, sn):
        board = self.operations_panel.board_panel
        board.factory_sn.setText(sn)
        board.factory_save_note.setText(f"当前SN：{sn}。新发起的检查完成后自动保存；现有历史快照需点击上方按钮关联。")
        if self.factory_guidance and not self.factory_guidance["sn"]:
            self.factory_guidance["sn"] = sn
        self.refresh_motor_maintenance()
        self.update_factory_guidance()

    def connect_factory_manual_dialog(self, dialog):
        target = {"sn": None, "robot_id": None}
        guidance = self.factory_guidance or {}
        if guidance.get("item") == 3 and guidance.get("sn") and guidance["sn"] == self.factory_panel.sn:
            dialog.angle_sn = guidance["sn"]
        def started(context):
            guidance = self.factory_guidance or {}
            sn = guidance.get("sn") if guidance.get("item") == 3 else None
            target.update(sn=sn if sn and sn == self.factory_panel.sn and context.get("source_kind") == "ros2" else None,
                          robot_id=context.get("robot_id"))
            if context.get("kind") == "angle_observations":
                dialog.angle_sn = target["sn"] or ""
        def saved(result):
            sn = target["sn"]
            if result.get("kind") == "angle_observations":
                def finished(_ok, message):
                    if isValid(dialog):
                        dialog.angle_report_finished(result["run_id"], message)
                if not sn or result["sn"] != sn or result["robot_id"] != target["robot_id"]:
                    finished(False, "本轮角度检查的 SN 或机器人绑定未确认。可导出本机 JSON。")
                    return
                self.factory_panel.sync_angle_calibration_check(sn, result, target["robot_id"], finished)
                return
            if not sn or result.get("robot_id") != target["robot_id"]:
                dialog.save_status.setText(f"本轮已保存在电脑：{result['path']}。未关联SN；请在工厂第3项选择SN后导入此JSON。")
                return
            def done(ok, message):
                dialog.save_status.setText(message if ok else message + " 文件：" + result["path"])
            self.factory_panel.sync_calibration_check(sn, result["path"], target["robot_id"], done)
        dialog.check_started.connect(started)
        dialog.check_saved.connect(saved)

    def factory_board_run_requested(self, request):
        panel, board = self.factory_panel, self.operations_panel.board_panel
        self.factory_board_runs.clear()
        sn = board.factory_sn.text().strip()
        if not sn:
            board.factory_save_note.setText("尚未选择SN：检查结果会保留在自检页，完成后输入SN并点击保存即可，无需重测。")
            return
        try:
            sn = panel.store.validate_sn(sn)
        except (AttributeError, ValueError) as exc:
            board.factory_save_note.setText(f"SN未能关联：{exc}。本次结果保留，完成后可修正SN并保存，无需重测。")
            return
        # This visible field is authoritative for this run, including when a
        # file import prevents switching the factory page immediately.
        if panel.sn != sn and not panel.busy:
            panel.activate_sn(sn)
        self.factory_board_runs[request["token"]] = {"sn": sn, "robot_id": request["robot_id"], "source_kind": request["source_kind"], "profile": board.factory_profile}
        board.factory_save_note.setText(f"本次检查归属SN：{sn} · {request['robot_id']}；等待完成后保存到电脑。")

    def factory_board_report_ready(self, result):
        target = self.factory_board_runs.pop(result["token"], None)
        if not target:
            return
        board = self.operations_panel.board_panel
        if (target["robot_id"], target["source_kind"]) != (result["robot_id"], result["source_kind"]):
            board.factory_save_note.setText("检查来源与发起时身份不一致，未自动保存；当前原始结果保留，请核对后手动关联SN。")
            return
        self.factory_panel.sync_board_snapshot(target["sn"], result["robot_id"], result["source_kind"], result["reports"], target["profile"])
        board.factory_save_note.setText(f"正在将本次检查保存到SN：{target['sn']}…")

    def sync_existing_board_results(self):
        panel, board = self.factory_panel, self.operations_panel.board_panel
        if not hasattr(panel, "store") or panel.closing:
            board.factory_save_note.setText("工厂本地目录不可用，当前自检结果保留。")
            return
        if panel.busy:
            board.factory_save_note.setText("本机正在读写日志，请完成后再关联现有自检。当前结果保留，无需重测。")
            return
        sn = board.factory_sn.text().strip()
        if not sn:
            board.factory_save_note.setText("请先在上方输入整机SN，再保存已经完成的自检。")
            board.factory_sn.setFocus()
            return
        if not board.reports:
            board.factory_save_note.setText("当前没有可保存的自检快照。请先读取状态 / 历史报告；不会自动运行检查。")
            return
        try:
            panel.store.results(sn)  # validate before changing the current SN
            if panel.sn != sn:
                panel.activate_sn(sn)
        except (ValueError, OSError) as exc:
            board.factory_save_note.setText(f"未保存：{exc}。当前自检结果保留。")
            return
        source = next(iter(board.reports.values()))["source_kind"]
        panel.sync_board_snapshot(sn, board.robot_id, source, board.reports)
        board.factory_save_note.setText(f"正在将当前 {len(board.reports)} 项状态快照保存到SN：{sn}；保留原始时间和结论…")

    def factory_board_saved(self, sn, snapshot):
        board = self.operations_panel.board_panel
        if snapshot.get("error"):
            board.factory_save_note.setText(f"SN：{sn} 未保存成功：{snapshot['error']}。当前快照保留，可核对后点击保存重试。")
            return
        counts = snapshot.get("display_counts") or (snapshot.get("assessment") or {}).get("counts", {})
        board.factory_save_note.setText(f"已保存到电脑 · SN：{sn} · {snapshot['robot_id']} · 已执行 {snapshot['executed']}/{snapshot['total']}，通过 {counts.get('通过', 0)}，不通过 {counts.get('不通过', 0)}，未完成 {counts.get('未完成', 0)}，未检查 {counts.get('未检查', 0)}。返回工厂即可查看；拔线或重开上位机仍保留。")

    def return_factory_result(self):
        if not self.factory_guidance:
            return
        guidance = self.factory_guidance
        if self.factory_panel.busy or self.factory_panel.closing:
            self.factory_panel.notice.setText("正在完成本机日志读写，请等待当前任务结束后再填写本项结果；当前SN和已有文件保持。")
            self.factory_tool_hint.setText(self.factory_panel.notice.text())
            return
        self.navigate(9)
        if self.factory_panel.sn != guidance["sn"]:
            self.factory_panel.notice.setText("工厂SN已变化，请核对本次工具输出属于哪台机器人，再选择对应测试项保存；未自动关联或保存。")
            return
        if guidance.get("case_code"):
            self.factory_panel.show_case_result(guidance["case_code"])
        else:
            self.factory_panel.show_item_result(guidance["item"])

    def open_factory_hardware_notes(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser

        if self.hardware_notes_dialog:
            self.hardware_notes_dialog.raise_()
            self.hardware_notes_dialog.activateWindow()
            return
        dialog = QDialog(self)
        self.hardware_notes_dialog = dialog
        dialog.setObjectName("hardwareNotesDialog")
        dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        palette = dialog.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#eef2fa"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#142441"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#142441"))
        dialog.setPalette(palette)
        dialog.setAutoFillBackground(True)
        dialog.setStyleSheet("""
            QDialog#hardwareNotesDialog { background:#eef2fa; color:#142441; }
            QDialog#hardwareNotesDialog QLabel { background:transparent; color:#142441; }
            QDialog#hardwareNotesDialog QTextBrowser { background:#ffffff; color:#142441;
                border:1px solid #bdcce4; border-radius:6px; padding:12px; }
        """)
        dialog.setWindowTitle("第1项 · 灯板与电机参数检查说明")
        dialog.resize(860, 620)
        body = QVBoxLayout(dialog)
        body.addWidget(label("此处只查看原流程说明，不运行脚本。灯板脚本会实际亮灯，六参数检查只读查询；参数修正仍使用原工程工具。"))
        view = QTextBrowser()
        view.setOpenLinks(False)
        view.document().setDefaultStyleSheet("body { color:#142441; } pre, code { color:#294f86; } th, td { border:1px solid #bdcce4; padding:6px; }")
        path = Path(__file__).resolve().parents[2] / "docs/HARDWARE_CHECK.md"
        try:
            view.setMarkdown(path.read_text(encoding="utf-8"))
        except OSError:
            view.setPlainText("原入口：scripts/check_rp1_hardware.sh。请在机器人原部署目录按对应机型使用原流程，完成后保存终端输出，回到本SN添加为第1项附件。当前安装未包含完整HARDWARE_CHECK.md；此页不会自动执行或修正电机参数。")
        body.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        body.addWidget(buttons)
        dialog.finished.connect(lambda: setattr(self, "hardware_notes_dialog", None))
        dialog.open()

    def clear_preview(self):
        super().clear_preview()
        if self.shell_ready:
            self.update_factory_guidance()
            self.refresh_motor_maintenance()

    def check_stale(self):
        super().check_stale()
        self.refresh_motor_maintenance()

    def defer_motor_maintenance_close(self, event):
        if not self.motor_maintenance_panel.busy:
            return False
        self.connection_label.setText("正在完成电机维护，请等待结果后关闭。")
        event.ignore()
        return True

    def closeEvent(self, event):
        if not self.defer_motor_maintenance_close(event):
            super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.shell_ready:
            self.render_temperature_curves()

    def render_temperature_curves(self):
        if not self._temperature_curves_dirty or not self.health_plot.isVisible():
            return
        for buffer, curve in zip(self.temp_buffers, self.temp_curves):
            if buffer:
                data = np.asarray(buffer)
                curve.setData(data[:, 0] - buffer[-1][0], data[:, 1], connect="finite")
            else:
                curve.clear()
        self.health_plot.setXRange(-60, 0, padding=0)
        self._temperature_curves_dirty = False

    def open_settings(self):
        self.navigate(4)
        self.url.setFocus()
        self.url.selectAll()

    def on_frame(self, frame):
        if (
            self.last_frame
            and self.last_frame.get("robot_id") != frame.get("robot_id")
            and self.thermal_dialog
        ):
            self.thermal_dialog.close()
        if self.last_frame and self.last_frame["stream_id"] != frame["stream_id"]:
            self.temp_buffers = [deque(maxlen=600), deque(maxlen=600)]
        super().on_frame(frame)
        self.refresh_motor_maintenance()
        if self.factory_guidance:
            self.update_factory_guidance()

    def open_temperature_region(self, region):
        members = [
            row for row in self._current_motor_map.get("rows", []) if row["region"] == region
        ]
        if members:
            self.open_joint_temperature(
                max(
                    members,
                    key=lambda row: row["value"] if row["value"] is not None else float("-inf"),
                )["joint"]
            )

    def open_joint_temperature(self, joint):
        from .desktop_thermal import TemperatureDialog

        if not self.last_frame or type(joint) is not int or not 0 <= joint <= 63:
            return
        if self.thermal_dialog is None:
            self.thermal_dialog = TemperatureDialog(
                self,
                lambda: self.operations_panel.server.text(),
                lambda: self.operations_panel.token.text(),
                lambda: (self.last_frame or {}).get("robot_id"),
            )
            self.thermal_dialog.diagnose.connect(self.diagnose_temperature)
        self.thermal_dialog.open_joint(
            self.last_frame["robot_id"], self._current_motor_map.get("rows", []), joint
        )

    def activate_motor_temperature(self, row, column):
        item = self.motor_table.item(row, 0)
        if item:
            self.open_joint_temperature(item.data(Qt.ItemDataRole.UserRole))

    def diagnose_temperature(self, joint, minutes):
        self.navigate(5)
        self.operations_panel.focus_temperature(joint, minutes)

    def render_host_cores(self, host, *, available):
        super().render_host_cores(host, available=available)
        if not self.shell_ready:
            return
        core_lines = []
        for group, name in (("big", "大核"), ("little", "小核"), ("unknown", "分组未知")):
            items = [
                f"CPU{core['index']} {'未知' if core['usage_percent'] is None else str(round(core['usage_percent'], 1)) + '%'}"
                for core in self._host_cpu_rows
                if core.get("group", "unknown") == group
            ]
            if items:
                core_lines.append(f"{name}：" + " · ".join(items))
        _set_text(self.host_core_summary,
            "\n".join(core_lines) if core_lines else "每核使用率 · 采集端尚未提供"
        )

    def render_health(self, frame, stale=False):
        super().render_health(frame, stale)
        if not self.shell_ready:
            return
        payload = frame or self.last_frame or {}
        health = payload.get("health") if frame and not stale else None
        if not health:
            health = build_health(
                payload.get("values", {}),
                payload.get("ages_ms", {}),
                payload.get("health_sources", {}),
                payload.get("health_sensors", {}),
                payload.get("joint_names", []),
                available=bool(frame) and not stale,
            )

        def number(value, unit=""):
            return "—" if value is None else f"{value:.1f}{unit}"

        battery, host, motors = (health[key] for key in ("battery", "host", "motors"))
        processes = health.get("processes") if frame and not stale else None
        processes = processes or build_process_view(
            payload.get("processes", []),
            payload.get("values", {}),
            payload.get("ages_ms", {}),
            available=bool(frame) and not stale,
        )
        items = processes["items"]
        running = sum(item["state"] == "running" for item in items)
        _set_text(self.process_summary,
            f"核心进程 · 存活 {running}/{len(items)}" if items else "核心进程 · 未配置名单"
        )
        _set_text(self.process_note,
            "CPU单核100%；RSS为近似值含共享页；重启数仅覆盖本采集会话。"
            if items
            else "未配置进程名单；请在机器人档案 processes 中声明实际 comm。"
        )
        if self.process_table.rowCount() != len(items):
            self.process_table.setRowCount(len(items))
        height = min(170, max(110, 42 + len(items) * 31))
        if self.process_table.height() != height:
            self.process_table.setFixedHeight(height)
        for row, item in enumerate(items):
            texts = [
                item["label"],
                item["label_state"]
                + (f" · {item['kernel_state']}" if item.get("kernel_state") else ""),
                number(item["cpu_percent"], "%"),
                number(item["rss_mib"]),
                "—" if item["threads"] is None else str(int(item["threads"])),
                "—" if item["observed_restarts"] is None else str(int(item["observed_restarts"])),
                number(item["age_ms"] / 1000 if item.get("age_ms") is not None else None),
            ]
            for column, text in enumerate(texts):
                cell = self.process_table.item(row, column)
                if cell is None:
                    cell = QTableWidgetItem()
                    self.process_table.setItem(row, column, cell)
                _set_text(cell, text)
                tooltip = f"{item['detail']}\ncomm={item['comm']} · PID={item.get('pid') or '—'}\n观测时间 {item.get('observed_at') or '未知'}"
                if cell.toolTip() != tooltip:
                    cell.setToolTip(tooltip)
                if column == 1:
                    color = QColor("#14735b" if item["state"] == "running" else "#925709")
                    if cell.foreground().color() != color:
                        cell.setForeground(color)
        _set_text(self.hero_id, payload.get("robot_id", "RP1"))
        _set_text(self.device_id, payload.get("robot_id", "尚未连接机器人"))
        self.battery_bar.setVisible(battery["soc"] is not None)
        self.battery_bar.setValue(round((battery["soc"] or 0) * 10))
        _set_text(self.battery_summary,
            f"{number(battery['voltage'], ' V')}   ·   {number(battery['current'], ' A')}\n电池温度  {number(battery['temperature'], '°C')}"
        )
        _set_text(self.temperature_value, number(motors["max_temperature"], "°C"))
        _set_text(self.host_summary,
            f"CPU {number(host['cpu_usage'], '%')}   ·   内存 {number(host['memory_usage'], '%')}"
        )
        _set_text(self.motor_summary,
            f"非零故障上报 {len(motors['faults'])} 项"
            if motors["faults"]
            else "已接入反馈未报故障"
            if motors["available"]
            else "故障数据未知"
        )
        from .monitor_config import monitoring_runtime

        central_url = payload.get("upload", {}).get("central_url")
        if central_url and self.operations_panel.server.text() == self._automatic_central_url:
            self.operations_panel.server.setText(central_url)
            self._automatic_central_url = central_url
        configuration = payload.get("monitoring_configuration")
        mapping, policy = monitoring_runtime(
            configuration,
            payload.get("robot_id"),
            payload.get("source_kind"),
            self.motor_map_config,
            self.thermal_policy,
        )
        thermal_map = build_motor_map(
            payload.get("robot_id"),
            payload.get("source_kind"),
            health,
            mapping,
            policy.temperature_limits_c,
        )
        self._current_motor_map = thermal_map
        attach_motor_map(health, thermal_map)
        if thermal_map["dof"]:
            self.motor_coverage.setText(f"{motors['available']} / {motors['total']}")
            existing = {
                self.motor_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                for row in range(self.motor_table.rowCount())
            }
            for item in thermal_map["rows"]:
                if item["joint"] in existing:
                    continue
                row = self.motor_table.rowCount()
                self.motor_table.insertRow(row)
                for column, text in enumerate((item["label"], "—", "—", "—")):
                    cell = QTableWidgetItem(text)
                    if column == 0:
                        cell.setData(Qt.ItemDataRole.UserRole, item["joint"])
                    self.motor_table.setItem(row, column, cell)
        self.health_overall.setText(
            "预览已过期 · 健康状态未知" if stale else health["overall"]["label"]
        )
        if thermal_map["hot_count"]:
            _set_style(self.health_overall, "color:#ae283a;")
            self.health_status["motors"].setText(motors["label"])
            _set_style(self.health_status["motors"], "color:#ae283a;")
        elif thermal_map["dof"] and motors["state"] == "incomplete":
            self.health_status["motors"].setText(motors["label"])
            _set_style(self.health_status["motors"], "color:#7d5206;")
        self.robot_overview.set_heat_map(thermal_map)
        _set_text(self.motor_map_status,
            thermal_map["summary"]
            + "\n"
            + thermal_map["mapping_note"]
            + "\n"
            + thermal_map["mapping_coverage"]
            + (
                f"\n中央配置版本 {configuration['revision']}"
                if configuration and configuration.get("source") == "database"
                else "\n本机配置 / 待同步中央版本"
            )
            + (
                "\n" + payload["upload"]["configuration_error"]
                if payload.get("upload", {}).get("configuration_error")
                else ""
            )
        )
        _set_style(self.motor_map_status,
            "color:#ae283a;" if thermal_map["hot_count"] else "color:#576c88;"
        )
        self.motor_map_status.setToolTip(thermal_map["meaning"])
        _set_text(self.motor_map_legend,
            f"浅色 {thermal_map['scale_min_c']:g} → 深红 {thermal_map['scale_max_c']:g} °C\n灰色：未知 · 描边及文字提示超温"
            if thermal_map["scale_min_c"] is not None
            else "色阶及部位映射尚未配置 · 灰色：未知"
        )
        row_lookup = {item["joint"]: item for item in thermal_map["rows"]}
        for row in range(self.motor_table.rowCount()):
            name_cell = self.motor_table.item(row, 0)
            joint = name_cell.data(Qt.ItemDataRole.UserRole)
            view = row_lookup.get(joint)
            for column in (1, 2):
                cell = self.motor_table.item(row, column)
                if cell:
                    reading = view["readings"][column - 1] if view else None
                    cell.setBackground(QColor(reading["color"] if reading else "#ffffff"))
                    cell.setForeground(QColor(reading["text_color"] if reading else "#142441"))
                    if view:
                        cell.setToolTip(view["label"] + " · " + view["detail"])
            if view:
                name_cell.setText(
                    f"{joint} · {view['label']}" + (" · 超温" if view["state"] == "hot" else "")
                )
        self.motor_note.setText(thermal_map["mapping_note"] + "。" + thermal_map["meaning"])
        summary = "预览过期，当前状态未知" if stale else health["overall"]["label"]
        detail = (
            "预览连接已中断，旧数值不能代表当前状态。" if stale else health["overall"]["detail"]
        )
        self.health_detail.setToolTip(detail)
        self.health_detail.setText(
            "预览已过期，当前健康状态未知。"
            if stale
            else "监测尚未完整接入，查看详情核对缺失数据。"
            if health["overall"]["state"] == "incomplete"
            else "存在超温、故障或保护上报，请查看详情。"
            if health["overall"]["state"] == "fault"
            else "健康数据观测中 · 温度与电压判据待配置。"
        )
        fault_lines = [
            f"{item['joint']} · 原始故障码 {int(item['code'])}" for item in motors["faults"]
        ]
        _set_text(self.current_alerts, "\n".join([summary, detail, *fault_lines]))
        signature = (
            payload.get("robot_id"),
            payload.get("stream_id"),
            summary,
            detail,
            tuple(fault_lines),
        )
        if (frame or stale) and signature != self.last_event:
            stamp = (
                datetime.now().astimezone().strftime("%H:%M:%S")
                if stale
                else (
                    datetime.fromisoformat(payload["sampled_at"]).astimezone().strftime("%H:%M:%S")
                    if payload.get("sampled_at")
                    else "—"
                )
            )
            self.session_events.appendleft(
                f"{stamp}  {payload.get('robot_id', 'RP1')} · {summary}"
                + (" · " + " / ".join(fault_lines) if fault_lines else "")
            )
            self.event_list.clear()
            self.event_list.addItems(list(self.session_events))
            self.event_list.setFixedHeight(min(240, max(70, 10 + len(self.session_events) * 30)))
            self.last_event = signature
        if not frame:
            self.temp_buffers = [deque(maxlen=600), deque(maxlen=600)]
            self._temperature_curves_dirty = True
        elif payload.get("sampled_at"):
            timestamp = datetime.fromisoformat(payload["sampled_at"]).timestamp()
            for buffer, value in zip(
                self.temp_buffers,
                (host["max_temperature"], motors["max_temperature"]),
            ):
                if buffer and timestamp <= buffer[-1][0]:
                    buffer.clear()
                if buffer and timestamp - buffer[-1][0] > 0.3:
                    buffer.append((buffer[-1][0] + 0.001, np.nan))
                buffer.append((timestamp, np.nan if value is None else value))
            self._temperature_curves_dirty = True
        self.render_temperature_curves()
