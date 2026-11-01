"""Desktop runner for a local simulated limit-control loop; never uses SSH."""

import json
import math
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


def _text(value):
    label = QLabel(value)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _validate_inputs(config_path, urdf_path):
    from .limit_execution import load_execution_plan

    return load_execution_plan(config_path, urdf_path)


def _finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class LimitExecutionPanel(QFrame):
    """Run the software control loop in a cancellable local child process."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config_path = None
        self.urdf_path = None
        self.output_dir = Path.home() / "Documents" / "RP1" / "limit-checks"
        self.process = None
        self._stdout = bytearray()
        self._abort_sent = False
        self._result_event = None
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(250)
        self.heartbeat_timer.timeout.connect(self._heartbeat)
        self.setObjectName("healthBanner")
        layout = QVBoxLayout(self)
        title = _text("单侧限位执行 · simulated 软件闭环")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addWidget(
            _text(
                "在这台电脑上模拟角度、负载反馈和低速接近、候选阻挡、回退流程。"
                "仅保存 simulated 结果，不连接 SSH、不发送电机指令。"
            )
        )
        files = QHBoxLayout()
        self.config_button = QPushButton("选择执行配置…")
        self.config_button.clicked.connect(self.choose_config)
        self.urdf_button = QPushButton("选择执行 URDF…")
        self.urdf_button.clicked.connect(self.choose_urdf)
        self.output_button = QPushButton("选择结果目录…")
        self.output_button.clicked.connect(self.choose_output)
        for button in (self.config_button, self.urdf_button, self.output_button):
            button.setObjectName("secondary")
            button.setMinimumHeight(40)
            files.addWidget(button)
        files.addStretch()
        layout.addLayout(files)
        self.paths_label = _text("")
        layout.addWidget(self.paths_label)
        actions = QHBoxLayout()
        self.validate_button = QPushButton("参数预检")
        self.validate_button.clicked.connect(self.validate)
        self.start_button = QPushButton("开始模拟闭环")
        self.start_button.clicked.connect(self.start)
        self.abort_button = QPushButton("立即中止模拟")
        self.abort_button.clicked.connect(self.abort)
        self.open_output_button = QPushButton("打开结果目录")
        self.open_output_button.clicked.connect(self.open_output)
        for button in (
            self.validate_button,
            self.start_button,
            self.abort_button,
            self.open_output_button,
        ):
            button.setMinimumHeight(40)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status_label = _text("等待选择配置和 URDF。执行参数不完整时不能启动。")
        self.status_label.setAccessibleName("模拟执行状态")
        layout.addWidget(self.status_label)
        self.torque_feedback_label = _text("simulated · 尚无本次模拟反馈。")
        layout.addWidget(self.torque_feedback_label)
        self.torque_table = QTableWidget(0, 2)
        self.torque_table.setHorizontalHeaderLabels(["关节", "估算力矩 (N·m)"])
        self.torque_table.setAccessibleName("simulated 估算力矩")
        self.torque_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.torque_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.torque_table.verticalHeader().setVisible(False)
        self.torque_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.torque_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.torque_table.setMinimumHeight(160)
        self.torque_table.setMaximumHeight(340)
        layout.addWidget(self.torque_table)
        layout.addWidget(_text("simulated · 仅显示单电机输出轴估算力矩，闭链关节力矩需另行解算。"))
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(300)
        self.events.setMinimumHeight(150)
        self.events.setAccessibleName("simulated 执行事件")
        layout.addWidget(self.events)
        layout.addWidget(
            _text(
                "中止按钮向本地模拟进程发送 abort 请求；进程退出不证明硬件已停止。"
                "面板仅保留最近300行，完整执行记录保存在上面的电脑目录。"
            )
        )
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.shutdown)
        self._selected()

    def _running(self):
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def _selected(self):
        self.paths_label.setText(
            f"配置：{self.config_path or '未选择'}\n"
            f"执行 URDF：{self.urdf_path or '未选择'}\n"
            f"结果保存在电脑：{self.output_dir}"
        )
        available = bool(self.config_path and self.urdf_path)
        running = self._running()
        for button in (self.config_button, self.urdf_button, self.output_button):
            button.setEnabled(not running)
        self.validate_button.setEnabled(available and not running)
        self.start_button.setEnabled(available and not running)
        self.abort_button.setEnabled(running and not self._abort_sent)

    def choose_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择单侧执行配置", "", "配置 (*.json)")
        if path:
            self.config_path = Path(path)
            self.status_label.setText("配置已更新，请重新预检。")
            self._selected()

    def choose_urdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择执行参考 URDF", "", "URDF (*.urdf *.xml)")
        if path:
            self.urdf_path = Path(path)
            self.status_label.setText("URDF 已更新，请重新预检。")
            self._selected()

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择电脑上的结果目录", str(self.output_dir))
        if path:
            self.output_dir = Path(path)
            self._selected()

    def validate(self):
        if self._running() or not self.config_path or not self.urdf_path:
            return False
        try:
            plan = _validate_inputs(self.config_path, self.urdf_path)
            if plan.get("source_kind") != "simulated":
                raise ValueError("此入口只允许 simulated 执行计划")
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            self.status_label.setText(f"参数预检失败：{error}\n未启动模拟。")
            return False
        self.status_label.setText(
            f"参数预检通过 · {plan['robot_dof']}关节配置 · {len(plan['tasks'])}个检查任务\n"
            "仅允许 simulated 软件闭环；没有启动机器人动作。"
        )
        return True

    def start(self):
        if self._running() or not self.validate():
            return
        process = QProcess(self)
        self.process = process
        self._stdout.clear()
        self._abort_sent = False
        self._result_event = None
        self.events.clear()
        self.torque_table.setRowCount(0)
        self.torque_feedback_label.setText("simulated · 等待本次模拟反馈，旧会话数据已清除。")
        environment = QProcessEnvironment.systemEnvironment()
        source = str(Path(__file__).resolve().parents[1])
        existing = environment.value("PYTHONPATH")
        environment.insert("PYTHONPATH", source + (":" + existing if existing else ""))
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.errorOccurred.connect(self._error)
        process.started.connect(self._started)
        process.finished.connect(self._finished)
        process.start(
            sys.executable,
            [
                "-m",
                "rp1_monitor.limit_execution",
                "--config",
                str(self.config_path),
                "--urdf",
                str(self.urdf_path),
                "--output",
                str(self.output_dir),
                "--simulate",
                "--require-heartbeat",
            ],
        )
        self.status_label.setText("simulated 模拟启动中；没有连接机器人。")
        self._selected()

    def _started(self):
        self.heartbeat_timer.start()
        self._heartbeat()
        self._selected()

    def _heartbeat(self):
        if self._running() and not self._abort_sent:
            if self.process.write(b"heartbeat\n") < 0:
                self.events.appendPlainText("模拟心跳写入失败，正在请求中止。")
                self.abort()

    def abort(self):
        if not self._running() or self._abort_sent:
            return
        self.heartbeat_timer.stop()
        if self.process.write(b"abort\n") < 0:
            self.status_label.setText("中止请求写入失败；模拟进程仍可能运行，请查看事件。")
            return
        self._abort_sent = True
        self.status_label.setText(
            "已请求中止 simulated 模拟，等待执行器返回结果。进程退出不证明硬件停止。"
        )
        self._selected()

    def shutdown(self):
        """Drain the local executor before Qt destroys its QProcess child.

        A window close handler can cancel closing when this returns False.
        No process signal is used as a substitute for its stop handshake.
        """
        self.heartbeat_timer.stop()
        process = self.process
        if process is None:
            return True
        self.abort()
        if self.process is None:
            return True
        if process.waitForFinished(2500):
            if self.process is process:
                self._finished(process.exitCode(), process.exitStatus())
            return True
        self.status_label.setText(
            "退出前未收到模拟进程结束确认，完整结果未确认；请等待或再次中止。"
            "没有强制结束进程，进程退出也不证明硬件停止。"
        )
        self.events.appendPlainText("关闭等待超时：完整结果未确认，模拟进程仍可能运行。")
        return False

    def _read_stdout(self):
        self._stdout.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self._stdout:
            raw, _, remaining = self._stdout.partition(b"\n")
            self._stdout = bytearray(remaining)
            self._event(raw)
        if len(self._stdout) > 1024 * 1024:
            self._stdout.clear()
            self.events.appendPlainText("模拟输出异常：单行超过1 MiB，已请求中止。")
            self.abort()

    def _event(self, raw):
        if not raw.strip():
            return
        if len(raw) > 1024 * 1024:
            self.events.appendPlainText("模拟输出异常：单行超过1 MiB，已请求中止。")
            self.abort()
            return
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("事件必须是 JSON 对象")
        except (ValueError, UnicodeError, RecursionError):
            self.events.appendPlainText(
                "模拟输出格式异常：" + raw[:500].decode("utf-8", errors="replace")
            )
            return
        if event.get("event") == "finished" and event.get("source_kind") == "simulated":
            self._result_event = event
        if event.get("event") == "feedback":
            if event.get("source_kind") == "simulated":
                self._update_torque_feedback(event.get("joints"))
            # Live values belong in the table; full per-frame metadata stays in result.json.
            return
        if event.get("event") == "baseline":
            if event.get("source_kind") == "simulated":
                self.events.appendPlainText("simulated · 保持负载采样完成。")
            return
        self.events.appendPlainText(json.dumps(event, ensure_ascii=False)[:10000])

    def _update_torque_feedback(self, joints):
        self.torque_table.setRowCount(0)
        if not isinstance(joints, dict):
            self.torque_feedback_label.setText("simulated · 当前事件没有有效的关节反馈。")
            return
        rows = [
            (name, feedback)
            for name, feedback in joints.items()
            if isinstance(name, str) and name.strip() and isinstance(feedback, dict)
        ][:28]
        self.torque_table.setRowCount(len(rows))
        for index, (name, feedback) in enumerate(rows):
            current = feedback.get("current_a")
            values = [name, "—"]
            estimate = feedback.get("torque_estimation")
            if isinstance(estimate, dict):
                torque = estimate.get("provisional_estimated_nm")
                coefficient = estimate.get("coefficient_nm_per_a")
                model = estimate.get("model_name")
                alias = estimate.get("model_alias")
                if (
                    _finite_number(current)
                    and _finite_number(torque)
                    and _finite_number(coefficient)
                    and coefficient > 0
                    and isinstance(model, str)
                    and bool(model.strip())
                    and isinstance(alias, str)
                    and bool(alias.strip())
                    and estimate.get("usable_for_contact_detection") is False
                    and estimate.get("basis_frame") == "motor_output"
                    and math.isclose(
                        torque, float(current) * float(coefficient), rel_tol=1e-6, abs_tol=1e-8
                    )
                ):
                    values[1] = f"{torque:.3f}"
            for column, value in enumerate(values):
                self.torque_table.setItem(index, column, QTableWidgetItem(value))
        self.torque_feedback_label.setText(
            f"simulated · 本次事件 {len(rows)} 轴，单电机输出轴力矩估算。"
            if rows
            else "simulated · 当前事件没有有效的关节反馈。"
        )

    def _read_stderr(self):
        raw = bytes(self.process.readAllStandardError())
        if raw:
            self.events.appendPlainText(
                "模拟诊断：" + raw[-4000:].decode("utf-8", errors="replace")
            )

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.heartbeat_timer.stop()
            self.status_label.setText("无法启动本地模拟进程：" + self.process.errorString())
            self.process.deleteLater()
            self.process = None
            self._selected()

    def _finished(self, exit_code, exit_status):
        self.heartbeat_timer.stop()
        self._read_stdout()
        self._read_stderr()
        if self._stdout:
            self._event(self._stdout)
            self._stdout.clear()
        files = (self._result_event or {}).get("output_files", [])
        report_confirmed = (
            isinstance(files, list)
            and bool(files)
            and all(isinstance(path, str) and Path(path).is_file() for path in files)
        )
        report_message = (
            "完整模拟结果已保存到电脑。"
            if report_confirmed
            else "完整结果未确认：缺少最终结果事件或报告文件，请检查模拟诊断。"
        )
        self.status_label.setText(
            f"simulated 模拟进程已退出（代码 {exit_code}）。结果目录：{self.output_dir}\n"
            f"{report_message}\n进程退出不证明硬件停止，也不代表真机标零通过。"
        )
        self.process.deleteLater()
        self.process = None
        self._selected()

    def open_output(self):
        if not self.output_dir.is_dir():
            self.status_label.setText("结果目录尚未创建，请先完成一次模拟或选择已有目录。")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir))):
            self.status_label.setText("无法打开结果目录：" + str(self.output_dir))
