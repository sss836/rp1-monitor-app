"""Explicit, bounded post-zero position queries, separate from writing motor zeros."""

import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .postzero import PostzeroSession, save_record


def _label(text=""):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _binding(context):
    return tuple(context.get(key) for key in (
        "robot_id", "target", "ssh_port", "known_hosts", "relay_url", "generation",
    ))


class PostzeroWorker(QThread):
    received = Signal(dict)
    failed = Signal(str)

    def __init__(self, connection, password, dof, session_id, parent=None, *, mode="query"):
        super().__init__(parent)
        self.connection, self.password = dict(connection), password
        self.dof, self.session_id = dof, session_id
        self.mode = mode
        self.cancel_event = threading.Event()
        self.capture_succeeded = False

    def run(self):
        from .postzero_transport import capture_postzero

        try:
            result = capture_postzero(
                self.connection, self.password, self.dof, self.session_id,
                self.received.emit, self.cancel_event, mode=self.mode,
            )
            self.capture_succeeded = result.get("completed") is True and result.get("cancelled") is False
        except Exception:
            # Neither authentication input nor remote stderr belongs in a GUI error.
            self.failed.emit("读取未完成。请核对密码、sudo权限、配置版本及电机供电，再重试。")
        finally:
            self.password = ""

    def stop(self):
        self.cancel_event.set()


class PostzeroDialog(QDialog):
    def __init__(self, calibration_context, connection_context, parent=None, *, embedded=False):
        super().__init__(parent)
        self.embedded = embedded
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.current_context, self.connection_context = calibration_context, connection_context
        initial = calibration_context()
        self.initial_identity = tuple(initial.get(k) for k in ("robot_id", "source_kind", "stream_id"))
        self.initial_configuration = initial.get("configuration_binding")
        self.initial_binding = _binding(connection_context())
        self.worker = self.session = None
        self._invalidated = False
        self._failure = ""
        self._dirty = False
        self.setWindowTitle("标零后角度复查")
        self.setObjectName("postzeroDialog")
        self.setStyleSheet("""
            QDialog#postzeroDialog { background:#eef2fa; color:#263451; }
            QLabel, QCheckBox { color:#263451; font-size:14px; background:transparent; }
            QLineEdit, QComboBox { background:white; color:#263451; padding:8px; border:1px solid #bdcce4; border-radius:10px; }
            QPushButton { min-height:28px; padding:8px 16px; border:1px solid #bdcce4; border-radius:10px; color:#4165a0; background:white; }
            QPushButton:hover { background:#e6edf8; }
            QPushButton:focus, QLineEdit:focus, QComboBox:focus { border:2px solid #4165a0; }
            QPushButton:disabled { background:#e3e9f2; color:#65758e; }
            QPushButton#primary { background:#4165a0; color:white; }
            QPushButton#primary:disabled { background:#d5dfed; color:#65758e; }
            QCheckBox { spacing:10px; }
            QCheckBox::indicator { width:19px; height:19px; background:white; border:1px solid #7e94b4; border-radius:5px; }
            QCheckBox::indicator:checked { background:#4165a0; image:url('__CHECK_ICON__'); }
            QTableWidget { background:white; alternate-background-color:#f2f5fb; color:#263451; border:1px solid #bdcce4; }
            QHeaderView::section { background:#e6edf8; color:#263451; padding:8px; border:0; }
        """.replace("__CHECK_ICON__", (Path(__file__).parent / "web/brand/check-light.svg").as_posix()))
        available = self.screen().availableGeometry()
        self.resize(min(980, available.width() - 48), min(820, available.height() - 64))
        self.setMinimumSize(600, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        self.heading = _label("标零后角度复查")
        self.heading.setStyleSheet("font-size:24px; font-weight:700;")
        layout.addWidget(self.heading)
        self.identity = _label(f"{initial.get('robot_id') or '尚未连接'} · {connection_context().get('target') or '尚未绑定SSH'}")
        layout.addWidget(self.identity)
        if embedded:
            self.heading.hide()
            self.identity.hide()
            layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label(
            "读取物理电机原始角度，踝部两台电机分别显示；本次采样保留为快照。"
            if embedded else
            "本窗口只观测角度，不会使能电机或自动运动到限位。"
            "已有运控使能后，可只监听24/28个物理电机的返回帧，无需 /joint_states。"
            "踝部按两台物理电机分别显示，不换算为URDF关节角度。"
        ))
        self.impact = _label()
        self.impact.setStyleSheet("background:#fff7e9; color:#78511a; padding:12px; border-radius:12px;")
        layout.addWidget(self.impact)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("已使能反馈 · 只监听返回帧", "passive")
        self.mode.addItem("独立位置查询 · 不使能电机", "query")
        self.mode.setAccessibleName("电机角度读取方式")
        form.addRow("读取方式", self.mode)
        self.dof = QComboBox()
        for text, value in (("请选择机型" if embedded else "请选择复查机型", None), ("RP1 · 24关节", 24), ("RP1 · 28关节", 28)):
            self.dof.addItem(text, value)
        configured = initial.get("configured_dof")
        if configured in (24, 28):
            self.dof.setCurrentIndex(self.dof.findData(configured))
            self.dof.setEnabled(False)
        self.dof.setAccessibleName("独立角度复查机型")
        form.addRow("机型" if embedded else "复查机型", self.dof)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("本次SSH和sudo密码")
        self.password.setPlaceholderText("只用于本次SSH和sudo认证，不保存")
        form.addRow("SSH / sudo密码", self.password)
        layout.addLayout(form)
        self.reference = QCheckBox("已重新上电，并按原标零工装或机械基准恢复标零姿态")
        self.reference.setAccessibleDescription("勾选后，以电机原始轴0度为机械参考，容差正负3度；未勾选仅采集角度。")
        layout.addWidget(self.reference)
        self.criteria = _label(
            "机械参考0° · 容差±3° · 每台3次反馈且波动不超过0.5°。"
            "未确认机械姿态时只显示读数。此项复查不代表机械限位或整机标定验收通过。"
        )
        layout.addWidget(self.criteria)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setAccessibleName("本次三轮读取进度")
        layout.addWidget(self.progress)
        self.status = _label("请选择机型并输入本次密码。没有角度反馈时保留缺失，不自动使能。")
        self.status.setAccessibleName("标零后复查结果和错误提示")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "电机 / 总线 / ID", "原始角度 / °", "机械参考 / °", "偏差 / °", "有效反馈 / 3", "结果", "使能状态",
        ])
        self.table.setAccessibleName("物理电机标零后角度复查记录")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.start = QPushButton("只读取角度")
        self.start.setObjectName("primary")
        self.start.clicked.connect(self.start_capture)
        self.stop = QPushButton("停止读取")
        self.stop.clicked.connect(self.stop_capture)
        self.save = QPushButton("保存本次复查…")
        self.save.clicked.connect(self.save_result)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        for button in (self.start, self.stop, self.save, self.close_button):
            actions.addWidget(button)
        if embedded:
            self.close_button.hide()
            self.save.setText("保存角度数据…")
            layout.insertLayout(layout.indexOf(self.table), actions)
        else:
            layout.addLayout(actions)
        self.dof.currentIndexChanged.connect(self.model_changed)
        self.mode.currentIndexChanged.connect(self.capture_mode_changed)
        self.password.textChanged.connect(self.refresh)
        self.reference.toggled.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.refresh()

    def launch_context(self, *, running=False, new_capture=False):
        status, connection = self.current_context(), self.connection_context()
        identity = tuple(status.get(k) for k in ("robot_id", "source_kind", "stream_id"))
        if not new_capture and (identity != self.initial_identity or _binding(connection) != self.initial_binding):
            raise ValueError("机器人、采集会话或SSH绑定已改变，请重新打开复查。")
        if not new_capture and status.get("configuration_binding") != self.initial_configuration:
            raise ValueError("机型配置已改变，请重新核对后打开复查。")
        # Launch needs a fresh preview; a running bounded query tolerates a short relay jitter.
        issues = status.get("issues", [])
        if running and status.get("elapsed_ms") is not None and 1000 < status["elapsed_ms"] <= 3000:
            issues = [issue for issue in issues if issue != "机器人预览帧本机接收年龄无效或已超过1000ms"]
        if issues or (not running and not status.get("can_preflight")):
            raise ValueError("；".join(issues) or "采集连接未确认。")
        if not status.get("connected") or not connection.get("connected"):
            raise ValueError("采集或SSH连接已断开。")
        if connection.get("robot_id") != status.get("robot_id") or status.get("source_kind") != "ros2":
            raise ValueError("需要绑定当前真实机器人，模拟来源不能执行角度查询。")
        if running and (status.get("elapsed_ms") is None or status["elapsed_ms"] > 3000):
            raise ValueError("采集连接超过3秒未更新，本次读取已中止。")
        dof = self.dof.currentData()
        if dof not in (24, 28):
            raise ValueError("请独立选择24或28关节机型。")
        if status.get("configured_dof") not in (None, dof) or status.get("health_dof") not in (None, dof):
            raise ValueError("所选机型与机器人配置冲突。")
        if not connection.get("known_hosts") or not Path(connection["known_hosts"]).is_file():
            raise ValueError("缺少已核对的SSH主机指纹记录，请先在设备设置连接。")
        return connection

    def model_changed(self):
        self.reference.setChecked(False)
        self.refresh()

    def capture_mode_changed(self):
        self.reference.setChecked(False)
        if self.session:
            self.session.invalidate("读取方式已更改，请重新采样。")
            self._failure = "读取方式已更改，请重新采样。"
            self.render_result()
        self.refresh()

    def refresh(self):
        running = self.worker is not None and self.worker.isRunning()
        passive = self.mode.currentData() == "passive"
        new_capture = self.embedded and self._invalidated and not running
        self.heading.setText("电机反馈观测" if passive else "标零后角度复查")
        self.setWindowTitle(self.heading.text())
        try:
            self.launch_context(running=running, new_capture=new_capture)
            ready, problem = not self._invalidated or new_capture, ""
        except ValueError as error:
            ready, problem = False, str(error)
        if not ready and running:
            self.invalidate(problem)
        if problem and self.session is None:
            self.status.setText("请先在设备设置连接机器人。" if self.embedded and not self.current_context().get("robot_id") else problem)
            self.status.setToolTip(problem)
        self.start.setText("开始监听已使能反馈" if passive else "读取并复查" if self.reference.isChecked() else "只读取角度")
        self.start.setEnabled(ready and not running and bool(self.password.text()))
        self.stop.setEnabled(running and not self.worker.cancel_event.is_set())
        self.password.setEnabled(not running)
        self.dof.setEnabled(not running and (new_capture or self.current_context().get("configured_dof") is None))
        self.mode.setEnabled(not running)
        self.reference.setEnabled(not running and not passive)
        self.reference.setVisible(not passive and not self.embedded)
        self.table.setColumnHidden(2, passive)
        self.table.setColumnHidden(3, passive)
        self.impact.setText(
            "点击监听后：通过SSH在机器人内存运行临时CAN接收程序，读取deploy配置；"
            "只接收已有返回帧，不发送查询、使能或运动指令，不启停现有运控。"
            "请由现场人员按原有流程使能；本窗口不会代为使能。代码不保存到机器人。"
            if passive else
            "点击读取后：通过SSH在机器人内存运行临时程序，每台电机发送3次低频CAN FD位置查询。"
            "不使能、不写零、不发送运动指令，不保存代码或启停服务；其他电机工具运行时拒绝查询。"
        )
        self.criteria.setText(
            "按现有deploy的Type1位置量程显示每台电机三轮采样的最后有效角度；本模式只作观测，不判定标零通过。"
            "电机尚未返回数据时显示缺失，不会补成0°。"
            if passive else
            "机械参考0° · 容差±3° · 每台3次反馈且波动不超过0.5°。"
            "未确认机械姿态时只显示读数。此项复查不代表机械限位或整机标定验收通过。"
        )
        if self.embedded:
            self.impact.setText(
                "只监听已有电机反馈，不发送查询、使能或运动指令。请按原有流程使能电机。" if passive else
                "每台发送3次低频位置查询，不使能或运动；请先退出其他电机写入工具。"
            )
            self.criteria.setText("每台采样3次，缺失显示未知；采样结束后显示快照，不是实时读数。")
        self.save.setEnabled(self.session is not None and not running)
        if self._dirty:
            self._dirty = False
            self.render_result()

    def start_capture(self):
        self.refresh()
        if not self.start.isEnabled():
            return
        if self.embedded and self._invalidated:
            # Rebinding is explicit at the next read, never on reconnect or source switch.
            initial = self.current_context()
            self.initial_identity = tuple(initial.get(k) for k in ("robot_id", "source_kind", "stream_id"))
            self.initial_configuration = initial.get("configuration_binding")
            self.initial_binding = _binding(self.connection_context())
            self._invalidated = False
        connection = self.launch_context()
        dof, session_id = self.dof.currentData(), str(uuid4())
        self.session = PostzeroSession(
            session_id, dof, connection["robot_id"],
            mechanical_reference=self.reference.isChecked(), trusted_capture=False,
            capture_mode=self.mode.currentData(),
        )
        self._failure = ""
        self.progress.setRange(0, dof * 3)
        self.progress.setValue(0)
        password = self.password.text()
        self.password.clear()
        self.worker = PostzeroWorker(connection, password, dof, session_id, self, mode=self.mode.currentData())
        self.worker.received.connect(self.on_event)
        self.worker.failed.connect(self.on_error)
        self.worker.finished.connect(self.capture_finished)
        self.status.setText("正在认证和监听已有反馈… 本窗口不会使能电机。" if self.mode.currentData() == "passive"
                            else "正在认证和读取… 本次最多3轮；缺失反馈不会填成0。")
        self.worker.start()
        self.refresh()

    def on_event(self, event):
        if self.session is None:
            return
        try:
            self.session.ingest(event)
        except ValueError:
            self.invalidate("收到不匹配或无效的读取记录，本次结果不可判定。")
        self._dirty = True
        if event.get("type") != "sample":
            self.render_result()

    def on_error(self, message):
        self._failure = message
        if self.session:
            self.session.invalidate(message)
        self.render_result()

    def stop_capture(self):
        if self.session:
            self.session.invalidate("操作人员停止读取，本次未形成完整复查。")
        if self.worker:
            self.worker.stop()
        self._failure = "已请求停止，等待关闭只读查询连接。"
        self.render_result()

    def invalidate(self, reason="连接已断开，本次复查失效。"):
        self._invalidated = True
        self.reference.setChecked(False)
        if self.session:
            self.session.invalidate(reason)
        if self.worker:
            self.worker.stop()
        self._failure = reason
        self.render_result()

    def capture_finished(self):
        if self.session and not self.session.report().get("completed"):
            self.session.invalidate("读取提前结束，未收到完整结束记录。")
        if self.session and self.worker and self.worker.capture_succeeded:
            try:
                self.launch_context(running=True)
            except ValueError as error:
                self.session.invalidate(str(error))
            else:
                # A complete JSON line alone is insufficient: SSH must also exit successfully.
                self.session.trusted_capture = True
        self.render_result()
        self.refresh()

    def render_result(self):
        if not self.session:
            return
        report = self.session.report()
        rows = report.get("rows", [])
        self.table.setRowCount(len(rows))
        self.progress.setValue(sum(e.get("type") == "sample" for e in report.get("events", [])))
        valid = sum(row.get("valid_sample_count", 0) for row in rows)
        expected = self.session.dof * 3
        passive = self.session.capture_mode == "passive"
        self.progress.setFormat(
            f"{'监听' if passive else '查询'}结束 · 有效反馈 {valid} / {expected} 次" if report.get("completed")
            else ("监听进度 %p%" if passive else "查询进度 %p%")
        )
        for index, row in enumerate(rows):
            def number(key):
                return "—" if row.get(key) is None else f"{row[key]:+.2f}"
            passed = row.get("pose_recheck_passed")
            outcome = "在容差内" if passed is True else row.get("reason") or "等待反馈"
            values = (
                f"{row['index']:02d} · {row.get('interface', '—')} / {row.get('motor_id', '—')}",
                number("angle_deg"), number("reference_deg"), number("deviation_deg"),
                str(row.get("valid_sample_count", 0)), outcome,
                "未知" if row.get("enabled") is None else "已使能" if row["enabled"] else "未使能",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(row.get("reason", ""))
                item.setForeground(QColor("#263451" if passed is True else "#835714"))
                self.table.setItem(index, column, item)
        if self._failure:
            self.status.setText(self._failure + " 记录可保存在电脑，不代表复查通过。")
        elif report.get("pose_recheck_passed") is True:
            self.status.setText("本次标零姿态复查通过：全部电机3次有效反馈均在±3°内且稳定。采样快照，不是实时读数。")
        elif report.get("completed") and valid == 0:
            self.status.setText(
                "读取失败：未取得有效电机角度，无法复查标零。查询结束不代表检查通过。"
                "请保存本次记录，以区分无应答、异常状态及协议或接收时间校验失败。"
            )
        elif report.get("completed") and passive:
            self.status.setText(
                f"被动监听结束：{valid} / {expected}次有效返回；显示最后有效角度和使能状态。"
                "本窗口未发CAN指令，当前记录只作角度观测。"
            )
        elif report.get("completed"):
            full = sum(row.get("valid_sample_count") == 3 for row in rows)
            self.status.setText(
                f"查询结束：{full} / {self.session.dof}台电机取得3次有效反馈；本次未形成复查通过结果。"
                + "；".join(report.get("issues", []))
                + (" 请查看各电机结果。" if self.session.mechanical_reference
                   else " 未确认机械姿态，本次只记录读数。")
            )
        else:
            self.status.setText("正在读取本次角度序列… " + "；".join(report.get("issues", [])))

    def save_result(self):
        if not self.session or (self.worker and self.worker.isRunning()):
            return
        session = self.session
        directory = Path.home() / "Documents" / "RP1" / "postzero-checks"
        filename = f"{session.robot_id}_{datetime.now():%Y%m%d-%H%M%S}_postzero.json"
        path, _ = QFileDialog.getSaveFileName(self, "将本次复查记录保存在电脑", str(directory / filename), "复查记录 (*.json)")
        if not path or self.session is not session:
            return
        try:
            save_record(path, session.report())
        except (OSError, ValueError) as error:
            self.status.setText("保存失败：" + str(error))
            return
        self.status.setText("已将本次角度、缺失项和比较结果保存在电脑：" + path)

    def shutdown(self):
        if self.worker and self.worker.isRunning():
            self.stop_capture()
            return self.worker.wait(1500)
        return True

    def reject(self):
        self.password.clear()
        if self.shutdown():
            if not self.embedded:
                self.timer.stop()
                super().reject()

    def closeEvent(self, event):
        if self.shutdown():
            self.password.clear()
            self.timer.stop()
            event.accept()
        else:
            event.ignore()
