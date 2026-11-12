"""Native robot-scoped diagnostic conversation; HTTP stays off the GUI thread."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

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
    QInputDialog,
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

from .board_check_view import board_check_view
from .capabilities import evaluate_capabilities
from .desktop_chat import CHAT_STYLE, ChatInput, ChatMessage, message_row, text_label
from .desktop_models import ModelDialog
from .factory_board_profile import BoardTestProfile, default_board_profile, factory_board_view
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
            if self.action == "bms_recovery":
                from .bms_recovery import request_recovery

                try:
                    self.done.emit(request_recovery(*self.body))
                except ValueError as error:
                    self.failed.emit(str(error))
                return
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
        self._disconnected = False
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
        top.setContentsMargins(15, 10, 15, 10)
        mark = text_label("RP", "assistantMark")
        mark.setFixedSize(34, 34)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(mark)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        titles.addWidget(text_label("诊断对话", "chatTitle"))
        titles.addWidget(text_label("查看当前机器人的观测与证据", "chatMuted"))
        top.addLayout(titles, 1)
        self.model_settings = QPushButton("AI 连接设置")
        self.model_settings.setObjectName("modelSettings")
        self.model_settings.setAccessibleName("AI 连接与模型设置")
        self.model_settings.setToolTip("配置中央服务、模型服务商、模型 ID 和 API Key")
        self.model_settings.setMinimumHeight(34)
        top.addWidget(self.model_settings)
        context_toggle = QPushButton("上下文")
        context_toggle.setObjectName("chatQuiet")
        context_toggle.setCheckable(True)
        context_toggle.setChecked(True)
        top.addWidget(context_toggle)
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
        self.thread.setContentsMargins(17, 13, 17, 13)
        self.thread.setSpacing(10)
        self.welcome = QWidget()
        welcome = QVBoxLayout(self.welcome)
        welcome.setContentsMargins(0, 8, 0, 6)
        welcome.setSpacing(7)
        welcome.addWidget(text_label("从当前问题开始", "contextTitle"))
        welcome.addWidget(
            text_label(
                "选择常见问题，或在下方输入需要排查的现象。",
                "welcomeCopy",
            )
        )
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
        context.setFixedWidth(250)
        context_toggle.toggled.connect(context.setVisible)
        context_layout = QVBoxLayout(context)
        context_layout.setContentsMargins(0, 0, 0, 0)
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_widget = QWidget()
        side = QVBoxLayout(side_widget)
        side.setContentsMargins(13, 12, 13, 12)
        side.setSpacing(8)
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
        self.model_settings.clicked.connect(self.models.open)
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
        if self._disconnected:
            self.status.setText("已收到采集服务更新；已有回答仍为历史查询快照")
            self._disconnected = False
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
        self._disconnected = True
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
    """Robot-scoped basic checks; refreshing never starts a test or service."""

    run_requested = Signal(object)
    report_ready = Signal(object)

    def __init__(self, source_url):
        super().__init__()
        from .factory_basic_checks import BASIC_CHECKS, BASIC_GROUPS, BASIC_NOTES
        from .robopi_tui import PASSIVE_PLUGINS

        self.source_url = source_url
        self.plugins = {
            identity: title
            for group in dict.fromkeys(BASIC_GROUPS.values())
            for identity, title in BASIC_CHECKS.items()
            if BASIC_GROUPS[identity] == group
        }
        self.groups = BASIC_GROUPS
        self.notes = BASIC_NOTES
        self.supported_checks = set(PASSIVE_PLUGINS)
        self.robot_id = None
        self.source_kind = None
        self.connected = False
        self.generation = 0
        self.busy = False
        self.enabled = False
        self.running = False
        self.reports = {}
        self.native_context = None
        self._native_bms_report_id = None
        self._pending_run = None
        self._cooldown_until = 0.0
        self.factory_profile = default_board_profile().model_dump(mode="json")
        self.profile_saver = None
        self.profile_dialog = None
        self.batch_queue = []
        self.batch_active = False
        self.batch_current = None
        self.batch_context = None
        self.round_reports = None
        self.round_previous_ids = {}
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
        layout.setContentsMargins(13, 10, 13, 10)
        layout.setSpacing(7)
        layout.addWidget(
            text_label(
                "按内置主控标准显示逐项结果；异常项给出原因和处理建议，原始证据可按需展开。",
                "welcomeCopy",
            )
        )
        self.summary = text_label("未检查 · 结果未知", "boardSummary")
        self.summary.setAccessibleName("各项硬件自检结果汇总")
        archive = QHBoxLayout()
        archive.addWidget(text_label("归档 SN", "contextTitle"))
        self.factory_sn = QLineEdit()
        self.factory_sn.setPlaceholderText("输入整机SN，关联已经完成的自检")
        self.factory_sn.setAccessibleName("硬件自检归档SN")
        archive.addWidget(self.factory_sn, 1)
        self.factory_sync = QPushButton("保存结果到此 SN")
        self.factory_sync.setObjectName("contextAction")
        archive.addWidget(self.factory_sync)
        self.profile_note = text_label(self.factory_profile["name"], "chatMuted")
        archive.addWidget(self.profile_note, 1)
        self.profile_button = QPushButton("测试标准")
        self.profile_button.setObjectName("contextAction")
        self.profile_button.clicked.connect(self.edit_board_profile)
        archive.addWidget(self.profile_button)
        layout.addLayout(archive)
        self.factory_save_note = text_label(
            "未关联工厂SN。已有结果可直接保存，无需重测；会保留各项原来的时间和结论。",
            "chatMuted",
        )
        layout.addWidget(self.factory_save_note)
        layout.addWidget(self.summary)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.select = QComboBox()
        self.select.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.select.setMinimumContentsLength(12)
        self.select.setAccessibleName("选择单项基础自检")
        for identity, title in self.plugins.items():
            self.select.addItem(f"{identity} · {title}" if identity.startswith("RP") else title, identity)
        actions.addWidget(self.select, 1)
        self.refresh = QPushButton("读取状态 / 历史报告")
        self.run = QPushButton("运行所选单项")
        for button, action in ((self.refresh, "board_status"), (self.run, "board_run")):
            button.setObjectName("contextAction")
            button.setMinimumHeight(33)
            button.clicked.connect(lambda checked=False, a=action: self.request(a))
            actions.addWidget(button)
        self.run_all = QPushButton("一键执行17项基础自检")
        self.run_all.setObjectName("contextAction")
        self.run_all.setMinimumHeight(33)
        self.run_all.clicked.connect(self.start_batch)
        actions.addWidget(self.run_all)
        layout.addLayout(actions)
        batch = QHBoxLayout()
        self.batch_status = text_label("17项＝原14项＋IMU连续数据、固件版本＋BMS重启恢复；IMU版本发送读取请求，BMS项会重启服务，结果保存到当前SN。", "chatMuted")
        batch.addWidget(self.batch_status, 1)
        self.stop_batch_button = QPushButton("停止后续检查")
        self.stop_batch_button.clicked.connect(lambda: self.stop_batch("已停止后续检查；已发起的单项完成后仍会保存结果。"))
        batch.addWidget(self.stop_batch_button)
        layout.addLayout(batch)
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
        self.table.setAccessibleName("按组展示的基础自检及最近结果")
        self.table.setHorizontalHeaderLabels(["分组", "检查项目", "测试结果", "结果说明", "完成时间"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in ((0, 82), (1, 205), (2, 85), (3, 280), (4, 130)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumWidth(0)
        self.table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        content.addWidget(self.table, 4)
        detail = QVBoxLayout()
        self.detail_title = text_label("所选检查详情", "contextTitle")
        detail.addWidget(self.detail_title)
        self.details = QPlainTextEdit()
        self.details.setObjectName("boardDetails")
        self.details.setReadOnly(True)
        self.details.setAccessibleName("所选检查的中文结果、实际读数及处理建议")
        self.details.setMinimumWidth(0)
        self.details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        detail.addWidget(self.details, 1)
        self.raw_toggle = QPushButton("展开原始证据（排查用）")
        self.raw_toggle.setCheckable(True)
        self.raw_toggle.setMinimumHeight(40)
        self.raw_toggle.toggled.connect(self._toggle_raw_evidence)
        detail.addWidget(self.raw_toggle)
        self.raw_details = QPlainTextEdit()
        self.raw_details.setReadOnly(True)
        self.raw_details.setAccessibleName("原始TUI报告，仅在展开后显示")
        self.raw_details.setMaximumHeight(190)
        self.raw_details.hide()
        self._raw_identity = None
        detail.addWidget(self.raw_details)
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
        self.select.currentIndexChanged.connect(self._update_controls)
        self.factory_sn.textChanged.connect(self._update_controls)
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
        if self.batch_active:
            self.stop_batch("连接或机器人身份已变化，本轮停止；重连不会自动重新执行。")
        identity_changed = bool(robot_id) and (robot_id != self.robot_id or source_kind not in (None, self.source_kind))
        if identity_changed:
            self._pending_run = None
            self.factory_save_note.setText("已切换机器人。新检查按上方SN归档；请核对SN，现有历史快照需手动关联。")
        elif not connected and self._pending_run:
            if self._pending_run.get("job_id"):
                self.factory_save_note.setText(self.factory_save_note.text() + "\n连接中断；本次归属保留，重连后读取状态，匹配任务后自动保存。")
            else:
                self._pending_run = None
                self.factory_save_note.setText("检查请求的响应未确认，未宣称已保存；重连后读取报告，再用上方SN保存已有结果，无需自动重测。")
        self.connected = connected
        self.enabled = False
        self.running = False
        self.poll.stop()
        if identity_changed:
            self.robot_id = robot_id
            self.source_kind = source_kind
            self.reports.clear()
            self._native_bms_report_id = None
            self.round_reports = None
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
            if (identity == "BMS_RECOVERY" and self._native_bms_report_id
                    and report.get("report_id") != self._native_bms_report_id):
                continue
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
            if self.round_reports is not None and report.get("report_id") and report["report_id"] != self.round_previous_ids.get(identity):
                self.round_reports[identity] = report
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
        from .factory_basic_checks import BASIC_CHECKS
        from .robopi_tui import PASSIVE_PLUGINS

        version = result.get("factory_basic_version")
        self.supported_checks = set(PASSIVE_PLUGINS)
        if type(version) is int and version >= 1:
            self.supported_checks.update(("IMU_CONTINUOUS", "BMS_RECOVERY"))
        if type(version) is int and version >= 2:
            self.supported_checks.add("IMU_FIRMWARE")
        job = result.get("job") or {}
        self.running = job.get("state") == "running"
        wait = max(0, float(result.get("cooldown_remaining_seconds", 0)))
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + wait)
        self.status.setText(
            "正在执行基础自检；完成后读取并保存结果"
            if self.running
            else job.get("error") or result.get("detail", "板端状态未知")
        )
        if result.get("upload_error"):
            self.status.setText(self.status.text() + "\n" + result["upload_error"])
        missing = [self.plugins[key] for key in self.plugins if key not in self.supported_checks]
        if self.enabled and missing:
            self.status.setText(self.status.text() + f"\n当前采集端支持{len(self.supported_checks)}/{len(BASIC_CHECKS)}项；需更新机器人observer才能执行：" + "、".join(missing) + "。")
        self._render_table()
        self._selection_changed()
        if self.running:
            self.poll.start()
        self._update_controls()

        pending = self._pending_run
        if pending and pending.get("job_id") and job.get("state") == "completed" and job.get("job_id") == pending["job_id"]:
            identity = pending["plugin_id"]
            report = self.reports.get(identity)
            if report and job.get("report_id") == report.get("report_id"):
                self._pending_run = None
                self.report_ready.emit({
                    "token": pending["token"], "robot_id": self.robot_id,
                    "source_kind": report["source_kind"],
                    "reports": {key: dict(value) for key, value in self.reports.items()
                                if value.get("report_id") == job["report_id"]},
                })
                if self.batch_active and self.batch_current == identity:
                    self.batch_current = None
        elif pending and job.get("job_id") == pending.get("job_id") and job.get("state") == "error":
            self._pending_run = None
            if self.batch_active:
                self.stop_batch("执行器未完成当前检查，本轮停止：" + str(job.get("error", "请核对采集端")))

    def shown_reports(self):
        return self.reports if self.round_reports is None else self.round_reports

    def _render_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.plugins))
        shown = self.shown_reports()
        for row, (identity, title) in enumerate(self.plugins.items()):
            report = shown.get(identity)
            view = factory_board_view(identity, report, self.factory_profile, shown)
            values = (
                self.groups.get(identity, "其他"),
                f"{identity} · {title}" if identity.startswith("RP") else title,
                view.label,
                view.summary,
                self._time(report["created_at"]).astimezone().strftime("%m-%d %H:%M:%S")
                if report
                else "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, identity)
                item.setToolTip(view.text() if column in (2, 3) else str(value))
                if column == 2:
                    item.setForeground(
                        QColor(
                            "#287568"
                            if view.label == "通过"
                            else "#a4334b"
                            if view.label == "不通过"
                            else "#8c601f"
                            if report
                            else "#61738e"
                        )
                    )
                self.table.setItem(row, column, item)
            self.table.setRowHeight(row, 37)
        self.table.blockSignals(False)
        views = [factory_board_view(key, shown.get(key), self.factory_profile, shown) for key in self.plugins]
        counts = {label: sum(view.label == label for view in views) for label in ("通过", "不通过", "未完成", "未检查")}
        simulated = self.source_kind == "simulated" or any(r["source_kind"] == "simulated" for r in shown.values())
        self.summary.setText(
            ("模拟数据 · 非实机 ｜ " if simulated else "本轮检查 ｜ " if self.round_reports is not None else "机器人检查快照 ｜ ")
            + f"通过 {counts['通过']}    不通过 {counts['不通过']}    未完成 {counts['未完成']}    未检查 {counts['未检查']}"
        )
        unchecked = [title for key, title in self.plugins.items() if not shown.get(key)]
        if 0 < len(unchecked) <= 3:
            self.summary.setText(self.summary.text() + "\n未检查项目：" + "、".join(unchecked))
        if self.enabled and len(self.supported_checks) < len(self.plugins):
            missing = [self.plugins[key] for key in self.plugins if key not in self.supported_checks]
            self.summary.setText(self.summary.text() + f"\n当前机器人采集端仅支持{len(self.supported_checks)}项；" + "、".join(missing) + "需更新observer后执行，TUI无需修改。")

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
        self.detail_title.setText(f"{identity} · {self.plugins[identity]}" if identity.startswith("RP") else self.plugins[identity])
        shown = self.shown_reports()
        report = shown.get(identity)
        if identity != self._raw_identity:
            self.raw_toggle.setChecked(False)
            self._raw_identity = identity
        self.raw_toggle.setVisible(bool(report))
        self.raw_details.setPlainText("")
        if not report:
            self.raw_toggle.setChecked(False)
            self.raw_details.hide()
            if self.enabled and identity not in self.supported_checks:
                self.details.setPlainText("测试结果：未检查\n当前机器人采集端不支持本项执行。\n\n处理方式：更新机器人observer后运行本项，TUI无需修改；已有结果保留。")
            else:
                self.details.setPlainText(
                    board_check_view(identity).text() + "\n\n当前结果未知，缺失不代表通过。"
                )
            return
        source = (
            "模拟数据 · 软件验证"
            if report["source_kind"] == "simulated"
            else "机器人采集端（ROS 2 来源）"
        )
        view = factory_board_view(identity, report, self.factory_profile, shown)
        self.details.setPlainText(
            view.text()
            + f"\n\n来源：{source}\n完成时间：{report['created_at']}"
            + "\n本项结果不代替其他功能或整机出厂结论。"
        )
        executor = f"工厂执行器：{report['executor_version']}" if report.get("report_kind") == "factory_check" else f"TUI 版本：{report.get('tui_version', '未知')}"
        raw_label = "读取成功，合格需对照标准" if identity == "IMU_FIRMWARE" and report["result"] == "pass" else RESULT_LABELS[report["result"]]
        self.raw_details.setPlainText(
            f"原始结论：{raw_label}\n{executor}\n"
            + report["message"] + "\n\n"
            + json.dumps(report.get("details", {}), ensure_ascii=False, indent=2)
        )

    def _toggle_raw_evidence(self, checked):
        self.raw_details.setVisible(checked)
        self.raw_toggle.setText("收起原始证据" if checked else "展开原始证据（排查用）")

    def set_board_profile(self, profile):
        self.factory_profile = BoardTestProfile.model_validate(profile).model_dump(mode="json")
        self.profile_note.setText(self.factory_profile["name"])
        self._render_table()
        self._selection_changed()

    def edit_board_profile(self):
        from .desktop_board_profile import BoardProfileDialog

        if self.profile_dialog:
            self.profile_dialog.raise_()
            return
        def save(values):
            if self.profile_saver is None:
                raise ValueError("本地工厂目录不可用，无法持久保存标准")
            self.set_board_profile(self.profile_saver(values))
        dialog = BoardProfileDialog(self.factory_profile, save, self)
        self.profile_dialog = dialog
        dialog.finished.connect(lambda: setattr(self, "profile_dialog", None))
        dialog.open()

    def start_batch(self):
        from .factory_basic_checks import BASIC_CHECKS
        from .factory_store import FactoryStore

        if not self.run_all.isEnabled():
            return
        try:
            FactoryStore.validate_sn(self.factory_sn.text().strip())
        except ValueError as exc:
            self.status.setText(f"SN无效，未开始检查：{exc}")
            return
        self.batch_context = (self.robot_id, self.source_kind, self.factory_sn.text().strip())
        self.round_previous_ids = {key: value.get("report_id") for key, value in self.reports.items()}
        self.round_reports = {}
        self.batch_queue = [identity for identity in BASIC_CHECKS if identity in self.supported_checks]
        self.batch_current = None
        self.batch_active = True
        self._render_table()
        self._update_controls()

    def stop_batch(self, message):
        self.batch_active = False
        self.batch_queue.clear()
        self.batch_status.setText(message)
        self._update_controls()

    def _advance_batch(self):
        if not self.batch_active or self.batch_current or self.busy or self.running or self.poll.isActive() or time.monotonic() < self._cooldown_until:
            return
        if (self.robot_id, self.source_kind, self.factory_sn.text().strip()) != self.batch_context:
            self.stop_batch("SN或机器人身份已变化，本轮停止；已有结果保留原归属。")
            return
        if not self.batch_queue:
            missing_names = [self.plugins[key] for key in self.plugins if key not in self.supported_checks]
            missing = "当前机器人采集端版本较旧，未执行：" + "、".join(missing_names) + "；需更新采集端。" if missing_names else ""
            self.stop_batch("基础自检已执行结束；各项结果及缺项见下表。" + missing + "灯板及电机测试另行完成。")
            return
        self.batch_current = self.batch_queue.pop(0)
        self.select.setCurrentIndex(self.select.findData(self.batch_current))
        self.batch_status.setText(f"本轮已取得 {len(self.round_reports)}/{len(self.plugins)} 项新结果 · 正在检查 {self.plugins[self.batch_current]} · SN：{self.batch_context[2]}")
        self.request("board_run", batch=True)

    def _update_controls(self):
        remaining = max(0, self._cooldown_until - time.monotonic())
        self.refresh.setEnabled(self.connected and not self.busy)
        self.profile_button.setEnabled(not self.busy and not self.running and not self.batch_active)
        self.factory_sn.setEnabled(not self.batch_active)
        self.select.setEnabled(not self.batch_active)
        self.stop_batch_button.setEnabled(self.batch_active)
        ready = self.connected and self.enabled and not self.busy and not self.running and not self.poll.isActive() and remaining <= 0
        self.run_all.setEnabled(ready and not self.batch_active and bool(self.factory_sn.text().strip()))
        missing_count = len(self.plugins) - len(self.supported_checks)
        self.run_all.setText(f"一键执行{len(self.supported_checks)}项（另{missing_count}项未支持）" if self.enabled and missing_count else f"一键执行{len(self.plugins)}项基础自检")
        self.run.setEnabled(
            self.connected
            and self.enabled
            and not self.busy
            and not self.running
            and not self.poll.isActive()
            and remaining <= 0
            and not self.batch_active
            and self.select.currentData() in self.supported_checks
        )
        self.run.setText(f"冷却 {int(remaining) + 1} 秒" if remaining > 0 else "运行所选单项")
        if remaining > 0 and not self.cooldown.isActive():
            self.cooldown.start()
        elif remaining <= 0:
            self.cooldown.stop()
        self._advance_batch()

    def request(self, action, *, batch=False):
        if self.busy or not self.robot_id or not self.connected:
            return
        if action == "board_run" and not self.run.isEnabled() and not (batch and self.batch_active):
            return
        if action == "board_run" and not batch:
            self.round_reports = None
            self._render_table()
            self._selection_changed()
        body = (
            {"robot_id": self.robot_id, "plugin_id": self.select.currentData()}
            if action == "board_run"
            else None
        )
        native_bms = (body and body["plugin_id"] == "BMS_RECOVERY" and self.source_kind == "ros2"
                      and self.native_context is not None)
        if native_bms:
            generation, sn = self.generation, self.factory_sn.text().strip()
            context = self.native_context()
            if (not context.get("connected") or context.get("robot_id") != self.robot_id
                    or not all(context.get(key) for key in ("relay_url", "station_url", "station_token_file"))):
                message = "请先在设备设置通过SSH连接本次BMS检查的机器人。"
                if batch:
                    self.stop_batch(message)
                self.status.setText(message)
                return
            password, ok = QInputDialog.getText(
                self, "授权 BMS 重启恢复测试",
                f"机器人 {self.robot_id} · SN {sn or '未填写'}\n请在维护窗口输入本次sudo密码。确认后重启 bms.service 并观察数据恢复。",
                QLineEdit.EchoMode.Password,
            )
            if (not ok or not password or self.generation != generation
                    or self.factory_sn.text().strip() != sn or self.native_context() != context
                    or not self.connected or self.busy or (batch and not self.batch_active)):
                message = "BMS重启授权未完成，本次未执行。"
                if batch and self.batch_active:
                    self.stop_batch(message)
                self.status.setText(message)
                return
            worker = OperationWorker(self.source_url(), "bms_recovery", body=(context, password))
        else:
            worker = OperationWorker(self.source_url(), action, body=body)
        if body:
            self._pending_run = {"token": str(uuid4()), "plugin_id": body["plugin_id"]}
            self.run_requested.emit({
                **self._pending_run, "robot_id": self.robot_id, "source_kind": self.source_kind,
            })
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
            if native_bms:
                pending, self._pending_run = self._pending_run, None
                head = result.head()
                self._native_bms_report_id = head["report_id"]
                self._merge_reports({"checks": [{"id": "BMS_RECOVERY", "latest": head}]})
                self._render_table()
                self._selection_changed()
                self.status.setText(result.message)
                if pending:
                    self.report_ready.emit({"token": pending["token"], "robot_id": self.robot_id,
                                            "source_kind": "ros2", "reports": {"BMS_RECOVERY": head}})
                if batch:
                    self.batch_current = None
                return
            if action == "board_run":
                if batch and not result.get("job_id"):
                    failed("开始响应缺少任务编号，执行状态未确认；请先读取现有任务状态。")
                    return
                if self._pending_run:
                    self._pending_run["job_id"] = result.get("job_id")
                self.running = True
                self._cooldown_until = time.monotonic() + 10
                self.status.setText("已请求基础自检，正在等待报告；不会自动重试执行")
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
                # Losing a GET response does not erase an already acknowledged
                # job's original SN ownership. A lost POST response is unknown.
                if action == "board_run" or not (self._pending_run or {}).get("job_id"):
                    self._pending_run = None
                self.enabled = False
                self.running = False
                self.poll.stop()
                if self.batch_active:
                    self.stop_batch("连接或执行请求未确认，本轮停止；重连后先读取状态，已有结果保留。")
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
