"""Native robot-scoped diagnostic conversation; HTTP stays off the GUI thread."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .capabilities import evaluate_capabilities
from .desktop_chat import CHAT_STYLE, ChatInput, ChatMessage, message_row, text_label
from .desktop_models import ModelDialog
from .health_view import build_health
from .operations_client import validate_url
from .profiles import RobotProfile

_WORKERS = set()


class OperationWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, url, action, token="", body=None):
        super().__init__()
        self.url, self.action, self.token, self.body = url, action, token, body

    def run(self):
        try:
            local = self.action in {"doctor", "export", "board_status", "board_run"}
            connection = (
                self.body[1].get("connection", {})
                if self.action == "ask" and self.body
                else self.body
                if self.action == "probe"
                else {}
            )
            credential = self.token or (
                "model-key" if connection and connection.get("api_key") else ""
            )
            base = validate_url(self.url, credential, local=local)
            with httpx.Client(
                timeout=100,
                trust_env=False,
                follow_redirects=False,
                headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
            ) as client:
                if self.action == "models":
                    response = client.get(base + "/api/v1/assistant/models")
                elif self.action == "probe":
                    response = client.post(base + "/api/v1/assistant/test", json=self.body)
                elif self.action == "doctor":
                    response = client.get(base + "/api/v1/doctor")
                elif self.action == "board_status":
                    response = client.get(base + "/api/v1/board-diagnostics")
                elif self.action == "board_run":
                    response = client.post(base + "/api/v1/board-diagnostics/runs", json=self.body)
                elif self.action == "export":
                    response = client.post(base + "/api/v1/diagnostics")
                    response.raise_for_status()
                    saved = response.json()
                    bundle = client.get(base + f"/api/v1/diagnostics/{saved['report_id']}/bundle")
                    bundle.raise_for_status()
                    self.done.emit({"bundle": bundle.content, "saved": saved})
                    return
                else:
                    robot_id, query = self.body
                    response = client.post(
                        base + f"/api/v1/robot-monitors/{quote(robot_id, safe='')}/assistant",
                        json=query,
                    )
                response.raise_for_status()
                self.done.emit(response.json())
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            self.failed.emit(
                detail
                if isinstance(detail, str) and len(detail) < 300
                else "请求失败，请核对服务、凭据与模型配置。"
            )
        except (httpx.HTTPError, ValueError, OSError, KeyError):
            self.failed.emit(
                "请求失败：请核对连接、只读凭据、服务版本及模型配置。现有采集不受影响。"
            )
        finally:
            self.token = ""
            self.body = None


class OperationsPanel(QWidget):
    board_checks_requested = Signal()

    def __init__(self, source_url):
        super().__init__()
        self.source_url = source_url
        self.frame = {}
        self.busy = False
        self.history = []
        self.messages = []
        self._robot_id = None
        self._generation = 0
        self._catalog = None
        self._context_key = None
        self._temperature_metrics = None
        self.setObjectName("operations")
        self.setStyleSheet(CHAT_STYLE)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        conversation = QFrame()
        conversation.setObjectName("conversation")
        chat = QVBoxLayout(conversation)
        chat.setContentsMargins(0, 0, 0, 0)
        chat.setSpacing(0)
        header = QFrame()
        header.setObjectName("chatHeader")
        top = QHBoxLayout(header)
        top.setContentsMargins(22, 18, 22, 18)
        mark = text_label("RP", "assistantMark")
        mark.setFixedSize(42, 42)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(mark)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        titles.addWidget(text_label("诊断对话", "chatTitle"))
        titles.addWidget(text_label("每一次判断，都有据可查", "chatMuted"))
        top.addLayout(titles, 1)
        self.clear = QPushButton("新对话  +")
        self.clear.setObjectName("chatQuiet")
        self.clear.clicked.connect(self.new_conversation)
        top.addWidget(self.clear)
        chat.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(30)
        self._scroll_timer.timeout.connect(
            lambda: self.scroll.verticalScrollBar().setValue(
                self.scroll.verticalScrollBar().maximum()
            )
        )
        content = QWidget()
        self.thread = QVBoxLayout(content)
        self.thread.setContentsMargins(26, 24, 26, 24)
        self.thread.setSpacing(18)
        self.welcome = QWidget()
        welcome = QVBoxLayout(self.welcome)
        welcome.setContentsMargins(4, 22, 4, 18)
        welcome.setSpacing(13)
        welcome.addWidget(text_label("RP1 / DIAGNOSTIC ASSISTANT", "chatEyebrow"))
        welcome.addWidget(text_label("一起看懂机器人状态", "welcomeTitle"))
        welcome.addWidget(
            text_label(
                "从监测缺口到接入排查，把设备观测整理成清楚的结论。\n选择一个问题，或直接在下方开始对话。",
                "welcomeCopy",
            )
        )
        welcome.addSpacing(12)
        for title, question in (
            ("01   看看当前监测还缺什么    →", "为什么这台机器的健康监测不完整？"),
            ("02   帮我核对温度数据来源    →", "当前温度数据有哪些来源与限制？"),
            ("03   应该从哪里排查接入    →", "请按优先级说明接入自检中需要核对的项目。"),
        ):
            button = QPushButton(title)
            button.setObjectName("suggestion")
            button.clicked.connect(lambda checked=False, q=question: self.suggest(q))
            welcome.addWidget(button)
        self.thread.addWidget(self.welcome)
        self.thread.addStretch(1)
        self.scroll.setWidget(content)
        chat.addWidget(self.scroll, 1)

        bottom = QVBoxLayout()
        bottom.setContentsMargins(22, 0, 22, 14)
        bottom.setSpacing(8)
        composer = QFrame()
        composer.setObjectName("composer")
        compose = QVBoxLayout(composer)
        compose.setContentsMargins(12, 8, 10, 10)
        compose.setSpacing(2)
        self.question = ChatInput()
        self.question.setObjectName("chatInput")
        self.question.setPlaceholderText("询问当前机器人的状态、数据来源或接入问题…")
        self.question.setAccessibleName("诊断问题")
        self.question.setFixedHeight(62)
        self.question.submitted.connect(lambda: self.start("ask"))
        compose.addWidget(self.question)
        actions = QHBoxLayout()
        self.mode = text_label("本地规则 · 未调用模型", "chatMuted")
        actions.addWidget(self.mode, 1)
        self.ask = QPushButton("发送  ↑")
        self.ask.setObjectName("chatSend")
        self.ask.setAccessibleName("发送诊断问题")
        self.ask.clicked.connect(lambda: self.start("ask"))
        actions.addWidget(self.ask)
        compose.addLayout(actions)
        bottom.addWidget(composer)
        self.status = text_label("Enter 发送 · Shift + Enter 换行 · 只读诊断", "chatMuted")
        self.status.setAccessibleName("诊断请求状态")
        bottom.addWidget(self.status)
        chat.addLayout(bottom)
        root.addWidget(conversation, 1)

        context = QFrame()
        context.setObjectName("chatContext")
        context.setFixedWidth(262)
        context_layout = QVBoxLayout(context)
        context_layout.setContentsMargins(0, 0, 0, 0)
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_widget = QWidget()
        side = QVBoxLayout(side_widget)
        side.setContentsMargins(17, 20, 17, 18)
        side.setSpacing(12)
        side.addWidget(text_label("当前上下文", "contextTitle"))
        self.robot = text_label("等待连接", "contextRobot")
        side.addWidget(self.robot)
        self.source = text_label("来源待确认", "sourceChip")
        side.addWidget(self.source)
        self.profile = text_label("等待采集服务连接", "chatMuted")
        side.addWidget(self.profile)
        side.addSpacing(6)
        side.addWidget(text_label("监测能力", "contextTitle"))
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("contextTable")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().hide()
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnHidden(2, True)
        self.table.setFixedHeight(206)
        side.addWidget(self.table)
        side.addWidget(text_label("查询范围", "contextTitle"))
        self.metric_select = QComboBox()
        self.metric_select.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.metric_select.setMinimumContentsLength(8)
        self.metric_select.setAccessibleName("查询历史指标")
        self.metric_select.addItem("仅查询当前状态", "")
        self.metric_select.currentIndexChanged.connect(self.clear_temperature_context)
        side.addWidget(self.metric_select)
        self.minutes = QSpinBox()
        self.minutes.setAccessibleName("最近查询时间范围")
        self.minutes.setRange(1, 1440)
        self.minutes.setValue(10)
        self.minutes.setPrefix("最近 ")
        self.minutes.setSuffix(" 分钟")
        side.addWidget(self.minutes)
        self.doctor = QPushButton("接入自检    ↗")
        self.export = QPushButton("导出诊断证据    ↓")
        for button, action in ((self.doctor, "doctor"), (self.export, "export")):
            button.setObjectName("contextAction")
            button.clicked.connect(lambda checked=False, a=action: self.start(a))
            side.addWidget(button)
        self.board_panel = BoardDiagnosticsPanel(source_url)
        self.board_checks = QPushButton("硬件自检    ↗")
        self.board_checks.setObjectName("contextAction")
        self.board_checks.clicked.connect(self.board_checks_requested.emit)
        side.addWidget(self.board_checks)
        side.addWidget(text_label("在独立页面查看 CAN、BMS 和外设检查。", "chatMuted"))
        settings_toggle = QPushButton("连接与模型设置  ›")
        settings_toggle.setObjectName("chatQuiet")
        side.addWidget(settings_toggle)
        self.server = QLineEdit("http://127.0.0.1:8890")
        self.server.setAccessibleName("诊断中央服务地址")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("中央服务只读凭据 · 仅本窗口使用")
        self.token.setAccessibleName("中央服务只读凭据")
        self.models = ModelDialog(self, self.server, self.token)
        self.use_model = self.models.enabled
        self.use_model.toggled.connect(
            lambda enabled: self.mode.setText(
                "模型分析 · 发送所选证据" if enabled else "本地规则 · 未调用模型"
            )
        )
        settings_toggle.clicked.connect(self.models.open)
        self.models.test_requested.connect(lambda: self.start("probe"))
        self.models.catalog_requested.connect(lambda: self.start("models"))
        side.addStretch(1)
        side.addWidget(
            text_label("只读取观测，不执行机器人操作。\n模拟结果不代表实机验收。", "chatMuted")
        )
        side_scroll.setWidget(side_widget)
        context_layout.addWidget(side_scroll)
        root.addWidget(context)
        # Kept as an inspectable raw result for the existing automation interface.
        self.output = QPlainTextEdit(self)
        self.output.hide()

    def suggest(self, question):
        self.question.setPlainText(question)
        self.question.setFocus()

    def new_conversation(self):
        self._temperature_metrics = None
        self._generation += 1
        self.history.clear()
        self.output.clear()
        for row in self.messages:
            self.thread.removeWidget(row)
            row.deleteLater()
        self.messages.clear()
        self.welcome.show()
        self.question.clear()
        self.status.setText("新的对话 · 仅保留本窗口内当前机器人的上下文")

    def clear_temperature_context(self, *_):
        self._temperature_metrics = None

    def focus_temperature(self, joint, minutes=10):
        self.new_conversation()
        metric = f"joint.{joint}.motor_temperature"
        index = self.metric_select.findData(metric)
        if index < 0:
            self.metric_select.addItem(f"关节 {joint} · 电机温度", metric)
            index = self.metric_select.count() - 1
        self.metric_select.setCurrentIndex(index)
        self._temperature_metrics = [metric, f"joint.{joint}.mos_temperature"]
        self.minutes.setValue(minutes)
        self.question.setPlainText(
            f"请分析关节 {joint} 的电机及MOS温度、温升摘要和超温触发/恢复记录。按该机器人配置核对限值，区分事实与未知，不推断无证据的发热根因。"
        )
        self.status.setText("已带入电机及MOS两项证据；发送后查询，尚未调用模型。")
        self.question.setFocus()

    def add_message(self, role, text, meta, evidence=None):
        self.welcome.hide()
        message = ChatMessage(role, text, meta, evidence)
        row = message_row(message, role)
        self.thread.insertWidget(self.thread.count() - 1, row)
        self.messages.append(row)
        if len(self.messages) > 40:
            old = self.messages.pop(0)
            self.thread.removeWidget(old)
            old.deleteLater()
        self._scroll_timer.start()
        return row

    def set_frame(self, frame):
        frame = dict(frame)
        frame["health"] = build_health(
            frame.get("values", {}),
            frame.get("ages_ms", {}),
            frame.get("health_sources", {}),
            frame.get("health_sensors", {}),
            frame.get("joint_names", []),
        )
        if self._robot_id != frame["robot_id"]:
            self.new_conversation()
            self._robot_id = frame["robot_id"]
        self.board_panel.set_robot(
            self._robot_id,
            source_kind=frame.get("source_kind"),
            connected=frame.get("upload", {}).get("collector_connected", True),
        )
        self.frame = frame
        raw = frame.get("device_profile") or {"robot_id": frame["robot_id"]}
        profile = RobotProfile.model_validate(raw)
        self.robot.setText(profile.robot_id)
        self.source.setText(
            "模拟数据 · 软件验证"
            if frame.get("source_kind") == "simulated"
            else "ROS 2 观测 · 待实机核对"
        )
        revisions = [
            "未登记" if value == "unknown" else value
            for value in (
                profile.hardware_revision,
                profile.inference_revision,
                profile.driver_revision,
            )
        ]
        self.profile.setText(f"硬件 {revisions[0]}\n运控 {revisions[1]} · 驱动 {revisions[2]}")
        configuration = frame.get("monitoring_configuration")
        if configuration and configuration.get("source") == "database":
            configured = configuration["configuration"]
            self.profile.setText(
                f"中央配置 #{configuration['revision']} · {configured['dof']} 自由度\n登记硬件 {configured['hardware_revision']}\n采集档案硬件 {revisions[0]}\n运控 {revisions[1]} · 驱动 {revisions[2]}"
            )
        capabilities = evaluate_capabilities(frame, profile)
        key = json.dumps(capabilities, ensure_ascii=False)
        if key != self._context_key:
            self._context_key = key
            labels = {
                "observed": "已观测",
                "missing": "缺失/过期",
                "unsupported": "未提供",
                "error": "来源错误",
                "verified": "已核对",
                "unverified": "待核对",
                "stale": "已过期",
            }
            self.table.setRowCount(len(capabilities))
            for i, item in enumerate(capabilities):
                for j, value in enumerate(
                    (item["label"], labels.get(item["state"], item["state"]), item["reason"])
                ):
                    cell = QTableWidgetItem(value)
                    cell.setToolTip(item["reason"])
                    if j == 1:
                        cell.setForeground(QColor("#287b70" if item["ready"] else "#986525"))
                    self.table.setItem(i, j, cell)
                self.table.setRowHeight(i, 33)
        catalog = tuple((item["key"], item["label"]) for item in frame.get("catalog", []))
        if catalog != self._catalog:
            chosen = self.metric_select.currentData()
            self.metric_select.clear()
            self.metric_select.addItem("仅查询当前状态", "")
            for key, label in catalog:
                self.metric_select.addItem(label, key)
            self.metric_select.setCurrentIndex(max(0, self.metric_select.findData(chosen)))
            self._catalog = catalog

    def mark_disconnected(self):
        if self.frame:
            self._generation += 1
        self.frame = {}
        self.source.setText("连接中断 · 观测未知")
        self.profile.setText("能力状态未知，请重新连接采集服务")
        self.status.setText("连接中断；已有消息为历史查询快照")
        self.table.setRowCount(0)
        self._context_key = None
        self.board_panel.set_robot(None)

    def start(self, action):
        if self.busy:
            return
        if action not in {"models", "probe"} and not self.frame:
            self.status.setText("请先在设备设置中连接采集服务。")
            return
        question = self.question.toPlainText().strip()
        if action == "ask" and not 0 < len(question) <= 2000:
            self.status.setText("请输入 1–2000 字的问题。")
            return
        destination = None
        if action == "export":
            destination, _ = QFileDialog.getSaveFileName(
                self, "保存诊断证据包", "rp1-diagnostics.zip", "ZIP (*.zip)"
            )
            if not destination:
                return
        body = None
        if action == "ask":
            end = datetime.now(timezone.utc)
            body = (
                self.frame["robot_id"],
                {
                    "question": question,
                    "history": self.history[-8:],
                    "metrics": self._temperature_metrics
                    or (
                        [self.metric_select.currentData()]
                        if self.metric_select.currentData()
                        else []
                    ),
                    "start": (end - timedelta(minutes=self.minutes.value())).isoformat(),
                    "end": end.isoformat(),
                    "use_model": self.use_model.isChecked(),
                    "connection": self.models.connection() if self.use_model.isChecked() else None,
                },
            )
            self.question.clear()
        if action == "probe":
            body = self.models.connection()
        if action in {"models", "probe"}:
            self.models.status.setText(
                "正在读取服务配置…" if action == "models" else "正在测试接口与工具调用…"
            )
        prompt = (
            question
            if action == "ask"
            else "帮我做一次接入自检。"
            if action == "doctor"
            else "记录并导出当前诊断证据。"
        )
        now = datetime.now().strftime("%H:%M")
        pending = None
        if action not in {"models", "probe"}:
            self.add_message("user", prompt, f"你 · {now}")
            pending = self.add_message(
                "assistant", "正在读取观测与查询证据…", "RP1 诊断助手 · 查询中"
            )
        worker = OperationWorker(
            self.server.text() if action in {"ask", "models", "probe"} else self.source_url(),
            action,
            self.token.text() if action in {"ask", "models", "probe"} else "",
            body,
        )
        requested_robot, generation = self.frame.get("robot_id"), self._generation
        self.busy = True
        for button in (self.doctor, self.export, self.ask, self.models.test, self.models.refresh):
            button.setEnabled(False)
        self.status.setText("正在查询 · 后台采集与上传继续运行")

        def current():
            from shiboken6 import isValid

            return (
                isValid(self)
                and self._generation == generation
                and self.frame.get("robot_id") == requested_robot
            )

        def remove_pending():
            if pending in self.messages:
                self.messages.remove(pending)
                self.thread.removeWidget(pending)
                pending.deleteLater()

        def complete(result):
            if not current():
                return
            remove_pending()
            if action == "models":
                self.models.load_catalog(result)
                self.models.status.setText("服务端配置已刷新；可选择模型并测试连接。")
                self.status.setText("模型服务配置已刷新")
                return
            if action == "probe":
                self.models.status.setText(
                    f"连接成功 · {result['provider']} / {result['model']} · 工具调用已通过；未发送机器人数据"
                )
                self.status.setText("所选模型接口与工具调用验证通过")
                return
            if action == "export":
                try:
                    Path(destination).write_bytes(result["bundle"])
                    result = result["saved"]
                    answer, mode = (
                        f"诊断证据包已保存。\n{destination}\n包含来源、时间和 SHA256 校验信息。",
                        "证据归档",
                    )
                except OSError:
                    fail("写入失败；记录仍保存在采集服务，可重新导出。")
                    return
            elif action == "doctor":
                checks = result.get("checks", [])
                attention = [c for c in checks if c["result"] != "pass"]
                answer = f"已完成 {len(checks)} 项接入检查，其中 {len(attention)} 项需要核对。\n\n"
                answer += (
                    "\n\n".join(
                        f"{i + 1}. {c['detail']}\n建议：{c['next_step']}"
                        for i, c in enumerate(attention[:4])
                    )
                    or "当前检查项均有有效观测。"
                )
                if len(attention) > 4:
                    answer += f"\n\n其余 {len(attention) - 4} 项可在下方查询证据中查看。"
                answer += "\n\n自检反映本次接入情况，不能代替整机健康验收。"
                mode = "接入自检 · 只读"
            else:
                answer = result["answer"]
                mode = (
                    f"{result.get('provider', '模型')} / {result.get('model', '')} · 需复核"
                    if result["mode"] == "ai"
                    else "本地规则 · 未调用模型"
                )
                if body[1]["use_model"] and result["mode"] != "ai":
                    mode = "模型未启用 · 已返回本地规则"
                self.history.extend(
                    [
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": answer[:4000]},
                    ]
                )
                self.history = self.history[-8:]
            self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            self.add_message("assistant", answer, f"RP1 · {mode} · {now}", result)
            self.status.setText("本次查询快照 · 来源与时间见每条回答的查询证据")

        def fail(message):
            if not current():
                return
            remove_pending()
            if action in {"models", "probe"}:
                self.models.status.setText(message)
                self.status.setText(message)
                return
            self.add_message("assistant", message, "RP1 · 请求未完成")
            if action == "ask" and not self.question.toPlainText():
                self.question.setPlainText(question)
            self.status.setText("请求未完成 · 问题已保留，可检查设置后重新发送")

        worker.done.connect(complete)
        worker.failed.connect(fail)

        def finish():
            from shiboken6 import isValid

            _WORKERS.discard(worker)
            if not isValid(self):
                return
            if pending in self.messages:
                remove_pending()
            self.busy = False
            for button in (
                self.doctor,
                self.export,
                self.ask,
                self.models.test,
                self.models.refresh,
            ):
                button.setEnabled(True)

        worker.finished.connect(finish)
        _WORKERS.add(worker)
        worker.start()


class BoardDiagnosticsPanel(QWidget):
    """Robot-scoped, explicit TUI checks; refreshing never starts a plugin."""

    def __init__(self, source_url):
        super().__init__()
        from .robopi_tui import PASSIVE_PLUGINS, PLUGIN_GROUPS, PLUGIN_NOTES

        self.source_url = source_url
        self.plugins = {
            identity: title
            for group in dict.fromkeys(PLUGIN_GROUPS.values())
            for identity, title in PASSIVE_PLUGINS.items()
            if PLUGIN_GROUPS[identity] == group
        }
        self.groups = PLUGIN_GROUPS
        self.notes = PLUGIN_NOTES
        self.robot_id = None
        self.source_kind = None
        self.connected = False
        self.generation = 0
        self.busy = False
        self.enabled = False
        self.running = False
        self.reports = {}
        self._cooldown_until = 0.0
        self.setObjectName("boardPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            CHAT_STYLE
            + """
QWidget#boardPanel { background:#ffffff; border:1px solid #d4e2f5; border-radius:16px; }
QLabel#boardTitle { color:#192d4c; font-size:22px; font-weight:700; }
QLabel#boardSummary { background:#edf3ff; color:#35537e; border:1px solid #d8e4f6; border-radius:9px; padding:10px 12px; font-size:13px; }
QTableWidget#boardChecks { background:#ffffff; border:1px solid #dce5f2; border-radius:9px; color:#263e61; gridline-color:#e7edf6; font-size:12px; }
QTableWidget#boardChecks::item { padding:6px; }
QTableWidget#boardChecks::item:selected { background:#e4edff; color:#1f458e; }
QHeaderView::section { background:#f3f7fd; border:0; border-bottom:1px solid #dce5f2; padding:10px 6px; color:#536b8c; font-weight:600; }
QPlainTextEdit#boardDetails { background:#f8faff; border:1px solid #dce5f2; border-radius:9px; padding:10px; color:#304766; font-size:12px; }
QPushButton#contextAction:disabled { background:#f2f5fa; color:#7b8ba2; border-color:#dce5f2; }
QTableWidget#boardChecks:focus { border:1px solid #2b59e8; }
"""
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        layout.addWidget(text_label("ROBO PI / HARDWARE CHECKS", "chatEyebrow"))
        layout.addWidget(text_label("硬件自检", "boardTitle"))
        layout.addWidget(
            text_label(
                "直接调用机器人已安装的 RoboPi TUI 被动插件。只在点击运行后执行单项检查；读取报告不会自动运行。",
                "welcomeCopy",
            )
        )
        self.summary = text_label("未检查 · 结果未知", "boardSummary")
        self.summary.setAccessibleName("各项硬件自检结果汇总")
        layout.addWidget(self.summary)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.select = QComboBox()
        self.select.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.select.setMinimumContentsLength(12)
        self.select.setAccessibleName("选择单项 RoboPi TUI 被动检查")
        for identity, title in self.plugins.items():
            self.select.addItem(f"{identity} · {title}", identity)
        actions.addWidget(self.select, 1)
        self.refresh = QPushButton("读取状态 / 历史报告")
        self.run = QPushButton("运行所选单项")
        for button, action in ((self.refresh, "board_status"), (self.run, "board_run")):
            button.setObjectName("contextAction")
            button.setMinimumHeight(40)
            button.clicked.connect(lambda checked=False, a=action: self.request(a))
            actions.addWidget(button)
        layout.addLayout(actions)
        self.selection_note = text_label("", "chatMuted")
        self.selection_note.setAccessibleName("所选检查说明与限制")
        layout.addWidget(self.selection_note)
        self.status = text_label("连接后读取板端检查状态", "chatMuted")
        self.status.setAccessibleName("板端检查状态")
        layout.addWidget(self.status)
        content = QHBoxLayout()
        content.setSpacing(16)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("boardChecks")
        self.table.setAccessibleName("按组展示的十四项硬件检查及最近结果")
        self.table.setHorizontalHeaderLabels(["分组", "检查项目", "最近结论", "完成时间", "来源"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in ((0, 82), (1, 220), (2, 120), (3, 140), (4, 116)):
            self.table.setColumnWidth(column, width)
        self.table.setMinimumWidth(0)
        self.table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        content.addWidget(self.table, 4)
        detail = QVBoxLayout()
        self.detail_title = text_label("所选检查详情", "contextTitle")
        detail.addWidget(self.detail_title)
        self.details = QPlainTextEdit()
        self.details.setObjectName("boardDetails")
        self.details.setReadOnly(True)
        self.details.setAccessibleName("所选检查的来源、完成时间及原始证据")
        self.details.setMinimumWidth(0)
        self.details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        detail.addWidget(self.details, 1)
        content.addLayout(detail, 2)
        layout.addLayout(content, 1)
        layout.addWidget(
            text_label(
                "RP040 · CPU 调频：仅原 TUI 开放，当前上位机未接入。原插件包含修改操作，不在可执行项目中。",
                "chatMuted",
            )
        )
        layout.addWidget(
            text_label(
                "结果为对应时间的检查快照，缺失保持未知；ROS 2 来源不等于实机验收。RP001 是主控板温度，不是电机温度。",
                "chatMuted",
            )
        )
        # Preserve the raw-result inspection interface used by existing automation.
        self.output = QPlainTextEdit(self)
        self.output.setReadOnly(True)
        self.output.hide()
        self.poll = QTimer(self)
        self.poll.setSingleShot(True)
        self.poll.setInterval(1500)
        self.poll.timeout.connect(lambda: self.request("board_status"))
        self.cooldown = QTimer(self)
        self.cooldown.setInterval(250)
        self.cooldown.timeout.connect(self._update_controls)
        self.select.currentIndexChanged.connect(self._selection_changed)
        self.table.currentCellChanged.connect(self._table_selected)
        self._render_table()
        self._selection_changed()
        self._update_controls()

    def set_robot(self, robot_id, *, source_kind=None, connected=True):
        connected = bool(robot_id) and bool(connected)
        if (
            robot_id == self.robot_id
            and connected == self.connected
            and source_kind in (None, self.source_kind)
        ):
            return
        self.generation += 1
        self.connected = connected
        self.enabled = False
        self.running = False
        self.poll.stop()
        if robot_id and (robot_id != self.robot_id or source_kind not in (None, self.source_kind)):
            self.robot_id = robot_id
            self.source_kind = source_kind
            self.reports.clear()
            self.output.clear()
            self._cooldown_until = 0.0
            self._render_table()
            self._selection_changed()
        self.status.setText(
            f"{robot_id} · 点击读取状态，检查仅由本机手动发起"
            if self.connected
            else "连接中断；已有结果为历史快照，当前状态未知，不能运行检查"
        )
        self._update_controls()

    @staticmethod
    def _time(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("检查时间必须包含时区")
        return parsed

    def _merge_reports(self, result):
        """Merge per-item results, including old collector responses, without erasing peers."""
        from .robopi_tui import RESULT_LABELS

        incoming = []
        for check in result.get("checks", []):
            if check.get("id") in self.plugins and check.get("latest"):
                incoming.append((check["id"], dict(check["latest"])))
        latest = result.get("latest")
        if latest:
            if latest.get("robot_id") != self.robot_id:
                raise ValueError("历史报告身份与当前机器人不一致")
            if latest.get("source_kind") not in {"ros2", "simulated"} or self.source_kind not in (
                None,
                latest.get("source_kind"),
            ):
                raise ValueError("历史报告来源与当前机器人不一致")
            raw = latest["board_diagnostics"]
            # New collectors supply authoritative per-check heads. Their global
            # latest report is raw evidence, not a replacement for those heads.
            for row in raw["results"] if "checks" not in result else []:
                if row["id"] in self.plugins:
                    incoming.append(
                        (
                            row["id"],
                            {
                                "report_id": latest.get("report_id"),
                                "robot_id": latest["robot_id"],
                                "created_at": latest["created_at"],
                                "source_kind": latest["source_kind"],
                                "validation_scope": latest.get("validation_scope"),
                                "tui_version": raw["tui_version"],
                                "result": row["result"],
                                "message": row["message"],
                                "details": row.get("details", {}),
                            },
                        )
                    )
        validated = []
        for identity, report in incoming:
            if (
                report.get("result") not in RESULT_LABELS
                or report.get("robot_id", self.robot_id) != self.robot_id
                or report.get("source_kind") not in {"ros2", "simulated"}
                or self.source_kind not in (None, report.get("source_kind"))
                or not isinstance(report.get("message"), str)
                or not isinstance(report.get("details", {}), dict)
            ):
                raise ValueError("检查结果或来源无效")
            self._time(report["created_at"])
            # Requests are serialized and generation-guarded. Robot wall time
            # can move backwards; it must not hide a newly observed failure.
            validated.append((identity, report))
        for identity, report in validated:
            self.reports[identity] = report
        if latest:
            source = (
                "模拟数据 · 软件验证"
                if latest["source_kind"] == "simulated"
                else "ROS 2 板端历史报告 · 非实机验收"
            )
            self.output.setPlainText(
                source
                + "\n"
                + "\n".join(
                    f"{row['id']} · {RESULT_LABELS[row['result']]}\n{row['message']}"
                    for row in latest["board_diagnostics"]["results"]
                )
                + "\n\n完整原始报告：\n"
                + json.dumps(latest, ensure_ascii=False, indent=2)
            )
        elif self.reports:
            self.output.setPlainText(json.dumps(self.reports, ensure_ascii=False, indent=2))
        else:
            self.output.setPlainText("暂无板端自检报告；不会将缺失结果视为通过。")

    def apply_status(self, result):
        if result.get("robot_id") != self.robot_id:
            raise ValueError("板端身份与当前机器人不一致")
        if result.get("source_kind") is not None and self.source_kind not in (
            None,
            result["source_kind"],
        ):
            raise ValueError("板端来源与当前机器人不一致")
        self._merge_reports(result)
        self.enabled = bool(result.get("enabled")) and self.connected
        job = result.get("job") or {}
        self.running = job.get("state") == "running"
        wait = max(0, float(result.get("cooldown_remaining_seconds", 0)))
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + wait)
        self.status.setText(
            "正在执行指定被动检查；采集继续，完成后只读取报告"
            if self.running
            else job.get("error") or result.get("detail", "板端状态未知")
        )
        if result.get("upload_error"):
            self.status.setText(self.status.text() + "\n" + result["upload_error"])
        self._render_table()
        self._selection_changed()
        if self.running:
            self.poll.start()
        self._update_controls()

    def _render_table(self):
        from .robopi_tui import RESULT_LABELS

        self.table.blockSignals(True)
        self.table.setRowCount(len(self.plugins))
        for row, (identity, title) in enumerate(self.plugins.items()):
            report = self.reports.get(identity)
            result = report["result"] if report else None
            source = (
                "模拟数据 · 示例"
                if report and report["source_kind"] == "simulated"
                else "ROS 2 · 待验收"
                if report
                else "未知"
            )
            values = (
                self.groups.get(identity, "其他"),
                f"{identity} · {title}",
                RESULT_LABELS.get(result, "未检查 · 未知"),
                self._time(report["created_at"]).astimezone().strftime("%m-%d %H:%M:%S")
                if report
                else "—",
                source,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, identity)
                item.setToolTip(str(value) if column != 3 or not report else report["created_at"])
                if column == 2:
                    item.setForeground(
                        QColor(
                            "#287568"
                            if result == "pass"
                            else "#a4334b"
                            if result in {"fail", "error"}
                            else "#8c601f"
                            if report
                            else "#61738e"
                        )
                    )
                self.table.setItem(row, column, item)
            self.table.setRowHeight(row, 37)
        self.table.blockSignals(False)
        counts = {
            key: sum(r["result"] == key for r in self.reports.values())
            for key in ("pass", "fail", "error", "needs_review", "skip")
        }
        unknown = len(self.plugins) - len(self.reports)
        simulated = any(r["source_kind"] == "simulated" for r in self.reports.values())
        self.summary.setText(
            ("模拟数据 · 软件验证 ｜ " if simulated else "检查快照 ｜ ")
            + f"通过 {counts['pass']}    不符合 {counts['fail']}    错误 {counts['error']}    待复核 {counts['needs_review']}    跳过 {counts['skip']}    未检查 {unknown}"
        )

    def _table_selected(self, row, column, *_):
        if row >= 0:
            identity = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            self.select.setCurrentIndex(self.select.findData(identity))

    def _selection_changed(self, *_):
        from .robopi_tui import RESULT_LABELS

        identity = self.select.currentData()
        row = list(self.plugins).index(identity)
        if self.table.currentRow() != row:
            self.table.setCurrentCell(row, 1)
        note = self.notes.get(identity, "仅按需运行所选被动检查，不自动重试。")
        self.selection_note.setText(note)
        self.detail_title.setText(f"{identity} · {self.plugins[identity]}")
        report = self.reports.get(identity)
        if not report:
            self.details.setPlainText(
                "未检查 · 结果未知\n\n连接采集端并读取状态后，可手动运行所选单项。缺失结果不代表检查通过。"
            )
            return
        source = (
            "模拟数据 · 软件验证"
            if report["source_kind"] == "simulated"
            else "ROS 2 板端观测 · 待实机验收"
        )
        self.details.setPlainText(
            f"{RESULT_LABELS[report['result']]}\n{source}\n完成时间：{report['created_at']}\nTUI 版本：{report.get('tui_version', '未知')}\n\n"
            + report["message"]
            + "\n\n原始证据：\n"
            + json.dumps(report.get("details", {}), ensure_ascii=False, indent=2)
        )

    def _update_controls(self):
        remaining = max(0, self._cooldown_until - time.monotonic())
        self.refresh.setEnabled(self.connected and not self.busy)
        self.run.setEnabled(
            self.connected
            and self.enabled
            and not self.busy
            and not self.running
            and not self.poll.isActive()
            and remaining <= 0
        )
        self.run.setText(f"冷却 {int(remaining) + 1} 秒" if remaining > 0 else "运行所选单项")
        if remaining > 0 and not self.cooldown.isActive():
            self.cooldown.start()
        elif remaining <= 0:
            self.cooldown.stop()

    def request(self, action):
        if self.busy or not self.robot_id or not self.connected:
            return
        if action == "board_run" and not self.run.isEnabled():
            return
        body = (
            {"robot_id": self.robot_id, "plugin_id": self.select.currentData()}
            if action == "board_run"
            else None
        )
        worker = OperationWorker(self.source_url(), action, body=body)
        generation = self.generation
        self.busy = True
        self._update_controls()
        self.status.setText("正在请求板端检查…" if body else "正在读取板端状态…")

        def current():
            from shiboken6 import isValid

            return isValid(self) and self.generation == generation

        def complete(result):
            if not current():
                return
            if action == "board_run":
                self.running = True
                self._cooldown_until = time.monotonic() + 10
                self.status.setText("已请求指定被动检查，正在等待报告；不会自动重试执行")
                self.poll.start()
            else:
                try:
                    self.apply_status(result)
                except (ValueError, KeyError, TypeError, AttributeError):
                    self.enabled = False
                    self.status.setText(
                        "板端报告无效，请核对采集版本和机器人身份；已有历史结果保留"
                    )

        def failed(message):
            if current():
                self.enabled = False
                self.running = False
                self.status.setText(
                    message + "\n已有历史结果保留；可读取状态重试，不会自动重新执行检查。"
                )

        def finished():
            from shiboken6 import isValid

            _WORKERS.discard(worker)
            if isValid(self):
                self.busy = False
                self._update_controls()
            worker.deleteLater()

        worker.done.connect(complete)
        worker.failed.connect(failed)
        worker.finished.connect(finished)
        _WORKERS.add(worker)
        worker.start()
