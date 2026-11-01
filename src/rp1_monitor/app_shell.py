"""App-style presentation of the existing read-only Qt monitor."""

from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QByteArray, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
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
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .desktop import STYLE, GridBackground, MonitorWindow
from .health_view import build_health
from .motor_map import MotorMapConfig, attach_motor_map, build_motor_map
from .process_monitor import build_process_view

APP_STYLE = (
    STYLE
    + """
QFrame#sidebar { background:#f7faff; border:1px solid #d4e2f5; border-radius:16px; }
QFrame#healthCard, QFrame#robotOverview { background:#ffffff; border:1px solid #d4e2f5; border-radius:16px; }
QPushButton#nav { background:transparent; color:#263e61; border:0; border-radius:9px; text-align:left; padding:12px 16px; font-size:15px; }
QPushButton#nav:hover { background:#edf3ff; }
QPushButton#nav:checked { background:#dae8ff; color:#234fd1; font-weight:700; }
QPushButton#nav:focus { border:2px solid #2b59e8; }
QPushButton#textLink { background:transparent; color:#2b59e8; border:0; text-align:left; padding:4px 0; font-size:13px; }
QPushButton#textLink:focus { border:1px solid #2b59e8; }
QLabel#healthValue { color:#142441; font-size:40px; font-weight:700; }
QLabel#healthOverall { font-size:14px; }
QLabel#source { font-size:12px; padding:7px 11px; }
QLabel#cardCaption { color:#576c88; font-size:12px; }
QLabel#heroId { color:#142441; font-size:23px; font-weight:700; }
QLabel#callout { background:#ffffff; border:1px solid #c2d7f6; border-radius:8px; color:#2853c6; font-size:13px; padding:8px; }
QProgressBar { background:#edf3ff; border:1px solid #d0e0fa; border-radius:5px; height:12px; }
QProgressBar::chunk { background:#3370ee; border-radius:4px; }
"""
)

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
    for checked, color in ((False, "#576c88"), (True, "#2b59e8")):
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
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
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
        self.setMinimumHeight(390)
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

    def __init__(self, url, motor_map_config=None, thermal_policy=None):
        from .central_policy import CentralPolicy

        self.motor_map_config = motor_map_config or MotorMapConfig()
        self.thermal_policy = thermal_policy or CentralPolicy()
        self.thermal_dialog = None
        self._automatic_central_url = "http://127.0.0.1:8890"
        self._current_motor_map = {}
        self.shell_ready = False
        super().__init__(url)
        self.setStyleSheet(APP_STYLE)
        old_root = self.takeCentralWidget()
        trend_page = self.tabs.widget(1)
        self.tabs.removeTab(1)
        self.tabs.removeTab(self.tabs.indexOf(self.joint_inspection_panel))
        self.joint_inspection_panel.setParent(None)
        self.session_events = deque(maxlen=100)
        self.last_event = None
        root = GridBackground()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(20)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(184)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(10, 22, 10, 18)
        side.setSpacing(8)
        brand = QLabel()
        logo = QPixmap(str(Path(__file__).parent / "web" / "brand" / "logo.png"))
        brand.setPixmap(
            logo.scaled(
                146,
                50,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        brand.setAccessibleName("ROBO PARTY")
        side.addWidget(brand, alignment=Qt.AlignmentFlag.AlignHCenter)
        side.addSpacing(26)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        titles = ("整机健康", "部件监测", "趋势分析", "告警记录", "设备设置", "诊断助手", "硬件自检", "监控记录", "角度与标定")
        for index, (title, glyph) in enumerate(
            zip(titles, ("home", "parts", "trend", "bell", "settings", "chat", "checks", "trend", "settings"))
        ):
            button = QPushButton(title)
            button.setObjectName("nav")
            button.setIcon(icon(glyph))
            button.setIconSize(QSize(22, 22))
            button.setCheckable(True)
            button.setMinimumHeight(48)
            self.nav.addButton(button, index)
            side.addWidget(button)
        side.addStretch()
        self.device_id = label("尚未连接机器人", "sectionTitle")
        side.addWidget(self.device_id)
        side.addWidget(label("只读监测 · 独立采集", "cardCaption"))
        layout.addWidget(sidebar)
        body = QVBoxLayout()
        body.setSpacing(14)
        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        self.app_title = label("RP1 整机健康", "title")
        title_box.addWidget(self.app_title)
        title_box.addWidget(self.health_overall)
        heading.addLayout(title_box, 1)
        switch = QPushButton("切换机器人")
        switch.setObjectName("secondary")
        switch.setMinimumHeight(40)
        switch.clicked.connect(self.open_settings)
        heading.addWidget(switch)
        heading.addSpacing(12)
        self.source_label.setFixedHeight(34)
        heading.addWidget(self.source_label)
        body.addLayout(heading)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.home_page())
        self.pages.addWidget(self.components_page())
        trend, trend_layout = panel("")
        trend_body = QHBoxLayout()
        self.curve_controls.setMinimumWidth(235)
        self.curve_controls.setMaximumWidth(285)
        self.curve_controls.show()
        trend_body.addWidget(self.curve_controls)
        trend_body.addWidget(trend_page, 1)
        trend_page.show()
        trend_layout.addLayout(trend_body)
        self.pages.addWidget(trend)
        self.pages.addWidget(self.alert_page())
        self.pages.addWidget(self.settings_page())
        self.pages.addWidget(self.operations_panel)
        self.pages.addWidget(self.operations_panel.board_panel)
        from .desktop_recordings import RecordingsPanel

        self.recordings_panel = RecordingsPanel(lambda: (self.station_url, self.station_token_file))
        self.pages.addWidget(self.recordings_panel)
        self.pages.addWidget(self.joint_inspection_panel)
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
        grid = QHBoxLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(14)
        hero, hero_layout = panel("")
        hero.setObjectName("robotOverview")
        hero.setMinimumWidth(260)
        hero.setMaximumWidth(360)
        self.hero_id = label("RP1", "heroId")
        hero_layout.addWidget(self.hero_id)
        hero_layout.addWidget(label("机器人概览"))
        self.robot_overview = RobotOverview()
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
        grid.addWidget(hero, 3)
        right = QVBoxLayout()
        right.setSpacing(12)
        cards = QGridLayout()
        cards.setSpacing(12)
        cards.setColumnStretch(0, 1)
        cards.setColumnStretch(1, 1)
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
        temperature_layout.addStretch()
        all_temps = QPushButton("查看全部温度 →")
        all_temps.setObjectName("textLink")
        all_temps.setMinimumHeight(36)
        all_temps.clicked.connect(lambda: self.navigate(1))
        temperature_layout.addWidget(all_temps)
        host, host_layout = panel("计算主机")
        host_layout.addWidget(self.host_temperature)
        self.host_summary = label()
        host_layout.addWidget(self.host_summary)
        self.process_summary = label("核心进程 · 等待观测", "cardCaption")
        host_layout.addWidget(self.process_summary)
        self.host_core_summary = label("每核使用率 · 采集端尚未提供", "cardCaption")
        host_layout.addWidget(self.host_core_summary)
        host_layout.addWidget(label("每核0–100% · 大小核按内核拓扑", "cardCaption"))
        host_layout.addStretch()
        host_layout.addWidget(self.health_status["host"])
        motors, motor_layout = panel("电机与通信")
        motor_layout.addWidget(self.motor_coverage)
        motor_layout.addWidget(label("温度与故障反馈覆盖"))
        self.motor_summary = label()
        motor_layout.addWidget(self.motor_summary)
        motor_layout.addWidget(self.health_status["motors"])
        for widget, row, column in (
            (battery, 0, 0),
            (temperature, 0, 1),
            (host, 1, 0),
            (motors, 1, 1),
        ):
            cards.addWidget(widget, row, column)
        right.addLayout(cards, 3)
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
        self.health_plot.setMinimumHeight(100)
        self.health_plot.setMaximumHeight(110)
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
        grid.addLayout(right, 7)
        scroll.setWidget(page)
        return scroll

    def components_page(self):
        page, layout = panel("部件温度与反馈")
        top = QHBoxLayout()
        battery, left = panel("电池与供电详情")
        left.addWidget(self.battery_metrics)
        left.addWidget(self.battery_age)
        left.addStretch()
        host, right = panel("主机热区与监测来源")
        right.addWidget(self.host_usage)
        right.addWidget(self.host_core_note)
        right.addWidget(self.host_core_table)
        self.host_sensors.setMaximumHeight(130)
        right.addWidget(self.host_sensors)
        right.addWidget(self.health_sources_label)
        top.addWidget(battery, 1)
        top.addWidget(host, 1)
        layout.addLayout(top)
        process_panel, process_layout = panel("核心进程 · 只读 / 每2秒采样")
        self.process_note = label("未配置进程名单", "cardCaption")
        process_layout.addWidget(self.process_note)
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
        process_layout.addWidget(self.process_table)
        layout.insertWidget(1, process_panel)
        layout.addWidget(self.motor_metrics)
        self.motor_table.setMaximumHeight(16777215)
        layout.addWidget(self.motor_table, 1)
        layout.addWidget(self.motor_note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page.setMinimumHeight(700)
        scroll.setWidget(page)
        return scroll

    def alert_page(self):
        page, layout = panel("当前告警与监测提示")
        self.current_alerts = label()
        layout.addWidget(self.current_alerts)
        layout.addWidget(label("本次打开期间的状态记录", "sectionTitle"))
        layout.addWidget(label("仅记录本窗口收到的状态变化，最多100条；关闭后不保存。"))
        self.event_list = QListWidget()
        layout.addWidget(self.event_list, 1)
        return page

    def settings_page(self):
        page, layout = panel("机器人连接")
        layout.addWidget(label("通过本机后台 relay 持续接收机器人数据；连接前只读核对服务身份。"))
        form = QFormLayout()
        form.setSpacing(16)
        for title, widget in (
            ("连接方式", self.connection_mode),
            ("上位机服务", self.url),
            ("后台机器人 / 本窗口预览", self.station_robots),
            ("只读刷新", self.station_refresh),
            ("后台连接操作", self.station_disconnect),
            ("SSH目标", self.ssh_target),
            ("SSH密码", self.ssh_password),
            ("主机指纹", self.ssh_fingerprint),
            ("身份确认", self.ssh_trust),
            ("SSH密钥", self.ssh_key),
            ("机器人采集端口", self.ssh_port),
        ):
            form.addRow(title, widget)
        layout.addLayout(form)
        self.connection_form = form
        layout.addWidget(self.connection_help)
        buttons = QHBoxLayout()
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        web = QPushButton("打开监控数据网页")
        self.quick_web_button = web
        def open_web():
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            if self.station_url:
                QDesktopServices.openUrl(QUrl(self.station_url + "/recordings"))
        web.clicked.connect(open_web)
        layout.addWidget(web)
        self.connection_summary = label(
            "上位机服务填写 relay 的根地址，不是中央网页地址。缺失、过期数据保持未知。"
        )
        layout.addWidget(self.connection_summary)
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
        self.pages.setCurrentIndex(index)
        self.nav.button(index).setChecked(True)
        self.app_title.setText("RP1 " + self.nav.button(index).text())

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
