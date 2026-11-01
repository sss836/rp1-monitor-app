"""PC-only recording controls and persistent-history plots; no robot commands."""

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QCoreApplication, QDateTime, Qt, QThread, QTimer, QTimeZone, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

_WORKERS = set()


def configure_postgres_file(source, viewer_token_file):
    """Import an explicitly selected private file without connecting to its database."""
    destination = Path(viewer_token_file).with_name("postgres-url.txt")
    if os.path.lexists(destination):
        raise FileExistsError("长期历史库已配置；请按文档人工变更，本入口不覆盖现有文件。")
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("请选择当前用户所有、权限为0600的普通私有连接文件，不接受符号链接。")
        with os.fdopen(source_fd, "r", encoding="utf-8", closefd=False) as handle:
            contents = handle.read(8193).strip()
        if len(contents) > 8192 or any(ord(c) < 32 for c in contents):
            raise ValueError("连接文件应只包含一行有效的PostgreSQL连接地址。")
        try:
            parsed = urlsplit(contents)
            parsed.port
        except ValueError:
            raise ValueError("连接文件不是有效的PostgreSQL地址。") from None
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or not contents.startswith(parsed.scheme+"://"):
            raise ValueError("只接受postgresql://或postgresql+psycopg://连接文件。")
    finally:
        os.close(source_fd)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_info = os.fstat(directory_fd)
        if directory_info.st_uid != os.getuid() or directory_info.st_mode & 0o077:
            raise ValueError("电脑后台配置目录必须属于当前用户，且其他用户没有访问权限。")
        temporary = ".postgres-url-"+uuid4().hex
        target_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=directory_fd)
        try:
            os.fchmod(target_fd, 0o600)
            with os.fdopen(target_fd, "w", encoding="utf-8", closefd=False) as handle:
                handle.write(contents+"\n")
                handle.flush()
                os.fsync(target_fd)
            os.link(temporary, destination.name, src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd, follow_symlinks=False)
            os.fsync(directory_fd)
        finally:
            os.close(target_fd)
            os.unlink(temporary, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def finish_workers():
    """Keep QThreads alive through a bounded PC HTTP request when the app exits."""
    for worker in list(_WORKERS):
        worker.requestInterruption()
        worker.wait(35000)


class RecordingWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, url, token_file, method, path, body=None):
        super().__init__()
        self.url, self.token_file, self.method, self.path, self.body = url, token_file, method, path, body

    def run(self):
        try:
            from .desktop_connection import local_endpoint, station_control_token

            url = local_endpoint(self.url)
            viewer = Path(self.token_file).read_text().strip()
            headers = {"Authorization": "Bearer " + viewer}
            if self.method == "POST":
                headers["X-RP1-Station-Token"] = station_control_token(self.token_file)
            with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
                r = client.request(self.method, url+self.path, headers=headers, json=self.body)
                r.raise_for_status()
                if not self.isInterruptionRequested():
                    self.done.emit(r.json())
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            self.failed.emit(detail if isinstance(detail, str) else "请检查输入参数与后台配置")
        except (OSError, ValueError, httpx.HTTPError):
            self.failed.emit("监控记录服务不可用；请用 start-monitor.sh 启动本机后台并核对 PostgreSQL 配置")


class RecordingsPanel(QWidget):
    def __init__(self, station):
        super().__init__()
        self.station = station
        self.context = {}
        self.records = []
        self.cursor = None
        self.busy = False
        self._callback = None
        self._save_selections = {}
        self._has_older_pages = False
        self._view_key = None
        self._view_selections = {}
        self.catalog = {}
        self.robot_id = None
        self._selection_generation = 0
        self._request_serial = 0
        self._drafts = {}
        app = QCoreApplication.instance()
        if app is not None and not app.property("rp1_recordings_shutdown"):
            app.aboutToQuit.connect(finish_workers)
            app.setProperty("rp1_recordings_shutdown", True)
        layout = QHBoxLayout(self)
        controls = QWidget()
        controls.setObjectName("healthCard")
        left = QVBoxLayout(controls)
        left.addWidget(QLabel("监控记录 · 独立于正式测试 / 审批 / MTBF"))
        self.configure_database = QPushButton("配置长期历史库")
        self.configure_database.clicked.connect(self.choose_postgres_file)
        left.addWidget(self.configure_database)
        database_note = QLabel("仅导入已准备好的私有PostgreSQL连接文件，后台重启后生效；不建库、不执行数据库迁移006。已有配置需按文档人工变更。")
        database_note.setWordWrap(True)
        left.addWidget(database_note)
        form = QFormLayout()
        self.robot_selector = QComboBox()
        self.robot_selector.setAccessibleName("监控记录机器人")
        self.robot_selector.currentIndexChanged.connect(self.recording_robot_changed)
        form.addRow("实际采集机器人", self.robot_selector)
        self.case = QLineEdit()
        self.case.setPlaceholderText("例如 PERF-SYS-001（仅标签）")
        self.robot = QLineEdit()
        self.robot.setPlaceholderText("例如 RP1.3-KM-CMF9（仅标签）")
        self.site = QLineEdit()
        self.site.setPlaceholderText("例如 16F")
        self.frequency = QDoubleSpinBox()
        self.frequency.setDecimals(3)
        self.frequency.setRange(0.001, 10)
        self.frequency.setValue(1)
        self.frequency.setSuffix(" Hz")
        for title, field in (("用例编号标签", self.case), ("机器人编号标签", self.robot),
                             ("场地", self.site), ("长期保存频率", self.frequency)):
            form.addRow(title, field)
        left.addLayout(form)
        self.number = QLabel()
        self.number.setWordWrap(True)
        left.addWidget(self.number)
        for field in (self.case, self.robot, self.site):
            field.textChanged.connect(self.number_preview)
        self.number_preview()
        self.source = QLabel("采集身份待连接；记录标签不会修改机器人身份。")
        self.source.setWordWrap(True)
        left.addWidget(self.source)
        left.addWidget(QLabel("长期保存指标 · 多选（缺失项保存为空值）"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索温度、力矩、关节编号…")
        self.search.textChanged.connect(self.filter_metrics)
        left.addWidget(self.search)
        presets = QGridLayout()
        self.presets = []
        for i, (title, quantity) in enumerate((("温度", "temperature"), ("力矩", "torque"), ("SOC", "soc"), ("关节电压", "voltage"), ("电池电流", "current"))):
            b = QPushButton(title)
            b.clicked.connect(lambda checked=False, q=quantity: self.preset(q))
            self.presets.append(b)
            presets.addWidget(b, i // 3, i % 3)
        left.addLayout(presets)
        self.save_metrics = QListWidget()
        self.save_metrics.setMinimumHeight(200)
        left.addWidget(self.save_metrics, 1)
        row = QHBoxLayout()
        self.start = QPushButton("开始长期记录")
        self.stop = QPushButton("结束本次记录")
        self.start.setEnabled(False)
        self.stop.setEnabled(False)
        self.start.clicked.connect(self.start_recording)
        self.stop.clicked.connect(self.stop_recording)
        row.addWidget(self.start)
        row.addWidget(self.stop)
        left.addLayout(row)
        note = QLabel("1 Hz=每秒一次；0.1 Hz=每10秒一次。频率控制电脑端记录，最高10 Hz受预览限速约束，不会提高机器人来源频率；同一源样本可能重复，原始时间与缺失保留。关闭窗口后电脑后台继续记录；无需改动机器人运控。")
        note.setWordWrap(True)
        left.addWidget(note)
        self.controls_scroll = QScrollArea()
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setMinimumWidth(300)
        self.controls_scroll.setMaximumWidth(440)
        self.controls_scroll.setWidget(controls)
        layout.addWidget(self.controls_scroll, 1)
        right = QVBoxLayout()
        self.service_status = QLabel("点击刷新读取 PostgreSQL 配置与历史记录。")
        self.service_status.setTextFormat(Qt.TextFormat.PlainText)
        self.service_status.setWordWrap(True)
        right.addWidget(self.service_status)
        self.status = QLabel("点击刷新读取 PostgreSQL 配置与历史记录。")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        toolbar = QHBoxLayout()
        self.refresh = QPushButton("刷新状态 / 记录")
        self.refresh.clicked.connect(lambda: self.refresh_context())
        toolbar.addWidget(self.refresh)
        self.more = QPushButton("加载更早记录")
        self.more.setEnabled(False)
        self.more.clicked.connect(lambda: self.load_records(True))
        toolbar.addWidget(self.more)
        right.addLayout(toolbar)
        self.session = QComboBox()
        self.session.currentIndexChanged.connect(self.session_changed)
        right.addWidget(self.session)
        self.period = QComboBox()
        for title, value in (("所选记录全部时段", 0), ("最近10分钟", 600), ("最近1小时", 3600), ("最近24小时", 86400), ("自定义区间", -1)):
            self.period.addItem(title, value)
        right.addWidget(self.period)
        custom = QHBoxLayout()
        self.range_start = QDateTimeEdit(QDateTime.currentDateTime().addSecs(-3600))
        self.range_end = QDateTimeEdit(QDateTime.currentDateTime())
        for title, widget in (("开始", self.range_start), ("结束", self.range_end)):
            custom.addWidget(QLabel(title))
            if hasattr(widget, "setTimeZone"):
                widget.setTimeZone(QTimeZone(b"Asia/Shanghai"))
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            custom.addWidget(widget)
        right.addLayout(custom)
        self.period.currentIndexChanged.connect(self.update_period)
        self.update_period()
        self.view_metrics = QListWidget()
        self.view_metrics.setMaximumHeight(125)
        right.addWidget(QLabel("显示指标 · 最多12条，按单位分图"))
        right.addWidget(self.view_metrics)
        self.query = QPushButton("查询完整曲线")
        self.query.clicked.connect(self.curves)
        right.addWidget(self.query)
        self.graphs = QWidget()
        self.graph_layout = QVBoxLayout(self.graphs)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.graphs)
        right.addWidget(scroll, 1)
        layout.addLayout(right, 2)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def remember_draft(self):
        if self.robot_id:
            self._save_selections.update({self.save_metrics.item(i).data(Qt.ItemDataRole.UserRole):
                                         self.save_metrics.item(i).checkState() == Qt.CheckState.Checked
                                         for i in range(self.save_metrics.count())})
            self._drafts[self.robot_id] = dict(case=self.case.text(), robot=self.robot.text(),
                                             site=self.site.text(), frequency=self.frequency.value(),
                                             selections=dict(self._save_selections), search=self.search.text())

    def set_robot(self, robot_id, refresh=None):
        """Select a recording identity without starting or stopping any recording."""
        if not robot_id or robot_id == self.robot_id:
            return
        self.remember_draft()
        self.robot_id = robot_id
        self._selection_generation += 1
        self.busy = False
        self._callback = None
        self.context = {}
        self.records = []
        self.cursor = None
        self._has_older_pages = False
        self._signature = None
        self.catalog = {}
        self.save_metrics.clear()
        self.session.clear()
        self.view_metrics.clear()
        while self.graph_layout.count():
            item = self.graph_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        draft = self._drafts.get(robot_id, {})
        self._save_selections = dict(draft.get("selections", {}))
        self.case.setText(draft.get("case", ""))
        self.robot.setText(draft.get("robot", ""))
        self.site.setText(draft.get("site", ""))
        self.frequency.setValue(draft.get("frequency", 1))
        self.search.setText(draft.get("search", ""))
        self.robot_selector.blockSignals(True)
        index = self.robot_selector.findData(robot_id)
        if index < 0:
            self.robot_selector.addItem(robot_id, robot_id)
            index = self.robot_selector.count() - 1
        self.robot_selector.setCurrentIndex(index)
        self.robot_selector.blockSignals(False)
        self.source.setText(f"实际采集身份：{robot_id} · 来源待连接")
        self.service_status.setText("正在读取所选机器人记录状态…")
        self.status.setText("已切换记录机器人；请核对本机标签和指标。")
        self.number_preview()
        self.update_actions()
        should_refresh = self.isVisible() if refresh is None else refresh
        if should_refresh:
            self.refresh_context()

    def recording_robot_changed(self):
        self.set_robot(self.robot_selector.currentData())

    def update_robots(self, data):
        robots = data.get("robots", [])
        if not robots and data.get("robot_id"):
            robots = [data]
        self.robot_selector.blockSignals(True)
        self.robot_selector.clear()
        if not self.robot_id:
            self.robot_selector.addItem("请选择记录机器人", None)
        for row in robots:
            identity = row.get("robot", row)
            robot_id = identity.get("robot_id")
            if not robot_id:
                continue
            kind = identity.get("source_kind")
            note = "模拟数据，非实机" if kind == "simulated" else "ROS2，仍待实机验收" if kind == "ros2" else "来源待连接"
            self.robot_selector.addItem(robot_id+" · "+note, robot_id)
        if self.robot_id and self.robot_selector.findData(self.robot_id) < 0:
            self.robot_selector.addItem(self.robot_id, self.robot_id)
        index = self.robot_selector.findData(self.robot_id)
        if index >= 0:
            self.robot_selector.setCurrentIndex(index)
        self.robot_selector.blockSignals(False)

    def number_preview(self):
        active = self.context.get("active")
        if active:
            self.number.setText("本次编号："+active["session"]["run_number"])
            return
        stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%y%m%d%H%M")
        self.number.setText(f"编号预览：{self.case.text()}_{stamp}_{self.robot.text()}_{self.site.text()}")

    def choose_postgres_file(self):
        _, token_file = self.station()
        if not token_file:
            self.status.setText("请先使用start-monitor.sh启动电脑后台，建立私有配置目录。")
            return
        destination = Path(token_file).with_name("postgres-url.txt")
        if os.path.lexists(destination):
            self.status.setText("长期历史库已配置；请按文档人工变更，本入口不覆盖现有文件。配置变更后需重启电脑后台。")
            return
        source, _ = QFileDialog.getOpenFileName(
            self, "选择私有PostgreSQL连接文件（0600，当前用户所有）", "",
            "连接文件 (*.txt *.conf *.url);;所有文件 (*)")
        if not source:
            return
        try:
            configure_postgres_file(source, token_file)
        except FileExistsError:
            self.status.setText("长期历史库已配置；请按文档人工变更，本入口不覆盖现有文件。")
        except ValueError as error:
            self.status.setText(str(error))
        except (OSError, UnicodeError):
            self.status.setText("无法导入私有连接文件；请核对当前用户权限、0600权限和文件类型，不接受符号链接。")
        else:
            self.status.setText("私有连接文件已保存。请在数据库迁移006审核并人工部署后重启电脑后台；本次未连接数据库、未建库、未执行迁移。重启前不能开启长期记录。")

    def tick(self):
        if self.isVisible():
            self.number_preview()
            if not self.busy:
                self.refresh_context(False)

    def update_period(self):
        custom = self.period.currentData() == -1
        self.range_start.setEnabled(custom)
        self.range_end.setEnabled(custom)

    def update_actions(self):
        active = bool(self.context.get("active"))
        self.start.setEnabled(bool(self.context.get("enabled") and self.context.get("schema_ready") and
                                   self.context.get("connected") and not active and not self.busy))
        self.stop.setEnabled(active and not self.busy)
        for widget in (self.case, self.robot, self.site, self.frequency, self.save_metrics, *self.presets):
            widget.setEnabled(not active)
        self.refresh.setEnabled(not self.busy)
        self.more.setEnabled(bool(self.cursor) and not self.busy)
        self.query.setEnabled(bool(self.records) and not self.busy)

    def call(self, method, path, done, body=None):
        if self.busy or getattr(self.window(), "_closing", False):
            return
        url, token = self.station()
        if not url or not token:
            self.status.setText("请通过 start-monitor.sh 启动上位机快捷后台；采集连接与长期记录在上位机操作。")
            return
        self.busy = True
        self._callback = done
        self._request_serial += 1
        request = (self._selection_generation, self._request_serial)
        self.update_actions()
        worker = RecordingWorker(url, token, method, "/api/v1/station/recordings"+path, body)
        def current():
            from shiboken6 import isValid

            return (isValid(self) and not getattr(self.window(), "_closing", False)
                    and request == (self._selection_generation, self._request_serial))

        worker.done.connect(lambda value: self.request_succeeded(value) if current() else None)
        worker.failed.connect(lambda message: self.request_failed(message) if current() else None)
        worker.finished.connect(lambda: _WORKERS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        _WORKERS.add(worker)
        worker.start()

    @Slot(object)
    def request_succeeded(self, value):
        callback, self._callback = self._callback, None
        self.busy = False
        self.update_actions()
        if callback:
            try:
                callback(value)
            except (KeyError, TypeError, ValueError):
                self.status.setText("监控记录响应格式无效，请更新电脑后台后重试。")

    @Slot(str)
    def request_failed(self, message):
        self.busy = False
        self._callback = None
        self.status.setText(message)
        self.update_actions()

    @staticmethod
    def selected(widget):
        return [widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count())
                if widget.item(i).checkState() == Qt.CheckState.Checked]

    def refresh_context(self, refresh_records=True):
        def apply(data):
            if self.robot_id and data.get("robot_id") != self.robot_id:
                raise ValueError("记录状态的机器人身份与所选机器人不一致")
            if self.robot_id is None:
                self.robot_id = data.get("robot_id")
            self.context = data
            self.update_robots(data)
            active = data.get("active")
            if active:
                saved = active["session"]
                for field, key in ((self.case, "case_number"), (self.robot, "robot_number"), (self.site, "site")):
                    if key in saved:
                        field.setText(saved[key])
                if "frequency_hz" in saved:
                    self.frequency.setValue(saved["frequency_hz"])
            self.number_preview()
            kind = data.get("source_kind")
            source = "模拟数据，非实机" if kind == "simulated" else "ROS2来源，仍待实机验收" if kind == "ros2" else "来源待连接"
            self.source.setText(f"实际采集身份：{data.get('robot_id') or '待连接'} · {source}")
            self.service_status.setText(data["history_database"]+f" · 待确认入库 {data['pending_samples']} 点\n"+
                                ("正在记录："+data["active"]["session"]["run_number"] if data["active"] else "没有运行中的监控记录")+
                                ("\n"+data["error"] if data["error"] else ""))
            self.update_actions()
            signature = [(m["key"], m["available"], m["label"], m["unit"]) for m in data["catalog"]]
            if signature != getattr(self, "_signature", None):
                self._signature = signature
                self._save_selections.update({self.save_metrics.item(i).data(Qt.ItemDataRole.UserRole):
                                             self.save_metrics.item(i).checkState() == Qt.CheckState.Checked
                                             for i in range(self.save_metrics.count())})
                self.save_metrics.clear()
                self.catalog = {}
                for entry in data["catalog"]:
                    self.catalog[entry["key"]] = entry
                    item = QListWidgetItem(entry["label"]+f" / {entry['unit']}"+(" · 未接入" if not entry["available"] else ""))
                    item.setData(Qt.ItemDataRole.UserRole, entry["key"])
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    default = entry["key"] in {"battery.soc", "battery.current"} or entry["key"].endswith(("temperature", ".torque", ".voltage"))
                    checked = self._save_selections.get(entry["key"], default)
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                    self.save_metrics.addItem(item)
                self.filter_metrics(self.search.text())
            if active:
                saved_metrics = active.get("query", {}).get("metrics", active["session"].get("metrics", []))
                for i in range(self.save_metrics.count()):
                    item = self.save_metrics.item(i)
                    item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in saved_metrics else Qt.CheckState.Unchecked)
            if refresh_records:
                self.load_records()
        path = "/context" + ("?"+urlencode({"robot_id": self.robot_id}) if self.robot_id else "")
        self.call("GET", path, apply)

    def load_records(self, more=False):
        previous = self.session.currentData()
        def apply(data):
            items = [r for r in data["items"] if not self.robot_id or r["robot_id"] == self.robot_id]
            if more:
                self.records += [r for r in items if r["id"] not in {old["id"] for old in self.records}]
                self._has_older_pages = True
            else:
                latest = {r["id"]: r for r in items}
                self.records = items + [r for r in self.records if r["id"] not in latest]
            if more or not self._has_older_pages:
                self.cursor = data["next_cursor"]
            self.session.blockSignals(True)
            self.session.clear()
            for robot in dict.fromkeys(r["robot_id"] for r in self.records):
                self.session.addItem(robot+" · 全部监控记录", (robot, None))
            for row in self.records:
                self.session.addItem(("[模拟] " if row["source_kind"] == "simulated" else "")+row["run_number"], (row["robot_id"], row["id"]))
            if previous:
                index = next((i for i in range(self.session.count())
                              if self.session.itemData(i) == previous), -1)
                if index >= 0:
                    self.session.setCurrentIndex(index)
            self.session.blockSignals(False)
            self.session_changed()
            self.update_actions()
            if not self.records:
                self.status.setText("暂无长期监控记录；连接机器人并选择指标后开始记录。")
        params = {"robot_id": self.robot_id} if self.robot_id else {}
        if more and self.cursor:
            params["before"] = self.cursor
        self.call("GET", "?"+urlencode(params) if params else "", apply)

    def session_changed(self):
        selected = self.session.currentData()
        if not selected:
            self.view_metrics.clear()
            self._view_key = None
            return
        if self._view_key is not None:
            self._view_selections[self._view_key] = set(self.selected(self.view_metrics))
        robot, identifier = selected
        keys = list(dict.fromkeys(k for r in self.records if r["robot_id"] == robot and (not identifier or r["id"] == identifier) for k in r["metrics"]))
        selection_key = (robot, identifier)
        old = self._view_selections.get(selection_key)
        self._view_key = selection_key
        self.view_metrics.clear()
        from .models import metric_spec
        for i, key in enumerate(keys):
            item = QListWidgetItem(metric_spec(key)["label"]+" / "+metric_spec(key)["unit"])
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = key in old if old is not None else i < 4
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.view_metrics.addItem(item)

    def filter_metrics(self, text):
        for i in range(self.save_metrics.count()):
            item = self.save_metrics.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    def preset(self, quantity):
        for i in range(self.save_metrics.count()):
            item = self.save_metrics.item(i)
            key = item.data(Qt.ItemDataRole.UserRole)
            if key.endswith(quantity) and (quantity != "voltage" or key.startswith("joint.")):
                item.setCheckState(Qt.CheckState.Checked)

    def start_recording(self):
        if not self.selected(self.save_metrics):
            self.status.setText("请至少勾选一个长期保存指标。")
            return
        if not self.context.get("robot_id") or not all(field.text().strip() for field in (self.case, self.robot, self.site)):
            self.status.setText("请连接机器人并填写用例编号、机器人编号和场地。")
            return
        body = dict(robot_id=self.context["robot_id"], case_number=self.case.text().strip(),
                    robot_number=self.robot.text().strip(), site=self.site.text().strip(),
                    frequency_hz=self.frequency.value(), metrics=self.selected(self.save_metrics))
        self.call("POST", "", self.mutated, body)

    def mutated(self, result):
        self.status.setText("监控记录已创建："+result["run_number"] if result.get("run_number") else "已结束本次记录；已保存的历史仍可查询。")
        self.refresh_context()

    def stop_recording(self):
        if self.robot_id and self.context.get("active"):
            self.call("POST", "/stop", self.mutated, {"robot_id": self.robot_id})

    def curves(self):
        selected = self.session.currentData()
        metrics = self.selected(self.view_metrics)
        if not selected or not 1 <= len(metrics) <= 12:
            self.status.setText("请选择监控记录与1至12条显示曲线")
            return
        robot, identifier = selected
        params = [("robot_id", robot), ("max_points", 800), *[("metrics", k) for k in metrics]]
        if identifier:
            params.append(("recording_id", identifier))
        seconds = self.period.currentData()
        if seconds == -1:
            a, b = self.range_start.dateTime().toSecsSinceEpoch(), self.range_end.dateTime().toSecsSinceEpoch()
            if a >= b:
                self.status.setText("开始时间必须早于结束时间")
                return
            params += [("start", datetime.fromtimestamp(a, timezone.utc).isoformat()), ("end", datetime.fromtimestamp(b, timezone.utc).isoformat())]
        elif seconds:
            now = datetime.now(timezone.utc)
            params += [("start", (now-timedelta(seconds=seconds)).isoformat()), ("end", now.isoformat())]
        self.call("GET", "/curves?"+urlencode(params), self.draw)

    def draw(self, data):
        while self.graph_layout.count():
            item = self.graph_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not data["count"]:
            self.status.setText("该区间没有已入库记录")
            return
        zone = ZoneInfo("Asia/Shanghai")
        start = datetime.fromisoformat(data["start"]).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.fromisoformat(data["end"]).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
        self.status.setText(f"完整区间（北京时间）：{start} 至 {end}\n原始 {data['count']} 点，显示 {len(data['points'])} 个时间桶；分桶均值及最小/最大范围，缺失保留断点。")
        groups = {}
        for metric in data["metrics"]:
            groups.setdefault(metric["unit"], []).append(metric)
        plotted = []
        for point in data["points"]:
            if point.get("gap_before"):
                plotted.append((point["time"], None))
            plotted.append((point["time"], point))
        stamps = np.array([datetime.fromisoformat(stamp).timestamp() for stamp, point in plotted])
        for unit, entries in groups.items():
            plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(utcOffset=-8 * 3600)})
            plot.setMinimumHeight(260)
            plot.showGrid(x=True, y=True, alpha=0.2)
            plot.setLabel("left", unit or "数值")
            plot.addLegend()
            for i, metric in enumerate(entries):
                values = [p["values"][metric["key"]] if p else dict(value=None, minimum=None, maximum=None)
                          for stamp, p in plotted]
                color = ("#4165a0", "#ac5a00", "#168064", "#9b3bb8")[i % 4]
                y = [v["value"] if v["value"] is not None else np.nan for v in values]
                plot.plot(stamps, y, name=metric["label"], pen=pg.mkPen(color, width=1.7), connect="finite")
                for field in ("minimum", "maximum"):
                    plot.plot(stamps, [v[field] if v["value"] is not None and v[field] is not None else np.nan for v in values], pen=pg.mkPen(color, width=0.6), connect="finite")
            self.graph_layout.addWidget(plot)
