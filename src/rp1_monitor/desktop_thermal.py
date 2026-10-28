"""Robot-scoped temperature history and persisted alarm evidence for the Qt monitor."""

from datetime import datetime
from urllib.parse import quote

import httpx
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .operations_client import validate_url

_WORKERS = set()


class TemperatureWorker(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, url, token, robot_id, joint, minutes):
        super().__init__()
        self.url, self.token, self.robot_id, self.joint, self.minutes = (
            url,
            token,
            robot_id,
            joint,
            minutes,
        )

    def run(self):
        try:
            base = validate_url(self.url, self.token)
            with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
                result = client.get(
                    base
                    + f"/api/v1/robot-monitors/{quote(self.robot_id, safe='')}/joints/{self.joint}/temperature",
                    params={"minutes": self.minutes},
                    headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                )
                result.raise_for_status()
                self.done.emit(result.json())
        except (httpx.HTTPError, ValueError):
            self.failed.emit("温度证据查询失败，请在诊断设置中核对中央地址与只读凭据。")
        finally:
            self.token = ""


def plain_label(text=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


class TemperatureDialog(QDialog):
    diagnose = Signal(int, int)

    def __init__(self, parent, endpoint, credential, current_robot):
        super().__init__(parent)
        self.endpoint, self.credential, self.current_robot = endpoint, credential, current_robot
        self.robot_id = None
        self.generation = 0
        self.setWindowTitle("关节温度 · 曲线与告警")
        self.setObjectName("temperatureDialog")
        self.setStyleSheet(
            "QDialog#temperatureDialog { background:#f7faff; }"
            "QPlainTextEdit { background:#ffffff; border:1px solid #d4e2f5; "
            "border-radius:6px; padding:8px; }"
        )
        self.resize(860, 680)
        self.setMinimumSize(620, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        self.heading = plain_label("选择关节查看原始摘要与告警证据")
        self.heading.setStyleSheet("font-size:18px; font-weight:700; color:#142441;")
        layout.addWidget(self.heading)
        controls = QHBoxLayout()
        self.joints = QComboBox()
        self.joints.setAccessibleName("温度详情关节")
        controls.addWidget(self.joints, 1)
        self.minutes = QComboBox()
        self.minutes.setAccessibleName("温度历史时间范围")
        for title, minutes in (("最近10分钟", 10), ("最近1小时", 60), ("最近24小时", 1440)):
            self.minutes.addItem(title, minutes)
        controls.addWidget(self.minutes)
        self.refresh = QPushButton("刷新证据")
        controls.addWidget(self.refresh)
        layout.addLayout(controls)
        self.status = plain_label()
        layout.addWidget(self.status)
        self.observation = plain_label()
        layout.addWidget(self.observation)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.plot.setLabel("left", "温度", units="°C")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend()
        self.curves = [
            self.plot.plot(name=name, pen=pg.mkPen(color, width=2))
            for name, color in (("电机最后值", "#ae283a"), ("MOS最后值", "#2b59e8"))
        ]
        layout.addWidget(self.plot, 1)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setAccessibleName("关节温度告警触发与恢复时间")
        self.events.setMaximumHeight(165)
        layout.addWidget(self.events)
        layout.addWidget(
            plain_label(
                "时间来自持久告警确认事件；观测丢失不代表恢复，开启历时不等于连续超温。曲线为摘要最后值，缺失处保留断点。"
            )
        )
        buttons = QHBoxLayout()
        self.to_agent = QPushButton("带入诊断对话")
        self.to_agent.setEnabled(False)
        self.to_agent.clicked.connect(self.open_diagnosis)
        buttons.addWidget(self.to_agent)
        buttons.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.joints.currentIndexChanged.connect(self.load)
        self.minutes.currentIndexChanged.connect(self.load)
        self.refresh.clicked.connect(self.load)
        self.finished.connect(self.invalidate)

    def invalidate(self, *_):
        self.generation += 1

    def open_joint(self, robot_id, rows, joint):
        self.invalidate()
        self.robot_id = robot_id
        self.joints.blockSignals(True)
        self.joints.clear()
        for row in rows:
            self.joints.addItem(f"{row['joint']} · {row['label']}", row["joint"])
        self.joints.setCurrentIndex(max(0, self.joints.findData(joint)))
        self.joints.blockSignals(False)
        self.show()
        self.raise_()
        self.load()

    def load(self, *_):
        if not self.isVisible() or not self.robot_id or self.joints.currentData() is None:
            return
        self.generation += 1
        generation, robot, joint = self.generation, self.robot_id, self.joints.currentData()
        self.to_agent.setEnabled(False)
        self.observation.clear()
        self.events.clear()
        for curve in self.curves:
            curve.clear()
        self.heading.setText(f"{robot} · {self.joints.currentText()}")
        if len(_WORKERS) >= 3:
            self.status.setText("已有温度查询正在完成，请稍后刷新。")
            return
        self.status.setText("正在读取当前配置、曲线与告警记录…")
        worker = TemperatureWorker(
            self.endpoint(), self.credential(), robot, joint, self.minutes.currentData()
        )
        _WORKERS.add(worker)

        def current():
            from shiboken6 import isValid

            return (
                isValid(self)
                and self.isVisible()
                and generation == self.generation
                and self.current_robot() == robot
            )

        def completed(data):
            if current() and data.get("robot_id") == robot and data.get("joint") == joint:
                self.render(data)

        def failed(message):
            if current():
                self.status.setText(message)

        worker.done.connect(completed)
        worker.failed.connect(failed)
        worker.finished.connect(lambda: _WORKERS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def render(self, data):
        row = data["current"]
        source = (
            "模拟数据 · 非实机" if data["source_kind"] == "simulated" else "ROS2观测 · 待实机验收"
        )
        self.status.setText(
            f"{source} · 配置版本 {data.get('configuration_revision') or '本机文件/未配置'} · 查询于 {data['as_of_at']}"
        )
        self.observation.setText(row["detail"])
        for curve, payload in zip(self.curves, data["curves"]):
            points = []
            for point in payload.get("points", []):
                stamp = datetime.fromisoformat(point["time"]).timestamp()
                if point.get("missing_windows") or point.get("gap_count"):
                    points.append((stamp - 0.001, np.nan))
                points.append((stamp, np.nan if point.get("last") is None else point["last"]))
                if point.get("missing_windows") or point.get("gap_count"):
                    points.append((stamp + 0.001, np.nan))
            if points:
                values = np.asarray(points)
                curve.setData(values[:, 0], values[:, 1], connect="finite")
        lines = []
        for head in data["alerts"]["heads"]:
            lines.append(
                f"{head['rule_key']} · {head['state']}{'' if head['evaluation_fresh'] else '（评估过期）'} · 持续观测开始 {head.get('condition_since') or '未知'} · 确认触发 {head.get('opened_at') or '未触发'}"
            )
        labels = {
            "opened": "超温触发",
            "recovered": "恢复确认",
            "observation_lost": "观测丢失",
            "observation_restored": "观测恢复",
        }
        lines += [
            f"{event['occurred_at']} · {labels.get(event['event_kind'], event['event_kind'])} · {event['rule_key']}"
            for event in data["alerts"]["events"]
        ]
        self.events.setPlainText("\n".join(lines) or "当前关节尚无已持久化温度告警记录。")
        self.to_agent.setEnabled(True)

    def open_diagnosis(self):
        self.diagnose.emit(self.joints.currentData(), self.minutes.currentData())
        self.close()
