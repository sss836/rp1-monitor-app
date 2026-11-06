"""Native platform entry around the existing local monitor and test workbench.

Only explicit platform actions cross the account/domain boundary. Device readers,
controls, recording configuration and source evidence retain their original owners.
"""

import json
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from threading import Event
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QDate,
    QDateTime,
    Qt,
    QThread,
    QTime,
    QTimer,
    QTimeZone,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .app_shell import APP_STYLE, AppMonitorWindow
from .desktop import GridBackground

_WORKERS = set()
_CONTEXT_KEYS = (
    "campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id",
    "test_case_version_id", "station_id",
)
_ROLES = {
    "SYSTEM_ADMIN": "系统管理员", "TEST_EXECUTOR": "测试执行员",
    "FACTORY_REVIEWER": "工厂审核员", "VIEWER": "查看者",
}
_APPROVAL = {"PENDING": "待审核", "APPROVED": "已批准", "REJECTED": "已驳回"}
_STATES = {
    "pending": "待处理", "queued": "待处理", "uploading": "上传中",
    "processing": "解析中", "failed": "待处理 · 失败可重试", "sent": "已送达",
    "validating": "平台解析 / 校验中",
    "submitted": "已送审", "complete": "已传输", "COMPLETE": "上传完成",
    "READY": "解析就绪", "WAITING": "等待上传", "QUEUED": "等待解析",
    "PROCESSING": "解析中", "FAILED": "解析失败", "CORRUPT": "校验失败",
}
PLATFORM_STYLE = APP_STYLE + """
QFrame#platformHeader { background:#ffffff; border:1px solid #d4e2f5; border-radius:9px; }
QFrame#platformLogin { background:#ffffff; border:1px solid #c8d8ef; border-radius:12px; }
QLabel#platformNotice { color:#7d5206; padding:8px; background:#fff3dd; border-radius:5px; }
QLabel#platformAccount { color:#405774; font-size:12px; }
QPushButton#domainNav { background:#ffffff; color:#263e61; border:1px solid #c8d8ef; }
QPushButton#domainNav:checked { background:#dae8ff; color:#234fd1; border-color:#9fb7ed; }
QPushButton#secondary:checked { background:#dae8ff; color:#234fd1; border-color:#9fb7ed; }
QPushButton#domainNav:disabled, QPushButton#secondary:disabled { background:#e4ebf5; color:#65778e; border-color:#c7d3e3; }
QPlainTextEdit { background:#ffffff; border:1px solid #a7bcd8; border-radius:5px; }

QFrame#workflowCard { background:#ffffff; border:1px solid #d7e3f3; border-radius:10px; }
QLabel#workflowTitle { font-size:23px; font-weight:700; color:#142441; }
QLabel#cardTitle { font-size:17px; font-weight:700; color:#142441; }
QLabel#cardHint { color:#63758e; font-size:12px; }
QLabel#fieldLabel { color:#4b607b; font-size:12px; }
QLabel#metadataValue { color:#142441; font-size:17px; font-weight:600; }
QLabel#selectedFile { color:#2853c6; font-size:14px; font-weight:600; }
QPushButton#workflowStep { background:transparent; color:#6e819b; border:0; padding:6px 10px; font-size:14px; }
QPushButton#workflowStep:checked { color:#2454d8; background:#e7efff; border-radius:6px; font-weight:700; }
QPushButton#disclosure { background:transparent; color:#57749f; border:0; padding:4px 0; font-weight:400; font-size:12px; }
QTabBar::tab { padding:7px 14px; font-size:13px; }
QTableWidget { font-size:12px; }
QHeaderView::section { padding:5px; font-size:12px; }
QDateTimeEdit { background:#ffffff; border:1px solid #b7c9e2; border-radius:5px; padding:6px; }
"""


def text(value="", name="subtitle"):
    widget = QLabel(str(value))
    widget.setObjectName(name)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def edit(name, placeholder="", password=False):
    widget = QLineEdit()
    widget.setAccessibleName(name)
    widget.setPlaceholderText(placeholder)
    if password:
        widget.setEchoMode(QLineEdit.EchoMode.Password)
    return widget


def combo(name):
    widget = QComboBox()
    widget.setAccessibleName(name)
    widget.setMinimumContentsLength(12)
    widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    return widget


def button(title, action, name="secondary"):
    widget = QPushButton(title)
    widget.setObjectName(name)
    widget.clicked.connect(action)
    return widget


def table(headers, name):
    widget = QTableWidget(0, len(headers))
    widget.setAccessibleName(name)
    widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.verticalHeader().hide()
    widget.setMinimumHeight(150)
    return widget


def rows_into(widget, rows):
    widget.setRowCount(len(rows))
    for index, values in enumerate(rows):
        for column, value in enumerate(values):
            widget.setItem(index, column, QTableWidgetItem(str(value if value is not None else "未记录")))


def platform_link(base, domain="RESEARCH", archive_id=None, curves=False):
    """A user-selected origin and business identifiers, never a native session."""
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("平台地址必须是可信 HTTPS 地址")
    if parsed.query or parsed.fragment:
        raise ValueError("平台地址不能包含查询参数或片段")
    parsed.port
    query = {"view": "home", "kind": "whole", "domain": domain}
    if archive_id:
        if domain == "FACTORY":
            query.update(view="factory", factory_archive=str(archive_id))
        else:
            query.update(view="curves" if curves else "archive", id=str(archive_id))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", urlencode(query), ""))


def platform_state_path(station_directory):
    """Keep platform state outside the station's marker-protected directory."""
    station = Path(station_directory or "runtime/station").expanduser()
    return station.with_name(station.name + "-platform")


class LazyPlatformService:
    """Platform persistence/client creation belongs to explicit background work."""

    def __init__(self, state_directory):
        self.state_directory = state_directory
        self._service = None
        self._cancelled = Event()
        self._closed = False

    def begin_request(self):
        if not self._closed:
            self._cancelled.clear()

    def _check_cancelled(self):
        if self._closed or self._cancelled.is_set() or QThread.currentThread().isInterruptionRequested():
            raise RuntimeError("平台任务已取消")

    def operation(self, action, payload):
        self._check_cancelled()
        if self._service is None:
            from .platform_workspace import NativePlatformService

            self._service = NativePlatformService(self.state_directory)
        try:
            self._check_cancelled()
        except RuntimeError:
            self._service.cancel_current()
            raise
        return self._service.operation(action, payload)

    def cancel_current(self):
        self._cancelled.set()
        if self._service is not None:
            self._service.cancel_current()

    def close(self):
        self._closed = True
        self.cancel_current()
        if self._service is not None:
            self._service.close()


class PlatformJob(QThread):
    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, operation, ticket):
        super().__init__()
        self.operation = operation
        self.ticket = ticket

    def run(self):
        try:
            self.succeeded.emit({"ticket": self.ticket, "value": self.operation()})
        except Exception as exc:
            self.failed.emit({"ticket": self.ticket, "error": exc})
        finally:
            # Password-containing request closures are released when the call ends.
            self.operation = None


class CurrentPageStack(QWidget):
    """A short factory form must not inherit the research form's full height."""

    currentChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self._pages = []
        self._index = 0
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def addWidget(self, widget):
        self._pages.append(widget)
        self._layout.addWidget(widget)
        widget.setVisible(len(self._pages) == 1)

    def setCurrentIndex(self, index):
        self._index = index
        for position, widget in enumerate(self._pages):
            widget.setVisible(position == index)
        self.updateGeometry()
        self.currentChanged.emit(index)

    def currentWidget(self):
        return self._pages[self._index]

    def setCurrentWidget(self, widget):
        self.setCurrentIndex(self._pages.index(widget))


def card(title, description=""):
    widget = QFrame()
    widget.setObjectName("workflowCard")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(8)
    if title:
        layout.addWidget(text(title, "cardTitle"))
    if description:
        layout.addWidget(text(description, "cardHint"))
    return widget, layout


def field(title, widget):
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    label = text(title, "fieldLabel")
    label.setBuddy(widget)
    layout.addWidget(label)
    layout.addWidget(widget)
    return box


class Disclosure(QWidget):
    def __init__(self, title):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = button("▸ " + title, lambda: None, "disclosure")
        self.toggle.setCheckable(True)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 5, 0, 0)
        layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)
        self.body.hide()
        self.toggle.toggled.connect(lambda checked: (self.body.setVisible(checked), self.toggle.setText(("▾ " if checked else "▸ ") + title)))


class WizardFlow(QWidget):
    step_requested = Signal(int)

    def __init__(self, labels, submit_button):
        super().__init__()
        self.index = 0
        self._pages = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)
        bar = QHBoxLayout()
        self.step_buttons = []
        for index, title in enumerate(labels):
            step = button(f"{index + 1:02}  {title}", lambda _checked=False, index=index: self.step_requested.emit(index), "workflowStep")
            step.setCheckable(True)
            self.step_buttons.append(step)
            bar.addWidget(step)
            if index < len(labels) - 1:
                bar.addWidget(text("—", "stepDivider"))
        bar.addStretch()
        layout.addLayout(bar)
        self.body = QScrollArea()
        self.body.setWidgetResizable(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self.body_layout = QVBoxLayout(content)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body.setWidget(content)
        layout.addWidget(self.body, 1)
        footer = QHBoxLayout()
        self.back = button("上一步", lambda: self.step_requested.emit(self.index - 1))
        self.next = button("下一步", lambda: self.step_requested.emit(self.index + 1), "primary")
        self.submit = submit_button
        footer.addWidget(self.back)
        self.hint = text("", "cardHint")
        footer.addWidget(self.hint, 1)
        footer.addWidget(self.next)
        footer.addWidget(self.submit)
        layout.addLayout(footer)

    def add_page(self, page):
        self._pages.append(page)
        self.body_layout.addWidget(page)
        page.hide()

    def set_index(self, index):
        self.index = max(0, min(2, index))
        for position, page in enumerate(self._pages):
            page.setVisible(position == self.index)
        for position, step in enumerate(self.step_buttons):
            step.setChecked(position == self.index)
        self.back.setVisible(self.index > 0)
        self.next.setVisible(self.index < 2)
        self.submit.setVisible(self.index == 2)
        self.updateGeometry()


class DateTimeInput(QWidget):
    def __init__(self, canonical, label):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.input = QDateTimeEdit()
        self.input.setAccessibleName(label)
        self.input.setTimeZone(QTimeZone(b"Asia/Shanghai"))
        self.input.setCalendarPopup(True)
        self.input.setDisplayFormat("yyyy-MM-dd HH:mm:ss.zzz")
        self.input.setMinimumDateTime(QDateTime(QDate(2000, 1, 1), QTime(0, 0), QTimeZone(b"Asia/Shanghai")))
        self.input.setSpecialValueText("未填写")
        self.input.setDateTime(self.input.minimumDateTime())
        self.input.dateTimeChanged.connect(lambda value: canonical.setText(datetime.fromtimestamp(value.toMSecsSinceEpoch() / 1000, timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()) if value > self.input.minimumDateTime() else canonical.clear())
        layout.addWidget(self.input, 1)
        layout.addWidget(button("今天", lambda: self.input.setDateTime(QDateTime.currentDateTime().toTimeZone(QTimeZone(b"Asia/Shanghai")))))


class PlatformMonitorWindow(AppMonitorWindow):
    first_authenticated = Signal()

    def __init__(self, url, *, platform_service=None, platform_state_directory=None, **kwargs):
        self.platform_ready = False
        self.platform_worker = None
        self._platform_closing = False
        self._platform_closed = False
        self._identity_epoch = 0
        self._source_epoch = 0
        self._ticket_serial = 0
        self._current_ticket = None
        self._result_callback = None
        self._cancel_requested = False
        self._local_initialization_started = False
        self._first_login_emitted = False
        self._platform_url = ""
        self.session = {"authenticated": False, "user": None}
        self.domain = "RESEARCH"
        self._archives = []
        self._recordings = []
        self._contexts = []
        self._catalog = []
        self._exports = None
        self._tasks = []
        self._factory_preview = None
        self._factory_projects = []
        self._factory_current_rows = []
        self._pending_factory_path = None
        self._deferred_platform_action = None
        self._detail_dialogs = set()
        super().__init__(url, **kwargs)
        if platform_service is None:
            state = platform_state_directory or Path.home() / ".local/share/rp1-monitor/platform"
            platform_service = LazyPlatformService(state)
        self.platform_service = platform_service
        self.setWindowTitle("RP1 整机测试工作台")
        self.setMinimumSize(1080, 720)
        self.resize(1440, 920)
        self.setStyleSheet(PLATFORM_STYLE)
        self.local_workbench_root = self.takeCentralWidget()
        self.local_workbench = QScrollArea()
        self.local_workbench.setWidgetResizable(True)
        self.local_workbench.setFrameShape(QFrame.Shape.NoFrame)
        self.local_workbench.setWidget(self.local_workbench_root)
        root = GridBackground()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(self._build_header())
        self.platform_notice = text("本地工作台可直接连接、测试与保存。需要查询或提交平台数据时，再在顶栏登录。", "platformNotice")
        self.platform_notice.setAccessibleName("平台操作状态")
        layout.addWidget(self.platform_notice)
        self.platform_stack = CurrentPageStack()
        self.login_page = self._build_login()
        self.account_dialog = QDialog(self)
        self.account_dialog.setWindowTitle("平台连接设置")
        self.account_dialog.setModal(False)
        self.account_dialog.resize(780, 620)
        settings_layout = QVBoxLayout(self.account_dialog)
        settings_layout.addWidget(self.login_page, 1)
        settings_close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        settings_close.rejected.connect(self.account_dialog.hide)
        settings_layout.addWidget(settings_close)
        self.platform_stack.addWidget(self.local_workbench)
        self.workspace = self._build_workspace()
        self.platform_stack.addWidget(self.workspace)
        self.offline_history = self._build_offline_history()
        self.platform_stack.addWidget(self.offline_history)
        layout.addWidget(self.platform_stack, 1)
        self.setCentralWidget(root)
        self.platform_ready = True
        self.platform_stack.setCurrentWidget(self.local_workbench)
        summary = getattr(self.factory_panel, "result_summary", None)
        if summary:
            summary.exported.connect(self._local_exported)
        if hasattr(self.factory_panel, "sn_changed"):
            self.factory_panel.sn_changed.connect(self._source_changed)
        for field in (self.factory_source, self.factory_robot):
            field.textChanged.connect(self._factory_identity_changed)
        self.factory_kind.currentIndexChanged.connect(self._factory_identity_changed)
        self.pg_file.textChanged.connect(self._research_source_changed)
        self.research_station.currentIndexChanged.connect(self._reset_research_confirmation)
        self.session_clock = QTimer(self)
        self.session_clock.setInterval(10_000)
        self.session_clock.timeout.connect(self._check_session_expiry)
        self.session_clock.start()
        self._update_platform_actions()

    def _build_header(self):
        frame = QFrame()
        frame.setObjectName("platformHeader")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(7)
        account = QHBoxLayout()
        account.setSpacing(8)
        account.addWidget(text("模式", "fieldLabel"))
        self.platform_mode = QComboBox()
        self.platform_mode.setAccessibleName("工作台模式")
        for label, value in (("本地工作台", "LOCAL"), ("研发平台", "RESEARCH"), ("工厂平台", "FACTORY")):
            self.platform_mode.addItem(label, value)
        self.platform_mode.setFixedWidth(128)
        self.platform_mode.currentIndexChanged.connect(self._mode_selected)
        account.addWidget(self.platform_mode)
        account.addWidget(text("账号", "fieldLabel"))
        self.username_edit = edit("平台用户名", "平台账号（可选）")
        self.username_edit.setFixedWidth(145)
        account.addWidget(self.username_edit)
        account.addWidget(text("密码", "fieldLabel"))
        self.password_edit = edit("平台密码", "平台密码", password=True)
        self.password_edit.setFixedWidth(145)
        self.password_edit.returnPressed.connect(self.login)
        account.addWidget(self.password_edit)
        self.login_button = button("登录", self.login, "primary")
        self.logout_button = button("退出", self.logout)
        account.addWidget(self.login_button)
        account.addWidget(self.logout_button)
        self.account_label = text("未登录（本地可用）", "platformAccount")
        self.account_label.setWordWrap(False)
        account.addWidget(self.account_label, 1)
        self.account_button = button("连接设置", self.open_account)
        account.addWidget(self.account_button)
        outer.addLayout(account)
        layout = QHBoxLayout()
        logo = QLabel()
        pixmap = QPixmap(str(Path(__file__).parent / "web/brand/logo.png"))
        if pixmap.isNull():
            logo.setText("ROBO PARTY")
        else:
            logo.setPixmap(pixmap.scaled(122, 42, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        logo.setAccessibleName("RoboParty")
        layout.addWidget(logo)
        layout.addWidget(text("RP1 整机测试工作台", "sectionTitle"))
        self.local_nav = button("本地工作台", self.open_local_workbench)
        self.local_nav.setCheckable(True)
        layout.addWidget(self.local_nav)
        self.research_nav = button("研发平台", lambda: self.select_domain("RESEARCH"), "domainNav")
        self.factory_nav = button("工厂平台", lambda: self.select_domain("FACTORY"), "domainNav")
        for item in (self.research_nav, self.factory_nav):
            item.setCheckable(True)
            layout.addWidget(item)
        layout.addStretch()
        self.screen_button = button("打开大屏", self.open_bigscreen)
        layout.addWidget(self.screen_button)
        outer.addLayout(layout)
        return frame

    def _mode_selected(self):
        value = self.platform_mode.currentData()
        if value == "LOCAL":
            self.open_local_workbench()
        else:
            self.select_domain(value)
        self._update_platform_actions()

    def _build_login(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.addStretch()
        box = QFrame()
        box.setObjectName("platformLogin")
        box.setMinimumWidth(680)
        box.setMaximumWidth(760)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setContentsMargins(30, 26, 30, 26)
        form.addRow(text("RP1 / RELIABILITY TEST", "eyebrow"))
        form.addRow(text("平台连接设置", "title"))
        introduction = text("与可靠性测试大屏使用同一平台账号。原生会话与浏览器会话各自登录，密码和会话 Cookie 不落盘。")
        introduction.setMinimumHeight(48)
        form.addRow(introduction)
        self.platform_url_edit = edit("平台地址", "https://平台地址:8443")
        self.platform_url_edit.setText(os.environ.get("RP1_PLATFORM_URL", ""))
        form.addRow("平台地址", self.platform_url_edit)
        ca_row = QHBoxLayout()
        self.ca_edit = edit("可信CA文件", "留空使用系统信任；内部证书请选择可信CA文件")
        self.ca_edit.setText(os.environ.get("RP1_PLATFORM_CA", ""))
        ca_row.addWidget(self.ca_edit, 1)
        self.ca_button = button("选择CA", self.choose_ca)
        ca_row.addWidget(self.ca_button)
        certificates = Disclosure("证书设置（可选）")
        certificates.body_layout.addLayout(ca_row)
        form.addRow(certificates)
        self.password_box = QWidget()
        passwords = QFormLayout(self.password_box)
        passwords.setContentsMargins(0, 0, 0, 0)
        passwords.addRow(text("首次登录须先修改密码；仅限制平台功能，本地工作台继续可用。"))
        self.current_password = edit("当前平台密码", password=True)
        self.new_password = edit("新平台密码", password=True)
        self.confirm_password = edit("确认新平台密码", password=True)
        passwords.addRow("当前密码", self.current_password)
        passwords.addRow("新密码", self.new_password)
        passwords.addRow("确认新密码", self.confirm_password)
        self.change_password_button = button("修改密码", self.change_password)
        passwords.addRow(self.change_password_button)
        self.password_box.hide()
        form.addRow(self.password_box)
        account_actions = QHBoxLayout()
        self.profile_button = button("核对会话", lambda: self.request("profile", {}, self._profile_loaded))
        account_actions.addWidget(self.profile_button)
        form.addRow(account_actions)
        self.offline_button = button("不登录，使用本地工作台", self.open_local_workbench)
        form.addRow(self.offline_button)
        footer = text("平台会话默认8小时，失效后须重新登录。退出或失效不会停止已开始的本地采集、记录或控制。")
        footer.setMinimumHeight(54)
        form.addRow(footer)
        outer.addWidget(box, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        scroll.setWidget(content)
        return scroll

    def _build_workspace(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        self.domain_heading = text("研发测试", "workflowTitle")
        self.domain_description = text("从已有记录建立可追溯的测试结果", "cardHint")
        heading.addWidget(self.domain_heading)
        heading.addSpacing(12)
        heading.addWidget(self.domain_description, 1)
        layout.addLayout(heading)
        self.workspace_tabs = QTabWidget()
        current = QWidget()
        current_layout = QVBoxLayout(current)
        current_layout.setContentsMargins(0, 0, 0, 0)
        self.domain_forms = CurrentPageStack()
        self.domain_forms.addWidget(self._build_research_form())
        self.domain_forms.addWidget(self._build_factory_form())
        current_layout.addWidget(self.domain_forms, 1)
        self.workspace_tabs.addTab(current, "本次测试")

        records, records_layout = card("平台测试记录", "在这里查询已提交的记录、审批状态和曲线。")
        filters = QHBoxLayout()
        self.result_search = edit("平台记录查询", "搜索SN或测试名称")
        self.result_status = combo("平台审批状态")
        for title, value in (("全部状态", ""), ("待审核", "PENDING"), ("已批准", "APPROVED"), ("已驳回", "REJECTED")):
            self.result_status.addItem(title, value)
        self.refresh_results_button = button("查询", self.refresh_results)
        self.more_results_button = button("下一页", self.more_results)
        filters.addWidget(self.result_search, 1)
        filters.addWidget(self.result_status)
        filters.addWidget(self.refresh_results_button)
        filters.addWidget(self.more_results_button)
        records_layout.addLayout(filters)
        self.results_table = table(("记录 / 文件", "测试对象", "处理进度", "审批状态", "提交人"), "分区平台测试记录")
        self.results_table.itemSelectionChanged.connect(self._update_platform_actions)
        self.results_table.itemDoubleClicked.connect(lambda _item: self.show_detail())
        records_layout.addWidget(self.results_table, 1)
        actions = QHBoxLayout()
        self.detail_button = button("查看详情", self.show_detail)
        self.browser_record_button = button("平台记录", self.open_record)
        self.browser_curve_button = button("查看曲线", lambda: self.open_record(curves=True))
        for item in (self.detail_button, self.browser_record_button, self.browser_curve_button):
            actions.addWidget(item)
        actions.addStretch()
        records_layout.addLayout(actions)
        self.workspace_tabs.addTab(records, "平台记录")

        tasks, tasks_layout = card("待传与已送达任务", "待传是传输状态，待审核是审批状态。二者分别保留。")
        self.tasks_table = table(("分区", "文件 / 证据", "传输状态", "平台记录", "说明"), "本机平台提交任务")
        tasks_layout.addWidget(self.tasks_table, 1)
        task_actions = QHBoxLayout()
        self.refresh_tasks_button = button("刷新", lambda: self.request("tasks", {}, self._tasks_loaded))
        self.retry_task_button = button("重试所选任务", self.retry_task)
        task_actions.addWidget(self.refresh_tasks_button)
        task_actions.addWidget(self.retry_task_button)
        task_actions.addStretch()
        tasks_layout.addLayout(task_actions)
        self.workspace_tabs.addTab(tasks, "待传任务")
        layout.addWidget(self.workspace_tabs, 1)
        footer = QHBoxLayout()
        footer.addWidget(text("本地工作持续运行；平台提交使用当前登录账号。", "cardHint"), 1)
        self.cancel_button = button("取消当前任务", self.cancel_platform_task)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)
        return page

    def _build_factory_form(self):
        self.factory_submit_button = button("确认提交", self.submit_factory, "primary")
        flow = WizardFlow(("选择机器人与结果", "核对测试项目", "确认提交"), self.factory_submit_button)
        self.factory_flow = flow
        flow.step_requested.connect(self._factory_step_requested)
        first = QWidget()
        first_layout = QHBoxLayout(first)
        first_layout.setContentsMargins(0, 0, 0, 0)
        first_layout.setSpacing(14)
        identity, identity_layout = card("当前机器人", "选择实际采集身份，不能用样品SN代替。")
        identity.setMaximumWidth(420)
        self.factory_robot_selector = combo("当前真实采集机器人")
        self.factory_robot_selector.currentIndexChanged.connect(self._factory_robot_selected)
        identity_layout.addWidget(self.factory_robot_selector)
        self.factory_robot_status = text("尚未选择采集身份", "cardHint")
        identity_layout.addWidget(self.factory_robot_status)
        identity_layout.addWidget(button("连接机器人", self.open_settings), alignment=Qt.AlignmentFlag.AlignLeft)
        self.factory_manual_mapping = QCheckBox("指定已核实的采集ID")
        self.factory_manual_mapping.toggled.connect(self._factory_mapping_mode_changed)
        identity_layout.addWidget(self.factory_manual_mapping)
        self.factory_mapping_box = QWidget()
        mapping = QVBoxLayout(self.factory_mapping_box)
        mapping.setContentsMargins(0, 0, 0, 0)
        self.factory_robot = edit("平台采集机器人ID", "填写采集程序上报的ID")
        self.factory_robot.setMaxLength(128)
        mapping.addWidget(self.factory_robot)
        self.factory_mapping_confirmed = QCheckBox("已核实此ID与文件中的样品对应")
        mapping.addWidget(self.factory_mapping_confirmed)
        self.factory_kind = combo("工厂数据来源")
        for title, value in (("选择实际数据来源", ""), ("真实采集", "real"), ("模拟数据", "simulated")):
            self.factory_kind.addItem(title, value)
        self.factory_mapping_box.hide()
        identity_layout.addWidget(self.factory_mapping_box)
        identity_layout.addWidget(field("本文件的数据来源", self.factory_kind))
        identity_layout.addWidget(text("文件未标明来源时，请核对后选择；当前连接不代替文件来源。", "cardHint"))
        identity_layout.addStretch()
        first_layout.addWidget(identity, 3)
        result, result_layout = card("本次测试结果", "选择已有CSV，或从原本地工作台保存并导出。文件信息自动读取。")
        choices = QHBoxLayout()
        self.factory_file_button = button("选择结果CSV", self.choose_factory_csv)
        choices.addWidget(self.factory_file_button)
        choices.addWidget(button("本地测试工作台", lambda: self.open_local_workbench(9)))
        choices.addStretch()
        result_layout.addLayout(choices)
        self.factory_path = edit("待提交工厂CSV")
        self.factory_path.setReadOnly(True)
        self.factory_path.setParent(self)
        self.factory_path.hide()
        self.factory_source = edit("平台上位机编号")
        self.factory_source.setReadOnly(True)
        self.factory_source.setParent(self)
        self.factory_source.hide()
        self.factory_file_label = text("尚未选择结果文件", "selectedFile")
        result_layout.addWidget(self.factory_file_label)
        metadata = QGridLayout()
        self.factory_meta = {}
        for index, (key, title) in enumerate((("sn", "样品SN"), ("model", "机型"), ("source", "上位机"), ("operators", "测试人员"))):
            value = text("—", "metadataValue")
            self.factory_meta[key] = value
            metadata.addWidget(field(title, value), index // 2, index % 2)
        result_layout.addLayout(metadata)
        self.factory_preview_note = text("读取后将显示SYS 12个目录项目及CSV原始结论；不会自动上传。", "cardHint")
        result_layout.addWidget(self.factory_preview_note)
        advanced = Disclosure("文件校验信息")
        self.factory_hash_label = text("尚无文件校验信息", "cardHint")
        advanced.body_layout.addWidget(self.factory_hash_label)
        result_layout.addWidget(advanced)
        result_layout.addStretch()
        first_layout.addWidget(result, 7)
        flow.add_page(first)

        second, second_layout = card("核对测试项目", "左侧为12个目录项目；选择项目后查看CSV中的父项、子项和原始结论。")
        split = QSplitter()
        self.factory_project_list = table(("编号", "目录项目", "父项结论"), "SYS12测试项目清单")
        self.factory_project_list.setAccessibleName("SYS12测试项目清单")
        self.factory_project_list.setMinimumWidth(355)
        self.factory_project_list.setMaximumWidth(410)
        self.factory_project_list.currentCellChanged.connect(lambda row, *_args: self._factory_project_selected(row))
        split.addWidget(self.factory_project_list)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 0, 0, 0)
        detail_layout.setSpacing(7)
        self.factory_project_title = text("选择测试项目", "cardTitle")
        self.factory_project_target = text("", "cardHint")
        detail_layout.addWidget(self.factory_project_title)
        detail_layout.addWidget(self.factory_project_target)
        self.factory_rows_table = table(("检查项", "CSV测试内容", "样品SN", "结论"), "当前项目父项与子项")
        self.factory_rows_table.setMinimumHeight(84)
        self.factory_rows_table.setMaximumHeight(128)
        self.factory_rows_table.currentCellChanged.connect(lambda row, *_args: self._factory_row_selected(row))
        detail_layout.addWidget(self.factory_rows_table)
        self.factory_row_caption = text("", "cardHint")
        self.factory_row_caption.setWordWrap(False)
        self.factory_row_caption.setMaximumHeight(22)
        self.factory_row_caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        detail_layout.addWidget(self.factory_row_caption)
        detail_tabs = QTabWidget()
        self.factory_result_text = QPlainTextEdit()
        self.factory_criterion_text = QPlainTextEdit()
        self.factory_notes_text = QPlainTextEdit()
        self.factory_record_text = QPlainTextEdit()
        for widget, title in ((self.factory_result_text, "实测结果"), (self.factory_criterion_text, "通过标准"), (self.factory_notes_text, "原始备注"), (self.factory_record_text, "记录信息")):
            widget.setReadOnly(True)
            widget.setMinimumHeight(58)
            detail_tabs.addTab(widget, title)
        evidence = QWidget()
        evidence_layout = QVBoxLayout(evidence)
        evidence_layout.addWidget(text("此17列汇总没有独立证据列。原始证据请在原本地工作台核对；选择CSV不代表证据已上传或核验。", "cardHint"))
        evidence_layout.addWidget(button("打开本地项目", self._open_factory_project), alignment=Qt.AlignmentFlag.AlignLeft)
        evidence_layout.addStretch()
        detail_tabs.addTab(evidence, "本地证据")
        detail_layout.addWidget(detail_tabs, 1)
        split.addWidget(detail)
        split.setSizes([320, 850])
        second_layout.addWidget(split, 1)
        flow.add_page(second)

        third, third_layout = card("确认本次提交", "确认的是这份文件与所选采集身份。批准之前保持待审，不会自动判定项目通过。")
        self.factory_confirmation_summary = text("尚未选择结果", "metadataValue")
        third_layout.addWidget(self.factory_confirmation_summary)
        self.factory_verified = QCheckBox("我已逐项核对测试对象、人员、实测结果、标准与本地证据")
        self.factory_completed = QCheckBox("确认本次测试已结束；未测试和不适用项目按原记录保留")
        third_layout.addWidget(self.factory_verified)
        third_layout.addWidget(self.factory_completed)
        self.factory_reason = edit("工厂提交说明", "简要说明本次提交依据")
        self.factory_reason.setMaxLength(300)
        self.factory_reason.setMaximumWidth(760)
        third_layout.addWidget(field("提交说明", self.factory_reason))
        third_layout.addWidget(text("提交人使用当前平台账号；CSV中的测试人员独立保留。此工厂汇总不赋予可靠性或MTBF统计资格。", "cardHint"))
        third_layout.addStretch()
        flow.add_page(third)
        flow.hint.setText("选择文件只做预览，不会自动上传。")
        flow.set_index(0)
        return flow

    def _build_research_form(self):
        self.research_submit_button = button("核实并送审", self.submit_research, "primary")
        flow = WizardFlow(("选择原始记录", "关联用例与计划", "核实并送审"), self.research_submit_button)
        self.research_flow = flow
        flow.step_requested.connect(self._research_step_requested)
        first, layout = card("选择本次测试的原始记录", "只读取已经结束的监控记录，保留原始采样与时间；结束不等于测试通过。")
        toolbar = QHBoxLayout()
        self.records_button = button("刷新记录", self.load_research_records)
        self.export_button = button("准备原始记录", self.export_research)
        toolbar.addWidget(self.records_button)
        toolbar.addWidget(self.export_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.research_record = combo("已结束PG监控记录")
        self.research_record.currentIndexChanged.connect(self._recording_selected)
        layout.addWidget(field("选择原监控记录", self.research_record))
        self.record_reference = text("尚未选择记录", "metadataValue")
        layout.addWidget(self.record_reference)
        self.export_label = text("选择记录后准备精确原始点，再继续关联测试用例。", "cardHint")
        layout.addWidget(self.export_label)
        self.pg_advanced = Disclosure("记录库连接设置")
        connection = QHBoxLayout()
        self.pg_file = edit("研发PG私有连接文件", "选择已有的私有连接配置文件")
        connection.addWidget(self.pg_file, 1)
        connection.addWidget(button("选择配置", self.choose_pg_file))
        self.pg_advanced.body_layout.addLayout(connection)
        self.pg_advanced.body_layout.addWidget(text("使用现有记录库的只读导出，不修改连接配置或原始数据。", "cardHint"))
        layout.addWidget(self.pg_advanced)
        evidence = Disclosure("原始文件与校验信息")
        self.export_evidence_label = text("尚未准备记录", "cardHint")
        evidence.body_layout.addWidget(self.export_evidence_label)
        layout.addWidget(evidence)
        layout.addStretch()
        flow.add_page(first)

        second, layout = card("关联实际用例、计划与样品", "仅使用平台已有的发布用例与执行上下文，不自动替换原版本。")
        row = QHBoxLayout()
        self.setup_button = button("刷新用例与计划", lambda: self.request("setup", {}, self._setup_loaded))
        row.addWidget(self.setup_button)
        row.addStretch()
        layout.addLayout(row)
        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.research_catalog = combo("已发布SYS测试用例")
        self.research_catalog.currentIndexChanged.connect(self._filter_contexts)
        self.research_campaign = combo("研发测试计划")
        self.research_campaign.currentIndexChanged.connect(self._campaign_selected)
        self.research_context = combo("研发真实执行上下文")
        self.research_context.currentIndexChanged.connect(self._context_selected)
        self.research_station = combo("研发实际工位")
        grid.addWidget(field("测试用例", self.research_catalog), 0, 0)
        grid.addWidget(field("测试计划", self.research_campaign), 0, 1)
        grid.addWidget(field("实际样品与执行上下文", self.research_context), 1, 0, 1, 2)
        grid.addWidget(field("实际工位（随上下文固定）", self.research_station), 2, 0)
        self.research_test_name = edit("研发归档测试名称", "归档时使用的测试名称")
        self.research_object = edit("研发实际测试对象", "实际测试的样品或对象")
        grid.addWidget(field("实际测试对象", self.research_object), 2, 1)
        grid.addWidget(field("测试名称", self.research_test_name), 3, 0, 1, 2)
        layout.addLayout(grid)
        details = Disclosure("执行身份与版本详情")
        self.context_label = text("选择后显示实际执行身份。", "cardHint")
        details.body_layout.addWidget(self.context_label)
        layout.addWidget(details)
        layout.addStretch()
        flow.add_page(second)

        third, layout = card("核实实际测试，再送审", "以下按真实测试填写，原监控起止仅供参考。不会默认填入时长、次数或通过结论。")
        grid = QGridLayout()
        self.research_tester = edit("研发实际测试人")
        self.research_tester.setMaxLength(100)
        self.research_verdict = combo("研发人工核实结论")
        self.research_verdict.addItem("选择核实结论", "")
        for value, title in (("passed", "通过"), ("failed", "不通过"), ("blocked", "阻塞 / 无法完成")):
            self.research_verdict.addItem(title, value)
        self.research_started = edit("人工申报测试开始时间")
        self.research_ended = edit("人工申报测试结束时间")
        self.research_seconds = edit("人工申报有效运行秒数")
        for widget in (self.research_started, self.research_ended, self.research_seconds):
            widget.setParent(self)
            widget.hide()
        self.research_start_picker = DateTimeInput(self.research_started, "选择测试开始时间（北京时间）")
        self.research_end_picker = DateTimeInput(self.research_ended, "选择测试结束时间（北京时间）")
        duration = QWidget()
        duration_layout = QHBoxLayout(duration)
        duration_layout.setContentsMargins(0, 0, 0, 0)
        self.duration_fields = []
        for unit, title in (("小时", "有效运行小时"), ("分钟", "有效运行分钟"), ("秒", "有效运行秒")):
            entry = edit(title, "未填")
            entry.setMaximumWidth(78)
            entry.textChanged.connect(self._duration_changed)
            self.duration_fields.append(entry)
            duration_layout.addWidget(entry)
            duration_layout.addWidget(text(unit, "fieldLabel"))
        duration_layout.addStretch()
        self.research_cycles = edit("人工申报完成循环数", "未填写")
        self.research_exceptions = edit("人工申报异常次数", "未填写")
        counts = QWidget()
        counts_layout = QHBoxLayout(counts)
        counts_layout.setContentsMargins(0, 0, 0, 0)
        counts_layout.addWidget(field("完成循环", self.research_cycles))
        counts_layout.addWidget(field("异常次数", self.research_exceptions))
        for title, widget, row, column in (("实际测试人", self.research_tester, 0, 0), ("核实结论", self.research_verdict, 0, 1), ("有效运行时长", duration, 0, 2), ("开始时间 · 北京时间", self.research_start_picker, 1, 0), ("结束时间 · 北京时间", self.research_end_picker, 1, 1)):
            grid.addWidget(field(title, widget), row, column)
        grid.addWidget(counts, 1, 2)
        layout.addLayout(grid)
        notes = QHBoxLayout()
        self.research_notes = QPlainTextEdit()
        self.research_notes.setAccessibleName("研发核实备注")
        self.research_notes.setMaximumHeight(62)
        self.research_notes.setPlaceholderText("说明实际测试与原监控证据的关系、异常和需要中央保留的依据（最多1000字）")
        self.research_reason = edit("研发本地送审任务说明", "本地任务说明，最多300字")
        self.research_reason.setMaxLength(300)
        notes.addWidget(field("核实备注", self.research_notes), 3)
        notes.addWidget(field("本地送审说明", self.research_reason), 2)
        layout.addLayout(notes)
        self.research_verified = QCheckBox("已核对原始证据、实际对象、执行身份和人工声明")
        self.research_completed = QCheckBox("确认实际测试已结束；送审不代表批准或统计生效")
        layout.addWidget(self.research_verified)
        layout.addWidget(self.research_completed)
        layout.addStretch()
        flow.add_page(third)
        flow.hint.setText("原记录保留；只有当前步骤的资料会显示。")
        flow.set_index(0)
        return flow

    def _build_offline_history(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(text("本地历史 · 只读", "title"))
        layout.addWidget(text("未登录平台；只查看电脑已有整机结果。不连接机器人、不导出或提交平台，也不改变现有采集与控制。"))
        self.offline_sn = combo("只读本地历史SN")
        self.offline_sn.currentIndexChanged.connect(self._offline_sn_selected)
        layout.addWidget(self.offline_sn)
        self.offline_table = table(("项目编号", "项目", "结果", "测试人员", "实际结果"), "只读本地结果")
        layout.addWidget(self.offline_table, 1)
        return page

    def _valid_session(self):
        user = self.session.get("user") or {}
        try:
            expiry = datetime.fromisoformat(str(self.session.get("expires_at") or "").replace("Z", "+00:00"))
            unexpired = expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)
        except ValueError:
            unexpired = False
        return bool(self.session.get("authenticated") and user.get("id") and user.get("kind") == "HUMAN" and not user.get("must_change_password") and unexpired)

    def _set_session(self, value):
        if not isinstance(value, dict):
            return
        old_user = self.session.get("user") or {}
        old_identity = (old_user.get("id"), old_user.get("role"), old_user.get("kind"))
        self.session = deepcopy(value)
        user = value.get("user") or {}
        if old_identity != (user.get("id"), user.get("role"), user.get("kind")):
            for dialog in list(self._detail_dialogs):
                dialog.reject()
            self._archives = []
            self._tasks = []
            self._exports = None
            self._clear_factory_preview()
            self._clear_research_declarations()
            self.record_reference.setText("尚未选择记录")
            self.export_evidence_label.setText("尚未准备记录")
            self.context_label.setText("选择后显示实际执行身份。")
            self.research_flow.set_index(0)
            self._research_context_key = None
            self._research_next_after_export = False
            self._contexts = []
            self._catalog = []
            self._recordings = []
            rows_into(self.results_table, [])
            rows_into(self.tasks_table, [])
            for field in (self.research_catalog, self.research_campaign, self.research_context,
                          self.research_station, self.research_record):
                self._fill_combo(field, [], str, "请重新读取当前账号可访问的数据")
            for field in (self.research_test_name, self.research_object, self.research_tester,
                          self.research_started, self.research_ended, self.research_seconds,
                          self.research_cycles, self.research_exceptions, self.research_reason):
                field.clear()
            self.research_notes.clear()
            self.research_verdict.setCurrentIndex(0)
            self.export_label.setText("请在当前账号下重新选择并导出研发原始证据。")
            self._reset_factory_confirmation()
            self._reset_research_confirmation()
        if user:
            name = user.get("display_name") or user.get("username") or "平台用户"
            role = _ROLES.get(user.get("role"), user.get("role") or "未知角色")
            try:
                expiry = datetime.fromisoformat(str(value.get("expires_at") or "").replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            except ValueError:
                expiry = "有效期未返回"
            suffix = " · 须先修改密码" if user.get("must_change_password") else ""
            self.account_label.setText(f"{name} · {role}{suffix}")
            self.account_label.setToolTip(f"平台会话有效期：{expiry}；本地工作不依赖平台登录。")
        else:
            self.account_label.setText("未登录（本地可用）")
            self.account_label.setToolTip("平台会话默认8小时；本地连接、测试、控制与保存独立可用。")
            for field in (self.current_password, self.new_password, self.confirm_password):
                field.clear()
            if self.platform_stack.currentWidget() is self.workspace:
                self.open_local_workbench()
        self.password_box.setVisible(bool(user.get("must_change_password")))
        if self._valid_session():
            summary = getattr(self.factory_panel, "result_summary", None)
            if summary and not summary.operator_edit.text().strip():
                summary.operator_edit.setText(user.get("display_name") or user.get("username") or "")
            if not self.research_tester.text().strip():
                self.research_tester.setText(user.get("display_name") or user.get("username") or "")
        self._update_platform_actions()

    def _update_platform_actions(self):
        if not self.platform_ready:
            return
        idle = self.platform_worker is None and not self._platform_closing
        valid = self._valid_session()
        user = self.session.get("user") or {}
        needs_password = bool(user.get("must_change_password"))
        for widget in (self.platform_url_edit, self.ca_edit, self.ca_button, self.username_edit, self.password_edit, self.login_button):
            widget.setEnabled(idle and not self.session.get("authenticated") and not needs_password)
        self.password_box.setEnabled(idle)
        self.profile_button.setEnabled(idle and bool(user))
        self.logout_button.setEnabled(idle and bool(user))
        self.offline_button.setEnabled(idle)
        self.offline_history.setEnabled(idle)
        self.account_button.setEnabled(idle)
        self.local_nav.setEnabled(not self._platform_closing)
        self.research_nav.setEnabled(idle and user.get("role") != "FACTORY_REVIEWER")
        self.factory_nav.setEnabled(idle)
        in_domain = self.platform_stack.currentWidget() is self.workspace
        self.research_nav.setChecked(self.domain == "RESEARCH" and valid and in_domain)
        self.factory_nav.setChecked(self.domain == "FACTORY" and valid and in_domain)
        self.local_nav.setChecked(self.platform_stack.currentWidget() is self.local_workbench)
        self.platform_mode.blockSignals(True)
        self.platform_mode.setCurrentIndex(self.platform_mode.findData(self.domain if in_domain else "LOCAL"))
        self.platform_mode.blockSignals(False)
        self.platform_mode.setEnabled(idle)
        self.screen_button.setEnabled(idle and valid and bool(self._platform_url))
        self.workspace.setEnabled(idle and valid)
        self.factory_submit_button.setEnabled(idle and valid and bool(self.session.get("can_submit_factory")))
        self.research_submit_button.setEnabled(idle and valid and bool(self.session.get("can_submit_research")))
        self.records_button.setEnabled(idle and valid and bool(self.session.get("can_submit_research")))
        self.export_button.setEnabled(idle and valid and bool(self.session.get("can_submit_research")))
        self.setup_button.setEnabled(idle and valid and bool(self.session.get("can_submit_research")))
        can_submit = bool(self.session.get("can_submit_factory" if self.domain == "FACTORY" else "can_submit_research"))
        self.workspace_tabs.setTabEnabled(0, valid and can_submit)
        if valid and not can_submit and self.workspace_tabs.currentIndex() == 0:
            self.workspace_tabs.setCurrentIndex(1)
        self.factory_kind.setEnabled(idle and valid and not bool((self._factory_preview or {}).get("simulated_marker")))
        # The cancel button must remain usable while the rest of the form is frozen.
        if not idle and not self._platform_closing:
            self.workspace.setEnabled(valid)
            for widget in (self.domain_forms, self.result_search, self.result_status, self.refresh_results_button,
                           self.more_results_button, self.results_table, self.detail_button,
                           self.browser_record_button, self.browser_curve_button,
                           self.refresh_tasks_button, self.retry_task_button, self.tasks_table):
                widget.setEnabled(False)
        else:
            for widget in (self.domain_forms, self.result_search, self.result_status, self.refresh_results_button,
                           self.results_table, self.refresh_tasks_button, self.tasks_table):
                widget.setEnabled(valid)
        selected = self._selected_archive()
        self.detail_button.setEnabled(idle and valid and selected is not None)
        self.browser_record_button.setEnabled(idle and valid and selected is not None)
        self.browser_curve_button.setEnabled(idle and valid and selected is not None and self.domain == "RESEARCH")
        self.browser_curve_button.setVisible(self.domain == "RESEARCH")
        self.more_results_button.setEnabled(idle and valid and getattr(self, "_next_cursor", None) is not None)
        self.retry_task_button.setEnabled(idle and valid and bool(self.session.get("can_submit_factory") or self.session.get("can_submit_research")))
        self.cancel_button.setEnabled(not idle and not self._cancel_requested and not self._platform_closing)

    def request(self, action, payload, callback=None, *, local_operation=None):
        if self.platform_worker is not None or self._platform_closing:
            self.platform_notice.setText("正在完成当前平台任务，请等待或请求取消。已冻结的身份与证据不会随页面改变。")
            return False
        public = {"configure", "login", "change_password", "logout", "profile"}
        self._request_action = action
        self._request_requires_session = local_operation is None and action not in public
        account = self.session.get("user") or {}
        self._request_identity_signature = (account.get("id"), account.get("role"), account.get("kind"))
        if local_operation is None and action not in public and not self._valid_session():
            self.platform_notice.setText("请先完成平台登录；本地已有工作继续运行。")
            return False
        self._ticket_serial += 1
        ticket = (self._identity_epoch, self._source_epoch, self.domain, self._ticket_serial)
        self._current_ticket = ticket
        self._result_callback = callback
        self._cancel_requested = False
        frozen = deepcopy(payload)
        if local_operation is None and action not in public:
            frozen.setdefault("session_generation", self.session.get("generation"))
        if hasattr(self.platform_service, "begin_request"):
            self.platform_service.begin_request()
        operation = local_operation or (lambda: self.platform_service.operation(action, frozen))
        worker = PlatformJob(operation, ticket)
        worker.setObjectName("RP1NativePlatformTask")
        self.platform_worker = worker
        _WORKERS.add(worker)
        worker.succeeded.connect(self._platform_succeeded)
        worker.failed.connect(self._platform_failed)
        worker.finished.connect(self._platform_finished)
        worker.finished.connect(lambda: _WORKERS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        self.platform_notice.setText("正在准备本地连接；无需平台登录，不会自动开始测试。" if action == "local_bootstrap" else "正在处理平台任务；本地采集、记录和控制独立运行。")
        self._update_platform_actions()
        worker.start()
        return True

    @Slot(object)
    def _platform_succeeded(self, result):
        ticket = result["ticket"]
        if self._platform_closing or (self._request_action != "local_bootstrap" and ticket[0] != self._identity_epoch):
            return
        value = result["value"]
        if isinstance(value, dict) and "session" in value:
            self._set_session(value["session"])
        account = self.session.get("user") or {}
        if getattr(self, "_request_requires_session", False) and (not self._valid_session() or self._request_identity_signature != (account.get("id"), account.get("role"), account.get("kind"))):
            self.platform_notice.setText("平台会话或权限已变化，请重新登录后读取当前账号可访问的数据。此前结果不会填回页面。")
            return
        if self._cancel_requested:
            self.platform_notice.setText("平台请求已结束。取消前已送达的提交仍可能存在，请刷新台账核对；不会自动再次提交。")
            return
        if getattr(self, "_request_action", "") not in {"configure", "login", "profile", "change_password", "logout", "local_bootstrap"} and ticket != (self._identity_epoch, self._source_epoch, self.domain, self._ticket_serial):
            self.platform_notice.setText("此前身份下的任务已结束；当前来源或分区已变化，请刷新对应台账核对。")
            return
        callback = self._result_callback
        if callback:
            try:
                callback(value)
            except (ValueError, TypeError, KeyError, AttributeError):
                self.platform_notice.setText("平台响应无法完整显示，请刷新台账核对。已提交任务不会自动重复创建。")
        else:
            self.platform_notice.setText("平台操作已完成。")

    @Slot(object)
    def _platform_failed(self, result):
        if self._platform_closing or (self._request_action != "local_bootstrap" and result["ticket"][0] != self._identity_epoch):
            return
        error = result["error"]
        session = getattr(error, "session", None)
        if isinstance(session, dict):
            self._set_session(session)
        message = getattr(error, "message", None)
        if not isinstance(message, str) or not message:
            message = "平台操作未确认成功，请核对配置或刷新台账后重试。"
        self.platform_notice.setText(message + " 本地已有记录保留；未自动更换提交身份或幂等任务。")

    @Slot()
    def _platform_finished(self):
        self.platform_worker = None
        self._result_callback = None
        self._current_ticket = None
        self._update_platform_actions()
        if getattr(self, "_emit_first_login_after_finish", False) and self._valid_session():
            self._emit_first_login_after_finish = False
            self.first_authenticated.emit()
        pending, self._pending_factory_path = self._pending_factory_path, None
        deferred, self._deferred_platform_action = self._deferred_platform_action, None
        if pending and self._valid_session() and not self._platform_closing:
            QTimer.singleShot(0, lambda: self._local_exported(pending))
        elif deferred and self._valid_session() and not self._platform_closing and not self._cancel_requested:
            QTimer.singleShot(0, deferred)

    def cancel_platform_task(self):
        if self.platform_worker is not None:
            self._cancel_requested = True
            self.platform_worker.requestInterruption()
            self.platform_service.cancel_current()
            self.platform_notice.setText("已请求取消，正在等待后台任务自然结束。已送达的提交须在台账核对，不能假定已撤回。")
            self._update_platform_actions()

    def login(self):
        if self.platform_worker is not None:
            return
        url = self.platform_url_edit.text().strip().rstrip("/")
        try:
            platform_link(url)
        except (ValueError, TypeError):
            self.platform_notice.setText("请输入不含账号、密码、查询参数的 HTTPS 平台地址，并配置可信CA；不提供跳过证书校验。")
            return
        username, password = self.username_edit.text().strip(), self.password_edit.text()
        self.password_edit.clear()
        if not username or not password:
            self.platform_notice.setText("请填写平台账号和密码。")
            return
        self._identity_epoch += 1
        ca = self.ca_edit.text().strip()
        self._platform_url = url

        def operation():
            self.platform_service.operation("configure", {"url": url, "ca_file": ca or None})
            return self.platform_service.operation("login", {"username": username, "password": password})

        self.request("login", {}, self._login_loaded, local_operation=operation)

    def _login_loaded(self, _value):
        user = self.session.get("user") or {}
        if user.get("must_change_password"):
            self.platform_notice.setText("账号已验证，请先修改初始密码。修改完成前不能访问或提交平台测试数据。")
            self.open_account()
            return
        if not self._valid_session():
            self.platform_notice.setText("平台未返回有效会话，请重新登录。")
            return
        self.platform_notice.setText("平台登录成功。可进入研发或工厂流程；本地采集与控制仍沿用原逻辑。")
        if user.get("role") == "FACTORY_REVIEWER":
            self.domain = "FACTORY"
        self.account_dialog.hide()
        if not self._first_login_emitted:
            self._first_login_emitted = True
            self._emit_first_login_after_finish = True

    def change_password(self):
        old, new, confirmation = self.current_password.text(), self.new_password.text(), self.confirm_password.text()
        for field in (self.current_password, self.new_password, self.confirm_password):
            field.clear()
        if not old or not new or new != confirmation:
            self.platform_notice.setText("请填写当前密码，并确保两次新密码一致。")
            return
        self.request("change_password", {"current_password": old, "new_password": new}, self._login_loaded)

    def _profile_loaded(self, _value):
        self.platform_notice.setText("已核对平台会话；原生与浏览器登录会话独立。")

    def logout(self):
        self.request("logout", {}, self._logout_loaded)

    def _logout_loaded(self, _value):
        self._identity_epoch += 1
        self._archives = []
        self._tasks = []
        rows_into(self.results_table, [])
        rows_into(self.tasks_table, [])
        self.platform_notice.setText("已退出平台账号。已开始的本地采集、记录和控制继续运行；平台操作须重新登录。")
        self.open_local_workbench()

    def _check_session_expiry(self):
        if not self.session.get("authenticated"):
            return
        try:
            expiry = datetime.fromisoformat(str(self.session.get("expires_at") or "").replace("Z", "+00:00"))
            expired = expiry.tzinfo is None or expiry <= datetime.now(timezone.utc)
        except ValueError:
            expired = True
        if expired:
            self._identity_epoch += 1
            if self.platform_worker is not None:
                self.cancel_platform_task()
            self._set_session({"authenticated": False, "user": None})
            self.platform_notice.setText("平台会话已到期，请重新登录。已开始的本地采集、记录和控制不受影响。")

    def open_account(self):
        self.account_dialog.show()
        self.account_dialog.raise_()
        self.account_dialog.activateWindow()
        self._update_platform_actions()

    def choose_ca(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择可信平台CA证书", "", "CA证书 (*.pem *.crt);;所有文件 (*)")
        if path:
            self.ca_edit.setText(path)

    def select_domain(self, domain):
        if not self._valid_session() or self.platform_worker is not None:
            self.platform_notice.setText("平台查询和提交需要有效账号，请在顶栏登录；本地工作台可继续使用。当前任务须先结束。")
            self._update_platform_actions()
            return
        if domain not in {"RESEARCH", "FACTORY"}:
            return
        if domain == "RESEARCH" and (self.session.get("user") or {}).get("role") == "FACTORY_REVIEWER":
            self.platform_notice.setText("当前工厂审核账号使用工厂分区，研发数据权限由平台另行确定。")
            return
        if self.domain != domain:
            self._source_epoch += 1
        for dialog in list(self._detail_dialogs):
            dialog.reject()
        self.domain = domain
        self._archives = []
        self._next_cursor = None
        rows_into(self.results_table, [])
        self.domain_heading.setText("研发测试" if domain == "RESEARCH" else "工厂测试")
        self.domain_description.setText("从已有记录建立可追溯的测试结果" if domain == "RESEARCH" else "逐项核对本次整机测试，再提交结果")
        self.domain_forms.setCurrentIndex(0 if domain == "RESEARCH" else 1)
        self.platform_stack.setCurrentWidget(self.workspace)
        permission = "can_submit_factory" if domain == "FACTORY" else "can_submit_research"
        self.workspace_tabs.setCurrentIndex(0 if self.session.get(permission) else 1)
        self._prefill_factory_identity()
        self._update_platform_actions()

    def open_local_workbench(self, index=None):
        self.account_dialog.hide()
        self.platform_stack.setCurrentWidget(self.local_workbench)
        # clicked(bool) is not a page index.
        if isinstance(index, int) and not isinstance(index, bool):
            self.navigate(index)
        self._update_platform_actions()

    def navigate(self, index):
        super().navigate(index)
        if getattr(self, "platform_ready", False):
            self.local_workbench_root.setProperty("plainBackground", index == 9)
            self.local_workbench_root.update()
            self.platform_stack.setCurrentWidget(self.local_workbench)
            self._update_platform_actions()

    def open_bigscreen(self):
        if self._valid_session():
            self._open_link(domain=self.domain)

    def _open_link(self, *, domain, archive_id=None, curves=False):
        try:
            url = platform_link(self._platform_url, domain, archive_id, curves)
        except (ValueError, TypeError):
            self.platform_notice.setText("平台地址无效，请在平台账号页核对HTTPS地址。")
            return
        QDesktopServices.openUrl(QUrl(url))
        self.platform_notice.setText("已请求系统浏览器打开所选平台。浏览器须独立登录；链接不包含原生会话凭据。")

    def _source_changed(self, *_args):
        self._source_epoch += 1
        self._reset_factory_confirmation()

    def _reset_factory_confirmation(self, *_args):
        self.factory_verified.setChecked(False)
        self.factory_completed.setChecked(False)

    def _factory_identity_changed(self, *_args):
        self._source_epoch += 1
        self._reset_factory_confirmation()

    def _reset_research_confirmation(self, *_args):
        self.research_verified.setChecked(False)
        self.research_completed.setChecked(False)

    def _research_source_changed(self, *_args):
        self._exports = None
        self._recordings = []
        self._fill_combo(self.research_record, [], str, "来源已改变，请重新读取已结束记录")
        self._recording_selected()

    def _factory_step_requested(self, index):
        if self.platform_worker is not None:
            return
        if index > 0 and not self._factory_preview:
            self.platform_notice.setText("请先选择结果CSV，读取本次测试的项目和原始结论。")
            return
        if index == 2 and not self._factory_mapping_valid():
            return
        if index == 2:
            preview = self._factory_preview
            self.factory_confirmation_summary.setText(f"样品 {preview['serial_number']} · {preview['model']} · 上位机 {preview['source_id']}\n采集ID {self.factory_robot.text().strip()} · 来源 {'模拟数据' if self.factory_kind.currentData() == 'simulated' else '真实采集（人员确认）'} · 测试人员 {' / '.join(preview.get('operators', [])) or 'CSV未填写'}\n{preview['filename']} · 文件中的未测试、不适用和不通过结论将原样保存。")
        self.factory_flow.set_index(index)
        self.factory_flow.hint.setText(("文件信息自动读取；来源需要人员核对。", "逐项核对原记录，父项与子项分别保留。", "提交前仍需两个人工确认；不会自动上传。")[self.factory_flow.index])

    def _factory_mapping_valid(self):
        if not self.factory_robot.text().strip():
            self.platform_notice.setText("请选择真实采集机器人，或填写已经核实的采集ID映射；不使用SN猜测。")
            return False
        if self.factory_manual_mapping.isChecked() and not self.factory_mapping_confirmed.isChecked():
            self.platform_notice.setText("请明确核实此采集ID与文件中的样品对应。")
            return False
        if not self.factory_kind.currentData():
            self.platform_notice.setText("文件未标明真实或模拟来源，请核对原记录后选择。当前连接不能代替本文件来源确认。")
            return False
        if self._factory_preview and self._factory_preview.get("simulated_marker") and self.factory_kind.currentData() != "simulated":
            self.platform_notice.setText("文件含模拟数据标记，只能按模拟数据提交。")
            return False
        return True

    def _factory_mapping_mode_changed(self, checked):
        self.factory_mapping_box.setVisible(checked)
        self.factory_robot.setReadOnly(not checked)
        if checked:
            self.factory_robot_selector.blockSignals(True)
            self.factory_robot_selector.setCurrentIndex(0)
            self.factory_robot_selector.blockSignals(False)
        else:
            self.factory_robot.clear()
        self.factory_mapping_confirmed.setChecked(False)
        self._factory_identity_changed()

    def _factory_robot_selected(self, *_args):
        selected = self.factory_robot_selector.currentData()
        if not selected:
            return
        self.factory_manual_mapping.blockSignals(True)
        self.factory_manual_mapping.setChecked(selected.get("binding") == "stored")
        self.factory_manual_mapping.blockSignals(False)
        self.factory_mapping_box.setVisible(selected.get("binding") == "stored")
        self.factory_robot.setReadOnly(selected.get("binding") != "manual")
        self.factory_robot.setText(selected["robot_id"])
        self.factory_mapping_confirmed.setChecked(False)
        self.factory_robot_status.setText(selected.get("label", "已选择采集身份") + "\n请核对它与本文件样品的对应关系。")
        self._factory_identity_changed()

    def _prefill_factory_identity(self):
        if not hasattr(self, "factory_robot_selector"):
            return
        existing = self.factory_robot.text().strip()
        choices = []
        seen = set()
        for robot_id, value in self.station_connections.items():
            if value.get("connected"):
                identity = value.get("robot", {})
                label = f"{robot_id} · 后台连接列表"
                if identity.get("source_kind") == "simulated":
                    label += "（当前为模拟）"
                choices.append({"robot_id": robot_id, "label": label, "binding": "connected"})
                seen.add(robot_id)
        frame = self.last_frame or {}
        if frame.get("robot_id") and not self.health_expired and self.last_received and time.monotonic() - self.last_received < 2 and frame["robot_id"] not in seen:
            choices.append({"robot_id": frame["robot_id"], "label": frame["robot_id"] + " · 当前预览", "binding": "connected"})
            seen.add(frame["robot_id"])
        bound = None
        preview = self._factory_preview
        store = getattr(self.factory_panel, "store", None)
        if preview and store:
            bound = store.sn_identity(preview["serial_number"])
            if bound and bound.get("robot_id") not in seen:
                choices.append({"robot_id": bound["robot_id"], "label": bound["robot_id"] + " · 此CSV样品的本地历史绑定", "binding": "stored"})
        self._fill_combo(self.factory_robot_selector, choices, lambda row: row["label"], "选择采集机器人")
        # An exact CSV-SN binding may suggest an ID, never a data-source kind.
        if bound and not existing:
            index = next((i for i in range(self.factory_robot_selector.count()) if (self.factory_robot_selector.itemData(i) or {}).get("robot_id") == bound.get("robot_id")), -1)
            if index >= 0:
                self.factory_robot_selector.setCurrentIndex(index)
        elif existing:
            index = next((i for i in range(self.factory_robot_selector.count()) if (self.factory_robot_selector.itemData(i) or {}).get("robot_id") == existing), -1)
            if index >= 0:
                self.factory_robot_selector.blockSignals(True)
                self.factory_robot_selector.setCurrentIndex(index)
                self.factory_robot_selector.blockSignals(False)

    @Slot(str)
    def _local_exported(self, path):
        if self.platform_worker is not None:
            self._pending_factory_path = path
            self.platform_notice.setText("新CSV已导出，当前任务完成后会自动读取预览；当前提交保持原身份和文件。")
            return
        self._clear_factory_preview()
        self.factory_path.setText(path)
        self.factory_file_label.setText(Path(path).name)
        if not self._valid_session() or not self.session.get("can_submit_factory"):
            self.platform_notice.setText("结果文件已保留。请以有提交权限的账号登录后核对预览。尚未上传。")
            return
        self.select_domain("FACTORY")
        self.workspace_tabs.setCurrentIndex(0)
        self.factory_flow.set_index(0)
        self.request("factory_preview", {"path": path}, self._factory_preview_loaded)

    def _clear_factory_preview(self):
        self._factory_preview = None
        self._factory_projects = []
        self._factory_current_rows = []
        self._factory_current_project = None
        self._pending_factory_path = None
        self.factory_path.clear()
        self.factory_file_label.setText("尚未选择结果文件")
        self.factory_hash_label.setText("尚无文件校验信息")
        self.factory_preview_note.setText("选择CSV后自动读取文件信息和逐项原始结论，不会自动上传。")
        self.factory_confirmation_summary.setText("尚未选择结果")
        self.factory_project_title.setText("选择测试项目")
        self.factory_project_target.clear()
        self.factory_row_caption.clear()
        for widget in (self.factory_result_text, self.factory_criterion_text, self.factory_notes_text, self.factory_record_text):
            widget.clear()
        self._fill_combo(self.factory_robot_selector, [], str, "选择采集机器人")
        self.factory_manual_mapping.blockSignals(True)
        self.factory_manual_mapping.setChecked(False)
        self.factory_manual_mapping.blockSignals(False)
        self.factory_mapping_box.hide()
        self.factory_robot_status.setText("尚未选择采集身份")
        self.factory_flow.set_index(0)
        self.factory_flow.hint.setText("选择文件只做预览，不会自动上传。")
        self.factory_source.clear()
        self.factory_robot.clear()
        self.factory_mapping_confirmed.setChecked(False)
        self.factory_kind.setCurrentIndex(0)
        self.factory_kind.setEnabled(True)
        self._reset_factory_confirmation()
        self.factory_project_list.setRowCount(0)
        self.factory_rows_table.setRowCount(0)
        for value in self.factory_meta.values():
            value.setText("—")

    def _factory_preview_loaded(self, value):
        preview = value.get("preview")
        if not isinstance(preview, dict) or not preview.get("sha256"):
            raise ValueError("preview missing")
        self._factory_preview = deepcopy(preview)
        self._factory_projects = preview.get("projects", [])
        self.factory_path.setText(preview["path"])
        self.factory_source.setText(preview["source_id"])
        self.factory_file_label.setText(preview["filename"])
        for key, value in (("sn", preview["serial_number"]), ("model", preview["model"]), ("source", preview["source_id"]), ("operators", " / ".join(preview.get("operators", [])) or "CSV未填写")):
            self.factory_meta[key].setText(value)
        present = sum(bool(project.get("present")) for project in self._factory_projects)
        self.factory_preview_note.setText(f"已读取 {present} 个目录项目、{len(preview.get('rows', []))} 条原记录。父项与子项结论分别保留。")
        self.factory_hash_label.setText(f"SHA256：{preview['sha256']}\n原文件：{preview['path']}\n大小：{preview['size_bytes']} bytes")
        if preview.get("simulated_marker"):
            self.factory_kind.setCurrentIndex(self.factory_kind.findData("simulated"))
            self.factory_kind.setEnabled(False)
        self._prefill_factory_identity()
        rows = []
        original = preview.get("rows", [])
        for project in self._factory_projects:
            if not project.get("present"):
                status = "本文件未包含"
            elif not project.get("parent_indices"):
                status = "仅子项/父项缺失"
            else:
                verdicts = [original[index]["verdict"] for index in project["parent_indices"]]
                status = verdicts[0] if len(verdicts) == 1 else " / ".join(dict.fromkeys(verdicts))
            rows.append((project["case_code"], project["name"], status))
        rows_into(self.factory_project_list, rows)
        for index in range(self.factory_project_list.rowCount()):
            self.factory_project_list.setRowHeight(index, 24)
        if rows:
            self.factory_project_list.setCurrentCell(0, 0)
        self.factory_flow.set_index(1)
        self.factory_flow.hint.setText("可先逐项查看；提交前再确认采集身份与数据来源。")
        self.platform_notice.setText("结果已读取，仅在本机预览。可直接核对项目；提交前仍须确认采集身份与文件来源。")

    def _factory_project_selected(self, index):
        if not self._factory_preview or not 0 <= index < len(self._factory_projects):
            return
        project = self._factory_projects[index]
        self._factory_current_project = project
        rows = self._factory_preview["rows"]
        self._factory_current_rows = [rows[item] for item in project.get("row_indices", [])]
        self.factory_project_title.setText(f"{project['case_code']} · {project['name']}")
        mapping = f"采集ID {self.factory_robot.text().strip()}" if self.factory_robot.text().strip() else "采集映射待确认"
        self.factory_project_target.setText(f"样品SN {self._factory_preview['serial_number']} · {self._factory_preview['model']} · {mapping} · 以下保留CSV原文")
        rows_into(self.factory_rows_table, [(row.get("original_check_id") or row.get("case_code"), row["test_name"], row["sample_id"], row["verdict"]) for row in self._factory_current_rows])
        if not self._factory_current_rows:
            self.factory_row_caption.setText("本文件未包含该项目记录；不合成未测试或通过结论。")
            for widget in (self.factory_result_text, self.factory_criterion_text, self.factory_notes_text, self.factory_record_text):
                widget.clear()
        else:
            self.factory_rows_table.setCurrentCell(0, 0)
            self._factory_row_selected(0)

    def _factory_row_selected(self, index):
        if not 0 <= index < len(self._factory_current_rows):
            return
        row = self._factory_current_rows[index]
        self.factory_row_caption.setText(f"{row['level']} · CSV原记录：{row['test_name']} · 测试人员 {row.get('operator') or '未填写'} · {row['verdict']}")
        self.factory_result_text.setPlainText(row.get("actual_result") or "CSV未填写实测结果")
        self.factory_criterion_text.setPlainText(row.get("criterion") or "CSV未填写通过标准")
        self.factory_notes_text.setPlainText(row.get("notes") or "CSV未填写备注")
        self.factory_record_text.setPlainText(f"CSV记录编号：{row.get('record_id')}\n上位机：{row.get('station_id')}\n样品SN：{row.get('sample_id')}\n层级：{row.get('level')}\n原检查项ID：{row.get('original_check_id')}\n原开始时间：{row.get('started_at') or '未填写'}\n原结束时间：{row.get('ended_at') or '未填写'}")

    def _open_factory_project(self):
        project = getattr(self, "_factory_current_project", None)
        self.open_local_workbench(9)
        if project:
            self.factory_panel.select_case(project["case_code"])
        self.platform_notice.setText("已打开原本地测试工作台。请核对当前SN与CSV样品一致后查看原始证据；不会自动关联、改写或运行测试。")

    def _research_step_requested(self, index):
        if self.platform_worker is not None:
            return
        if index > 0 and not self._exports:
            if self.research_record.currentData():
                self._research_next_after_export = True
                self.export_research()
            else:
                self.platform_notice.setText("请先选择一条已结束的原始记录。")
            return
        if index > 1 and not (self.research_context.currentData() or {}).get("context"):
            self.platform_notice.setText("请选择真实测试用例、计划和对应样品上下文。")
            return
        self.research_flow.set_index(index)
        self.research_flow.hint.setText(("只准备原始记录，不会送审。", "保留所选上下文的原发布版本。", "实际声明保持空白，须按真实测试填写。")[self.research_flow.index])
        if index == 1 and not self._catalog:
            self.request("setup", {}, self._setup_loaded)

    def _duration_changed(self, *_args):
        values = [field.text().strip() for field in self.duration_fields]
        if not any(values):
            self.research_seconds.clear()
            return
        try:
            if any(value and not value.isdecimal() for value in values[:2]):
                raise ValueError("duration")
            hours, minutes = (int(value or 0) for value in values[:2])
            seconds = float(values[2] or 0)
            if minutes > 59 or not isfinite(seconds) or not 0 <= seconds < 60:
                raise ValueError("duration")
            self.research_seconds.setText(str(hours * 3600 + minutes * 60 + seconds))
        except ValueError:
            self.research_seconds.clear()

    def _clear_research_declarations(self):
        for field in (self.research_started, self.research_ended, self.research_seconds, self.research_cycles, self.research_exceptions):
            field.clear()
        for widget in (self.research_start_picker, self.research_end_picker):
            widget.input.setDateTime(widget.input.minimumDateTime())
        for entry in self.duration_fields:
            entry.clear()
        self.research_verdict.setCurrentIndex(0)
        self._reset_research_confirmation()

    def choose_factory_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择原17列整机结果CSV", "", "结果CSV (*.csv)")
        if path:
            self._local_exported(path)

    def submit_factory(self):
        if not self._factory_preview:
            self.platform_notice.setText("请先读取结果文件，逐项核对后提交。")
            return
        if not self._factory_mapping_valid():
            return
        if not self.factory_verified.isChecked() or not self.factory_completed.isChecked():
            self.platform_notice.setText("须先核对工厂结果并明确确认本次测试已结束。")
            return
        payload = {
            "path": self._factory_preview["path"], "source_id": self._factory_preview["source_id"],
            "robot_id": self.factory_robot.text().strip(), "source_kind": self.factory_kind.currentData(),
            "reason": self.factory_reason.text().strip(), "verified": True, "completed": True,
            "expected_sha256": self._factory_preview["sha256"],
        }
        if not all(payload[key] for key in ("path", "source_id", "robot_id", "source_kind", "reason")):
            self.platform_notice.setText("请补全原CSV、上位机编号、实际机器人ID、来源及提交说明。")
            return
        self.request("factory_submit", payload, self._factory_submitted)

    def _factory_submitted(self, value):
        receipt = value.get("receipt") or {}
        task = value.get("task") or {}
        status = _APPROVAL.get(receipt.get("status"), _STATES.get(task.get("status"), task.get("status") or "待处理"))
        self.platform_notice.setText(f"工厂汇总：{status} · 记录 {receipt.get('id') or task.get('archive_id') or '尚未确认送达'}。提交人取平台会话；不赋予可靠性或MTBF统计资格。")
        if receipt:
            self.factory_verified.setChecked(False)
            self.factory_completed.setChecked(False)

    def choose_pg_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择只读PG私有连接文件", "", "连接文件 (*.txt);;所有文件 (*)")
        if path:
            self.pg_file.setText(path)
            self._exports = None
            self.export_label.setText("来源已改变，请重新读取并导出原始记录。")

    def load_research_records(self):
        if not self.pg_file.text().strip():
            self.platform_notice.setText("请选择当前用户所有、权限0600的PG私有连接文件。")
            return
        self.request("research_records", {"postgres_file": self.pg_file.text().strip(), "limit": 100}, self._records_loaded)

    @staticmethod
    def _fill_combo(widget, items, title, placeholder):
        widget.blockSignals(True)
        widget.clear()
        widget.addItem(placeholder, None)
        for item in items:
            widget.addItem(title(item), item)
        widget.blockSignals(False)

    def _records_loaded(self, value):
        self._recordings = [row for row in value.get("items", []) if row.get("ended_at")]
        self._fill_combo(self.research_record, self._recordings, lambda row: f"{row.get('run_number') or row.get('id')} · {row.get('robot_id')} · {row.get('source_kind')}", "选择已结束监控记录候选")
        self._recording_selected()
        self.platform_notice.setText(f"读取到 {len(self._recordings)} 条已结束记录。结束不代表测试通过，原始点导出仍须核对完整性。")

    def _recording_selected(self, *_args):
        self._source_epoch += 1
        record = self.research_record.currentData() or {}
        self._exports = None
        self.export_label.setText("选择后准备原始记录，再继续关联用例。")
        self.export_evidence_label.setText("尚未准备所选记录")
        self._clear_research_declarations()
        if record:
            def local_time(value):
                try:
                    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return str(value or "未提供")
            self.record_reference.setText(f"{record.get('run_number') or '原监控记录'}\n机器人 {record.get('robot_id')} · {record.get('sample_count', '待核对')} 个原始点 · {record.get('frequency_hz')} Hz\n观察时段：{local_time(record.get('started_at'))} — {local_time(record.get('ended_at'))}（北京时间，仅供参考）")
        else:
            self.record_reference.setText("请选择原监控记录；人工声明不会从原记录起止自动填写。")

    def export_research(self):
        record = self.research_record.currentData()
        if not record or not self.pg_file.text().strip():
            self.platform_notice.setText("请先选择已结束的PG原监控记录。")
            return
        self.request("research_export", {"postgres_file": self.pg_file.text().strip(), "recording_id": record.get("recording_id") or record.get("id"), "robot_id": record.get("robot_id")}, self._research_exported)

    def _research_exported(self, value):
        self._exports = deepcopy(value.get("export"))
        if not self._exports:
            raise ValueError("missing export")
        output = self._exports
        self.export_label.setText(f"原始记录已准备：{output.get('sample_count')} 个采样点。原记录保留，可继续关联用例。")
        self.export_evidence_label.setText(f"文件：{output.get('path')}\nSHA256：{output.get('sha256')}\n大小：{output.get('size_bytes')} bytes")
        if getattr(self, "_research_next_after_export", False):
            self._research_next_after_export = False
            self.research_flow.set_index(1)
            if not self._catalog:
                self._deferred_platform_action = lambda: self.request("setup", {}, self._setup_loaded)
        self.platform_notice.setText("精确原始点导出完成。请逐项填写实际测试声明，关联平台上下文后核实送审。")

    def _setup_loaded(self, value):
        self._catalog = [row for row in value.get("catalog", []) if row.get("asset_kind") == "WHOLE_MACHINE" and row.get("target_part_code") == "SYS" and row.get("enabled", True) and row.get("version_id")]
        campaigns = [row for row in value.get("campaigns", []) if row.get("asset_kind") == "WHOLE_MACHINE" and row.get("status") != "CANCELLED"]
        stations = [row for row in value.get("options", {}).get("stations", []) if row.get("station_type") in {"WHOLE_MACHINE", "SHARED"}]
        self._fill_combo(self.research_catalog, self._catalog, lambda row: f"{row.get('code')} · {row.get('name')} · v{row.get('version')}", "选择已发布SYS用例")
        self._fill_combo(self.research_campaign, campaigns, lambda row: f"{row.get('code')} · {row.get('name')}", "选择已有执行上下文的整机研发计划")
        self._fill_combo(self.research_station, stations, lambda row: f"{row.get('code')} · {row.get('name')}", "选择实际测试工位")
        self._contexts = []
        self._filter_contexts()
        self.platform_notice.setText("平台目录已读取。请选择真实用例、计划与样品上下文；不会创建替代UUID。")

    def _campaign_selected(self, *_args):
        selected = self.research_campaign.currentData()
        self._contexts = []
        self._filter_contexts()
        if selected:
            self.request("contexts", {"campaign_id": selected["id"]}, self._contexts_loaded)

    def _contexts_loaded(self, value):
        self._contexts = value.get("items", [])
        self._filter_contexts()
        self.platform_notice.setText("已读取计划执行上下文，请核对真实样品、配置、循环、阶段和工位。")

    def _filter_contexts(self, *_args):
        selected = self.research_catalog.currentData() or {}
        code = selected.get("code")
        items = [row for row in self._contexts if code and row.get("case_code") == code and row.get("status") != "CANCELLED" and all(row.get("context", {}).get(key) for key in _CONTEXT_KEYS)]
        self._fill_combo(self.research_context, items, lambda row: f"{row.get('case_code')} · {row.get('case_name')} · {row.get('asset_code')} · v{row.get('version', '原版')}", "选择真实上下文；保留其原发布版本")
        self._context_selected()

    def _context_selected(self, *_args):
        self._reset_research_confirmation()
        selected = self.research_context.currentData() or {}
        context = selected.get("context", {})
        if context:
            context_key = tuple(context.get(key) for key in _CONTEXT_KEYS)
            if context_key != getattr(self, "_research_context_key", None):
                self.research_test_name.setText(selected.get("case_name") or (self.research_catalog.currentData() or {}).get("name") or "")
                self.research_object.setText(selected.get("asset_code") or "")
                self._research_context_key = context_key
            index = next((i for i in range(self.research_station.count()) if (self.research_station.itemData(i) or {}).get("id") == context["station_id"]), -1)
            self.research_station.blockSignals(True)
            if index < 0:
                self.research_station.addItem("上下文指定工位（查看详情）", {"id": context["station_id"]})
                index = self.research_station.count() - 1
            self.research_station.setCurrentIndex(index)
            self.research_station.blockSignals(False)
            self.context_label.setText("已选执行身份（工位随真实上下文固定；实际工位变化须先配置对应上下文）：\n" + "\n".join(f"{key}：{context.get(key)}" for key in _CONTEXT_KEYS))
        else:
            self.context_label.setText("尚未选择完整的7项执行身份；不会生成或猜测UUID。")
        self.research_station.setEnabled(False)

    def submit_research(self):
        if not self.research_verified.isChecked() or not self.research_completed.isChecked():
            self.platform_notice.setText("须先逐项核实原始证据和人工声明，并确认实际测试已结束。")
            return
        context = (self.research_context.currentData() or {}).get("context", {})
        station = self.research_station.currentData() or {}
        if not self._exports or not all(context.get(key) for key in _CONTEXT_KEYS) or not station.get("id"):
            self.platform_notice.setText("请先导出精确原点，选择真实7项执行身份及实际工位。")
            return
        if station["id"] != context["station_id"]:
            self.platform_notice.setText("实际工位须与真实执行上下文一致；请在平台配置并选择对应工位上下文，不会自动替换UUID。")
            return
        try:
            start = datetime.fromisoformat(self.research_started.text().strip().replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.research_ended.text().strip().replace("Z", "+00:00"))
            seconds = float(self.research_seconds.text().strip())
            cycles_text, exceptions_text = self.research_cycles.text().strip(), self.research_exceptions.text().strip()
            if not cycles_text.isdecimal() or not exceptions_text.isdecimal():
                raise ValueError("counts")
            cycles, exceptions = int(cycles_text), int(exceptions_text)
            if start.tzinfo is None or end.tzinfo is None or end < start or not 0 <= seconds <= 315_360_000 or seconds > (end - start).total_seconds() + 0.001 or max(cycles, exceptions) > 2_147_483_647:
                raise ValueError("declarations")
        except (ValueError, OverflowError):
            self.platform_notice.setText("请选择实际测试起止，填写有效运行时长与非负整数次数；分钟和秒为0—59，结束不能早于开始。")
            return
        verification = {
            "tester": self.research_tester.text().strip(), "station_id": station["id"],
            "verdict": self.research_verdict.currentData(), "notes": self.research_notes.toPlainText().strip(),
            "declared_started_at": start.isoformat(), "declared_ended_at": end.isoformat(),
            "declared_active_seconds": seconds, "declared_completed_cycles": cycles,
            "declared_exception_count": exceptions,
        }
        if len(verification["notes"]) > 1000:
            self.platform_notice.setText("核实备注最多1000字，请保留需要中央归档的实际测试依据。")
            return
        payload = {
            "export_id": self._exports["export_id"], "context": deepcopy(context),
            "verification": verification, "test_name": self.research_test_name.text().strip(),
            "actual_object": self.research_object.text().strip(), "reason": self.research_reason.text().strip(),
            "verified": True, "completed": True,
        }
        if not all((verification["tester"], verification["verdict"], verification["notes"], payload["test_name"], payload["actual_object"], payload["reason"])):
            self.platform_notice.setText("请补全实际测试人、人工结论、核实备注、测试名称、实际对象和送审说明。")
            return
        self.request("research_submit", payload, self._research_submitted)

    def _research_submitted(self, value):
        archive, task = value.get("archive") or {}, value.get("task") or {}
        status = _APPROVAL.get(archive.get("submission_status"), _STATES.get(task.get("status"), task.get("status") or "待处理"))
        self.platform_notice.setText(f"研发归档：{status} · 记录 {archive.get('id') or task.get('archive_id') or '尚未确认送达'}。等待平台处理和审批；未自动判定通过或计入正式统计。")
        if archive.get("submission_status") == "PENDING":
            self.research_verified.setChecked(False)
            self.research_completed.setChecked(False)

    def refresh_results(self, before=None):
        payload = {"limit": 50}
        query = self.result_search.text().strip()
        status = self.result_status.currentData()
        if self.domain == "FACTORY":
            payload.update(serial_number=query, status=status)
            action = "factory_results"
        else:
            payload.update(q=query, submission_status=status)
            action = "research_results"
        if before is not None and not isinstance(before, bool):
            payload["before"] = before
        self.request(action, payload, self._results_loaded)

    def more_results(self):
        cursor = getattr(self, "_next_cursor", None)
        if cursor is not None:
            self.refresh_results(cursor)

    def _results_loaded(self, value):
        self._archives = value.get("items", [])
        self._next_cursor = value.get("next_cursor")
        rows = []
        for item in self._archives:
            approval = item.get("status") if self.domain == "FACTORY" else item.get("submission_status")
            identity = " · ".join(str(item.get(key)) for key in ("serial_number", "robot_id", "source_id") if item.get(key)) or item.get("actual_object") or "未记录"
            if item.get("source_kind") == "simulated":
                identity = "模拟数据 · " + identity
            rows.append((item.get("filename") or item.get("archive_code") or item.get("id"),
                         identity,
                         " / ".join(_STATES.get(item.get(key), item.get(key) or "") for key in ("upload_state", "processing_state")) if self.domain == "RESEARCH" else "原CSV已保存",
                         _APPROVAL.get(approval, "未送审"), item.get("owner_name") or item.get("owner_public_id") or "未返回"))
        rows_into(self.results_table, rows)
        self.platform_notice.setText(f"已读取{('工厂' if self.domain == 'FACTORY' else '研发')}平台记录 {len(rows)} 条；权限及可审核状态由平台确定。")
        self._update_platform_actions()

    def _selected_archive(self):
        index = self.results_table.currentRow()
        return self._archives[index] if 0 <= index < len(self._archives) else None

    def open_record(self, curves=False):
        selected = self._selected_archive()
        if selected and self._valid_session() and not (curves and self.domain == "FACTORY"):
            self._open_link(domain=self.domain, archive_id=selected["id"], curves=bool(curves))

    def show_detail(self):
        selected = self._selected_archive()
        if selected:
            action = "factory_detail" if self.domain == "FACTORY" else "research_detail"
            self.request(action, {"archive_id": selected["id"]}, self._detail_loaded)

    def _detail_loaded(self, value):
        item = value.get("archive") or {}
        dialog = QDialog(self)
        dialog.setWindowTitle("工厂汇总详情" if self.domain == "FACTORY" else "研发归档详情")
        dialog.resize(1050, 680)
        layout = QVBoxLayout(dialog)
        status = item.get("status") if self.domain == "FACTORY" else item.get("submission_status")
        layout.addWidget(text(item.get("test_name") or item.get("filename") or "测试记录", "cardTitle"))
        identity = item.get("serial_number") or item.get("actual_object") or "对象未提供"
        layout.addWidget(text(f"{identity} · {_APPROVAL.get(status, '未送审')} · 审核状态版本 {item.get('revision', '未知')}", "cardHint"))
        proof = Disclosure("原件校验")
        proof.body_layout.addWidget(text(f"记录编号：{item.get('id')}\n原文件：{item.get('filename')}\nSHA256：{item.get('sha256')}", "cardHint"))
        layout.addWidget(proof)
        if item.get("source_kind") == "simulated":
            layout.addWidget(text("模拟数据 · 不能批准为实机工厂成果。", "platformNotice"))
        if self.domain == "FACTORY":
            from .desktop_platform_details import FactoryArchiveView

            layout.addWidget(FactoryArchiveView(item), 1)
        else:
            from .desktop_research_archive_view import ResearchArchiveView

            layout.addWidget(ResearchArchiveView(item), 1)
        history_box = Disclosure("审批记录")
        history = QPlainTextEdit()
        history.setReadOnly(True)
        history.setMaximumHeight(100)
        history.setAccessibleName("审批历史")
        history.setPlainText(json.dumps(item.get("review_history", []), ensure_ascii=False, indent=2))
        history_box.body_layout.addWidget(history)
        layout.addWidget(history_box)
        if self.domain == "FACTORY":
            layout.addWidget(text("本文件为工厂结果汇总，无原始采样曲线。批准不赋予可靠性/MTBF统计资格；原始曲线在本地测试记录查看。"))
            if item.get("can_review") and self.session.get("can_review_factory") and status == "PENDING":
                reason = edit("工厂审核说明", "核对后填写审核依据，最多300字")
                reason.setMaxLength(300)
                layout.addWidget(reason)
                review = QHBoxLayout()
                for decision, title in (("APPROVE", "批准工厂汇总"), ("REJECT", "驳回工厂汇总")):
                    if decision == "APPROVE" and item.get("source_kind") == "simulated":
                        continue
                    review.addWidget(button(title, lambda _checked=False, decision=decision: self._review_factory(dialog, item, decision, reason.text())))
                layout.addLayout(review)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._detail_dialogs.add(dialog)
        dialog.finished.connect(lambda _result: self._detail_dialogs.discard(dialog))
        dialog.finished.connect(dialog.deleteLater)
        dialog.show()
        self.platform_notice.setText("已读取平台保存的证据与审批状态。")

    def _review_factory(self, dialog, item, decision, reason):
        if not reason.strip():
            self.platform_notice.setText("请填写审核依据；审批与提交是独立动作。")
            return
        if self.request("factory_review", {"archive_id": item["id"], "decision": decision, "expected_revision": item["revision"], "reason": reason.strip()}, self._reviewed):
            dialog.reject()

    def _reviewed(self, value):
        item = value.get("archive") or {}
        self.platform_notice.setText(f"工厂审核回执：{_APPROVAL.get(item.get('status'), '请刷新核对')} · 审核状态版本 {item.get('revision')}。未改写原CSV或原始测试证据。")

    def _tasks_loaded(self, value):
        self._tasks = value.get("items", [])
        rows_into(self.tasks_table, [("工厂" if row.get("domain") == "FACTORY" else "研发", row.get("filename") or row.get("sha256"), _STATES.get(row.get("status"), row.get("status")), row.get("archive_id") or "尚未确认送达", row.get("last_error") or "") for row in self._tasks])
        self.platform_notice.setText("已读取当前平台账号的本机提交任务。传输状态不等于审批通过。")

    def retry_task(self):
        index = self.tasks_table.currentRow()
        if not 0 <= index < len(self._tasks):
            self.platform_notice.setText("请先选择待处理任务。")
            return
        task = self._tasks[index]
        if task.get("status") == "sent":
            self.platform_notice.setText("该任务已送达，请查询平台审批状态；不会重复提交已完成任务。")
            return
        if task.get("domain") != self.domain:
            self.platform_notice.setText("请先切换到该任务对应分区，再核对并重试。")
            return
        self.request("retry", {"task_id": task["task_id"]}, self._tasks_loaded)

    def open_offline_history(self):
        store = getattr(self.factory_panel, "store", None)
        if not store:
            self.platform_notice.setText("本地工厂目录不可用，请核对原目录权限；未连接机器人或平台。")
            return
        self.platform_stack.setCurrentWidget(self.offline_history)
        self.request("local_history", {}, self._offline_roster_loaded, local_operation=lambda: {"items": store.roster()})

    def _offline_roster_loaded(self, value):
        self._fill_combo(self.offline_sn, value.get("items", []), lambda row: row["sn"], "选择电脑已有SN")
        rows_into(self.offline_table, [])
        self.platform_notice.setText("本地历史只读模式；未发起机器人连接或平台请求。")

    def _offline_sn_selected(self, *_args):
        selected = self.offline_sn.currentData()
        if selected:
            store = self.factory_panel.store
            sn = selected["sn"]
            self.request("local_history", {}, self._offline_rows_loaded, local_operation=lambda: {"items": store.report_rows(sn)})

    def _offline_rows_loaded(self, value):
        rows_into(self.offline_table, [(row.get("case_code"), row.get("test_name"), row.get("verdict"), row.get("operator"), row.get("actual_result")) for row in value.get("items", [])])
        self.platform_notice.setText("显示电脑已有结果；本地测试人员文字不等于平台登录身份。")

    def closeEvent(self, event):
        self._platform_closing = True
        if self.platform_worker is not None and self.platform_worker.isRunning():
            self.platform_service.cancel_current()
            self.platform_worker.requestInterruption()
            self.platform_notice.setText("正在结束平台后台任务后关闭。不会强杀线程；本地设备操作继续按原关闭链路处理。")
            QTimer.singleShot(100, self.close)
            event.ignore()
            return
        if not self._platform_closed:
            self.platform_service.close()
            self._platform_closed = True
        super().closeEvent(event)

    def finish_platform_workers(self):
        """Safety net after the Qt event loop, including direct QApplication.quit()."""
        self._platform_closing = True
        self.platform_service.cancel_current()
        for worker in list(_WORKERS):
            worker.requestInterruption()
            worker.wait()
        self.platform_service.close()

    def on_frame(self, frame):
        if getattr(self, "platform_ready", False):
            previous = (self.last_frame or {}).get("robot_id")
            if previous != frame.get("robot_id"):
                self._source_epoch += 1
        super().on_frame(frame)

    def start_initial_connection(self, args):
        """Original local startup, once per window and independent of any account."""
        if self._platform_closing or self._local_initialization_started:
            return
        if self.platform_worker is not None:
            QTimer.singleShot(100, lambda: self.start_initial_connection(args))
            return
        self._local_initialization_started = True
        if getattr(args, "factory", False):
            self.open_local_workbench(9)
        elif getattr(args, "quick_connect", False):
            def operation():
                from .station import ensure_station

                try:
                    return {"station": ensure_station(args.station_dir, args.station_port)}
                except Exception as exc:
                    return {"station_error": f"电脑后台暂不可用：{type(exc).__name__}；本地历史仍保留。"}

            self.request("local_bootstrap", {}, self._initial_station_ready, local_operation=operation)
        else:
            self.connect_source()

    def _initial_station_ready(self, value):
        if value.get("station_error"):
            self.platform_notice.setText(value["station_error"])
            self.open_local_workbench(9)
            return
        self.station_url, self.station_token_file = value["station"]
        self.connection_mode.setCurrentIndex(2)
        self.ssh_target.setText("ssh robo@192.168.137.250")
        self.open_settings()
        self.ssh_password.setFocus()
        QTimer.singleShot(0, self.restore_station_connection)
