"""SOC observations from the existing preview; never polls or controls the robot."""

import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .models import metric_max_age_ms

MAX_SOC_CAPTURES = 64


def _number(value):
    return type(value) in (float, int) and math.isfinite(value)


def _label(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class BatterySwapDialog(QDialog):
    """Capture the baseline and SOC changes, leaving the conclusion to the operator."""

    def __init__(self, sn, store, parent=None):
        super().__init__(parent)
        self.sn, self.store = sn, store
        self.session_id = str(uuid4())
        self.observation_ids = []
        self.bound_robot_id = self.source_kind = None
        self.frame = None
        self.received_monotonic = None
        self.received_at = None
        self.last_sample = None
        self.last_stream = self.last_stamp = None
        self.last_uptime = None
        self.watching = False
        self.interrupted = False
        self.reboot_observed = False
        self.error = False
        self.setWindowTitle("热换电 · SOC监听与截图")
        self.setMinimumWidth(620)
        self.setObjectName("batterySwapDialog")
        self.setStyleSheet("QDialog#batterySwapDialog {background:#eef2fa;color:#142441;} QLabel {color:#142441;} QWidget#socEvidenceCard {background:white;border:1px solid #c8d8ef;border-radius:8px;}")
        layout = QVBoxLayout(self)
        layout.addWidget(_label("接好网线，点击“开始监听SOC”，再实际更换电池。自动保存起始电量和每次SOC变化的截图；不发送电源或BMS命令。"))
        self.card = QWidget()
        self.card.setObjectName("socEvidenceCard")
        card_layout = QVBoxLayout(self.card)
        self.identity_label = _label(f"SN：{sn} · 等待机器人数据")
        self.soc_label = _label("SOC：—")
        self.soc_label.setStyleSheet("font-size:28px;font-weight:700;")
        self.values_label = _label("电压：—　电流：—　主控开机时长：—")
        self.time_label = _label("采集时间：—\n电脑接收时间：—")
        self.continuity_label = _label("连续性：尚未开始")
        for widget in (self.identity_label, self.soc_label, self.values_label, self.time_label, self.continuity_label):
            card_layout.addWidget(widget)
        card_layout.addWidget(_label("SOC变化是观测证据；电量自然下降也会变化，不能单独证明已换电或主控全程不断电。"))
        layout.addWidget(self.card)
        self.notice = _label("等待实时SOC。截图与数值按当前SN追加保存，关闭窗口仍保留已保存证据。")
        self.notice.setAccessibleName("SOC监听状态")
        layout.addWidget(self.notice)
        row = QHBoxLayout()
        self.start_button = QPushButton("开始换电 / 监听SOC")
        self.start_button.setMinimumHeight(44)
        self.start_button.clicked.connect(self.start)
        row.addWidget(self.start_button)
        self.finish_button = QPushButton("结束监听并记录结果")
        self.finish_button.setMinimumHeight(44)
        self.finish_button.setEnabled(False)
        self.finish_button.clicked.connect(self.finish)
        row.addWidget(self.finish_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_stale)
        self.timer.start(250)

    def set_frame(self, frame, *, received_monotonic=None):
        if not isinstance(frame, dict):
            self.mark_disconnected()
            return
        identity = (frame.get("robot_id"), frame.get("source_kind"))
        if not identity[0] or identity[1] not in {"ros2", "simulated"}:
            self.mark_disconnected()
            return
        if self.bound_robot_id and identity != (self.bound_robot_id, self.source_kind):
            self.watching = False
            self.frame = None
            self.interrupted = True
            self.notice.setText("机器人身份或来源已变化，已停止监听；已有截图仍归原机器人。请关闭后重新核对SN。")
            self.start_button.setEnabled(False)
            return
        if frame.get("upload", {}).get("collector_connected") is False:
            self.mark_disconnected()
            return
        try:
            stamp = datetime.fromisoformat(frame["sampled_at"])
            if stamp.tzinfo is None:
                raise ValueError("missing timezone")
            stream = str(frame["stream_id"])
        except (ValueError, KeyError, TypeError):
            self.mark_disconnected()
            return
        if stream == self.last_stream and self.last_stamp is not None and stamp <= self.last_stamp:
            return
        if self.last_stream is not None and stream != self.last_stream:
            self.interrupted = True
        if self.received_monotonic is not None and time.monotonic() - self.received_monotonic > 2:
            self.interrupted = True
        self.last_stream, self.last_stamp = stream, stamp
        self.frame = frame
        now = time.monotonic()
        self.received_monotonic = now if received_monotonic is None else received_monotonic
        self.received_at = (datetime.now(timezone.utc) - timedelta(seconds=max(0, now - self.received_monotonic))).isoformat()
        sample = self.current_sample()
        if sample is None:
            self.show_unknown("SOC缺失、数值无效或已过期；等待新鲜数据。")
            return
        self.last_uptime = sample["uptime_seconds"]
        self.show_sample(sample)
        if self.watching:
            self.capture(sample)

    def current_sample(self):
        if self.frame is None or self.received_monotonic is None:
            return None
        elapsed = time.monotonic() - self.received_monotonic
        if elapsed > 2:
            return None
        values, ages = self.frame.get("values", {}), self.frame.get("ages_ms", {})
        def current(key):
            value, age = values.get(key), ages.get(key)
            if (_number(value) and _number(age) and 0 <= age <= metric_max_age_ms(key)
                    and 0 <= age + elapsed * 1000 <= metric_max_age_ms(key)):
                return value
            return None
        soc = current("battery.soc")
        if soc is None or not 0 <= soc <= 100:
            return None
        uptime = current("host.uptime.seconds")
        if uptime is not None and uptime < 0:
            uptime = None
        if uptime is not None and self.last_uptime is not None and uptime < self.last_uptime:
            self.reboot_observed = True
        continuity = ("观察到主控开机时长回退，存在重启证据" if self.reboot_observed else
                      "曾断流或采集流切换，连续供电未证实" if self.interrupted else
                      "当前预览持续接收；不代表已证明全程不断电")
        return dict(session_id=self.session_id, robot_id=self.frame["robot_id"], source_kind=self.frame["source_kind"],
                    event="baseline" if not self.observation_ids else "change", sampled_at=self.frame["sampled_at"],
                    received_at=self.received_at, soc=soc,
                    previous_soc=self.last_sample["soc"] if self.last_sample else None,
                    voltage=current("battery.voltage"), current=current("battery.current"), uptime_seconds=uptime,
                    soc_age_ms=ages["battery.soc"] + elapsed * 1000, continuity=continuity)

    def show_sample(self, sample):
        source = "SIMULATED · 模拟数据 · 非实机" if sample["source_kind"] == "simulated" else "ROS2观测数据"
        self.identity_label.setText(f"SN：{self.sn}\n机器人：{sample['robot_id']} · {source}")
        before = self.last_sample["soc"] if self.last_sample else None
        self.soc_label.setText((f"SOC：{before:g}% → {sample['soc']:g}%" if before is not None else f"SOC：{sample['soc']:g}%"))
        def show(value, unit):
            return "—" if value is None else f"{value:g}{unit}"
        self.values_label.setText(f"电压：{show(sample['voltage'], 'V')}　电流：{show(sample['current'], 'A')}\n主控开机时长：{show(sample['uptime_seconds'], 's')}")
        self.time_label.setText(f"采集时间：{sample['sampled_at']}\n电脑接收时间：{sample['received_at']}\nSOC观测年龄：{sample['soc_age_ms']:.0f}ms")
        self.continuity_label.setText(sample["continuity"])

    def show_unknown(self, message):
        self.soc_label.setText("SOC：—（当前不可用）")
        self.values_label.setText("电压：—　电流：—　主控开机时长：—")
        self.continuity_label.setText("数据连续性未知；已有截图保留")
        self.notice.setText(message)
        if self.watching:
            self.interrupted = True

    def mark_disconnected(self):
        self.frame = None
        if self.watching:
            self.interrupted = True
        self.show_unknown("连接中断，暂不截图；恢复后继续记录SOC变化。")

    def check_stale(self):
        if self.watching and self.current_sample() is None:
            self.show_unknown("未收到新鲜SOC，暂不截图；等待恢复。")

    def start(self):
        if self.observation_ids or self.watching:
            return
        self.watching = True
        self.interrupted = self.reboot_observed = False
        self.last_uptime = None
        self.start_button.setEnabled(False)
        self.finish_button.setEnabled(True)
        self.notice.setText("监听中：等待新鲜SOC作为起始值；请保持网线连接。")
        sample = self.current_sample()
        if sample is not None:
            self.capture(sample)

    def capture(self, sample, *, final=False):
        if self.error:
            return
        if not final and self.last_sample and sample["soc"] == self.last_sample["soc"]:
            return
        if len(self.observation_ids) >= MAX_SOC_CAPTURES:
            self.watching = False
            self.notice.setText("本轮已保存64张关键截图，已停止截图。已有证据保留，可结束并记录结果。")
            return
        if self.bound_robot_id is None:
            self.bound_robot_id, self.source_kind = sample["robot_id"], sample["source_kind"]
        sample = {**sample, "event": "final" if final else sample["event"]}
        self.show_sample(sample)
        directory = Path(self.store.directory) / "function-soc" / self.session_id
        path = directory / f"{len(self.observation_ids):03}-{uuid4().hex}.png"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.card.layout().activate()
            if not self.card.grab().save(str(path), "PNG"):
                raise OSError("无法生成SOC截图")
            row = self.store.save_soc_observation(self.sn, sample, path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.error = True
            self.watching = False
            self.notice.setText(f"截图或证据保存失败：{exc}。已有证据保留；请关闭后重试。")
            self.finish_button.setEnabled(True)
            return
        self.observation_ids.append(row["observation_id"])
        self.last_sample = sample
        self.last_uptime = sample["uptime_seconds"]
        self.finish_button.setEnabled(True)
        self.notice.setText(f"监听中 · 已保存{len(self.observation_ids)}张截图；起始SOC及每次变化均已关联SN：{self.sn}。")

    def finish(self):
        sample = self.current_sample()
        if sample is not None and self.observation_ids and not self.error:
            self.capture(sample, final=True)
        self.watching = False
        self.accept()

    def done(self, result):
        self.watching = False
        self.timer.stop()
        super().done(result)
