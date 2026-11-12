"""SOC observations from the existing preview; never polls or controls the robot."""

import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .factory_store import BATTERY_SWAP_STAGES
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
        self.checked_identity = None
        self.target_error = ""
        self.frame = None
        self.received_monotonic = None
        self.received_at = None
        self.last_sample = None
        self.last_stream = self.last_stamp = None
        self.last_uptime = None
        self.pending_stage = None
        self.stage_readings = {}
        self.backup_active = False
        self.watching = False
        self.interrupted = False
        self.reboot_observed = False
        self.error = ""
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
        self.stage_label = _label("")
        self.stage_label.hide()
        for widget in (self.identity_label, self.soc_label, self.values_label, self.time_label, self.continuity_label, self.stage_label):
            card_layout.addWidget(widget)
        card_layout.addWidget(_label("SOC变化是观测证据；电量自然下降也会变化，不能单独证明已换电或主控全程不断电。"))
        layout.addWidget(self.card)
        self.notice = _label("等待实时SOC。截图与数值按当前SN追加保存，关闭窗口仍保留已保存证据。")
        self.notice.setAccessibleName("SOC监听状态")
        layout.addWidget(self.notice)
        layout.addWidget(_label("第一块和第二块主电池：标记后等待新SOC，显示读数时点击保存。小电池：点击一次记录阶段，无SOC。"))
        stages = QHBoxLayout()
        self.stage_buttons = {}
        for stage, label in BATTERY_SWAP_STAGES.items():
            button = QPushButton(f"标记{label}" if stage != "backup" else "标记小电池（无SOC）")
            button.setEnabled(False)
            button.clicked.connect(lambda _checked=False, key=stage: self.mark_stage(key))
            stages.addWidget(button)
            self.stage_buttons[stage] = button
        layout.addLayout(stages)
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

    def validate_target(self, identity, *, recheck=False):
        if not recheck and identity == self.checked_identity:
            return not self.target_error
        self.checked_identity = identity
        was_blocked = bool(self.target_error)
        try:
            self.store.validate_soc_target(self.sn, *identity)
        except (ValueError, sqlite3.Error) as exc:
            self.target_error = f"无法开始监听：{exc} 请关闭窗口，核对当前机器人的整机SN后重试。"
            self.frame = None
            self.interrupted |= self.watching
            self.show_unknown(self.target_error)
            self.watching = False
            self.update_stage_buttons()
            self.start_button.setEnabled(False)
            self.finish_button.setEnabled(False)
            return False
        self.target_error = ""
        if was_blocked:
            self.start_button.setEnabled(True)
            self.notice.setText("机器人与当前SN已核对，可开始监听SOC。")
        return True

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
            self.update_stage_buttons()
            self.frame = None
            self.interrupted = True
            self.notice.setText("机器人身份或来源已变化，已停止监听；已有截图仍归原机器人。请关闭后重新核对SN。")
            self.start_button.setEnabled(False)
            return
        if frame.get("upload", {}).get("collector_connected") is False:
            self.mark_disconnected()
            return
        if not self.validate_target(identity):
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
        sample = self.current_sample(backup=self.backup_active)
        if sample is None:
            self.show_unknown("SOC缺失、数值无效或已过期；等待新鲜数据。")
            return
        self.show_sample(sample)
        if self.watching and not self.backup_active:
            self.capture(sample)
            if self.pending_stage:
                self.update_stage_buttons()

    def current_sample(self, *, backup=False):
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
        uptime = current("host.uptime.seconds")
        if uptime is not None and uptime < 0:
            uptime = None
        if uptime is not None and self.last_uptime is not None and uptime < self.last_uptime:
            self.reboot_observed = True
        if uptime is not None:
            self.last_uptime = uptime
        soc = None if backup else current("battery.soc")
        if not backup and (soc is None or not 0 <= soc <= 100):
            return None
        continuity = ("观察到主控开机时长回退，存在重启证据" if self.reboot_observed else
                      "曾断流或采集流切换，连续供电未证实" if self.interrupted else
                      "当前预览持续接收；不代表已证明全程不断电")
        sample = dict(session_id=self.session_id, robot_id=self.frame["robot_id"], source_kind=self.frame["source_kind"],
                    event="baseline" if self.last_sample is None else "change", sampled_at=self.frame["sampled_at"],
                    received_at=self.received_at, soc=soc,
                    previous_soc=self.last_sample["soc"] if not backup and self.last_sample else None,
                    voltage=None if backup else current("battery.voltage"),
                    current=None if backup else current("battery.current"), uptime_seconds=uptime,
                    soc_age_ms=None if backup else ages["battery.soc"] + elapsed * 1000, continuity=continuity)
        return {**sample, "stage": "backup"} if backup else sample

    def show_sample(self, sample):
        source = "SIMULATED · 模拟数据 · 非实机" if sample["source_kind"] == "simulated" else "ROS2观测数据"
        self.identity_label.setText(f"SN：{self.sn}\n机器人：{sample['robot_id']} · {source}")
        before = sample["previous_soc"]
        soc = sample["soc"]
        if soc is None:
            self.soc_label.setText("SOC：无（小电池阶段）")
        else:
            self.soc_label.setText(f"主电池SOC：{before:g}% → {soc:g}%" if before is not None else f"主电池SOC：{soc:g}%")
        def show(value, unit):
            return "—" if value is None else f"{value:g}{unit}"
        self.values_label.setText(f"电压：{show(sample['voltage'], 'V')}　电流：{show(sample['current'], 'A')}\n主控开机时长：{show(sample['uptime_seconds'], 's')}")
        timestamp = f"采集时间：{sample['sampled_at']}\n电脑接收时间：{sample['received_at']}"
        if soc is not None:
            timestamp += f"\nSOC观测年龄：{sample['soc_age_ms']:.0f}ms"
        self.time_label.setText(timestamp)
        self.continuity_label.setText(sample["continuity"])
        stage = sample.get("stage")
        self.stage_label.setText(f"现场标记：{BATTERY_SWAP_STAGES[stage]}" if stage else "")
        self.stage_label.setVisible(stage is not None)

    def show_unknown(self, message):
        self.soc_label.setText("SOC：无（小电池阶段）" if self.backup_active else "SOC：—（当前不可用）")
        self.values_label.setText("电压：—　电流：—　主控开机时长：—")
        self.continuity_label.setText("数据连续性未知；已有截图保留")
        self.stage_label.hide()
        self.notice.setText(message)
        if self.pending_stage:
            self.update_stage_buttons()

    def mark_disconnected(self):
        self.frame = None
        if self.watching:
            self.interrupted = True
        self.show_unknown(self.target_error or "连接中断，等待主控预览恢复。")

    def check_stale(self):
        if not self.watching:
            return
        if self.current_sample(backup=True) is None:
            self.interrupted = True
            self.show_unknown("未收到新鲜主控预览，连续性未知。")
        elif not self.backup_active and self.current_sample() is None:
            self.show_unknown("未收到新鲜SOC，暂不截图；等待恢复。")

    def start(self):
        if self.observation_ids or self.watching or self.target_error:
            return
        if self.frame is not None:
            identity = self.frame["robot_id"], self.frame["source_kind"]
            if not self.validate_target(identity, recheck=True):
                return
            self.bound_robot_id, self.source_kind = identity
        self.watching = True
        self.interrupted = self.reboot_observed = False
        self.last_uptime = None
        self.start_button.setEnabled(False)
        self.finish_button.setEnabled(True)
        self.update_stage_buttons()
        self.notice.setText("监听中：等待新鲜SOC作为起始值；请保持网线连接。")
        sample = self.current_sample()
        if sample is not None:
            self.capture(sample)

    def update_stage_buttons(self):
        candidate = self.stage_candidate()
        for stage, button in self.stage_buttons.items():
            label = BATTERY_SWAP_STAGES[stage]
            pending = self.pending_stage and self.pending_stage[0] == stage
            if stage == "backup":
                caption = "小电池已标记（无SOC）" if stage in self.stage_readings else "标记小电池（无SOC）"
            elif stage in self.stage_readings:
                caption = f"{label} {self.stage_readings[stage]:g}%"
            elif pending:
                caption = f"保存{label} {candidate['soc']:g}%" if candidate else f"{label}等待读数"
            else:
                caption = f"标记{label}"
            button.setText(caption)
            button.setEnabled(self.watching and stage not in self.stage_readings
                              and not (self.backup_active and stage == "first") and (not pending or candidate is not None))

    def stage_candidate(self):
        if self.watching and self.pending_stage:
            _, marked_at, last_frame_at = self.pending_stage
            sample = self.current_sample()
            if sample is not None and time.monotonic() - sample["soc_age_ms"] / 1000 >= marked_at:
                observed_at = self.last_stamp - timedelta(milliseconds=self.frame["ages_ms"]["battery.soc"])
                if last_frame_at is None or observed_at > last_frame_at:
                    return sample
        return None

    def mark_stage(self, stage):
        if not self.watching or stage in self.stage_readings or (self.backup_active and stage == "first"):
            return
        if stage == "backup":
            sample = self.current_sample(backup=True)
            if sample is None:
                if self.bound_robot_id is None:
                    self.notice.setText("等待已核对的机器人连接后，再标记小电池阶段。")
                    return
                now = datetime.now(timezone.utc).isoformat()
                self.interrupted = True
                sample = dict(session_id=self.session_id, robot_id=self.bound_robot_id, source_kind=self.source_kind,
                              sampled_at=now, received_at=now, event="stage", soc=None, previous_soc=None,
                              voltage=None, current=None, uptime_seconds=None, soc_age_ms=None,
                              continuity="人工标记小电池阶段；当前主控预览不可用，连续性未知")
            self.backup_active = True
            self.capture(sample, stage=stage)
            return
        if self.pending_stage and self.pending_stage[0] == stage:
            sample = self.stage_candidate()
            if sample is not None:
                self.capture(sample, stage=stage)
            return
        if stage == "second":
            self.backup_active = False
        self.pending_stage = (stage, time.monotonic(), self.last_stamp)
        self.update_stage_buttons()
        self.notice.setText(f"{BATTERY_SWAP_STAGES[stage]}：等待标记后的SOC读数。")

    def capture(self, sample, *, final=False, stage=None):
        if self.error:
            return
        if not final and not stage and self.last_sample and sample["soc"] == self.last_sample["soc"]:
            return
        if len(self.observation_ids) >= MAX_SOC_CAPTURES:
            self.watching = False
            self.update_stage_buttons()
            self.notice.setText("本轮已保存64张关键截图，已停止截图。已有证据保留，可结束并记录结果。")
            return
        if self.bound_robot_id is None:
            self.bound_robot_id, self.source_kind = sample["robot_id"], sample["source_kind"]
        sample = {**sample, "event": "final" if final else sample["event"]}
        if stage:
            sample.update(event="stage", stage=stage)
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
            self.error = str(exc) or type(exc).__name__
            self.watching = False
            self.update_stage_buttons()
            self.notice.setText(f"截图或证据保存失败：{exc}。已有证据保留；请关闭后重试。")
            self.finish_button.setEnabled(True)
            return
        self.observation_ids.append(row["observation_id"])
        if sample["soc"] is not None:
            self.last_sample = sample
        self.finish_button.setEnabled(True)
        if stage:
            self.stage_readings[stage] = sample["soc"]
            self.pending_stage = None
            self.update_stage_buttons()
            self.notice.setText("已记录小电池阶段（无SOC）。" if stage == "backup" else
                                f"已保存{BATTERY_SWAP_STAGES[stage]}的SOC读数 {sample['soc']:g}%。")
        elif self.pending_stage is None:
            self.notice.setText(f"监听中 · 已保存{len(self.observation_ids)}张截图；起始SOC及每次变化均已关联SN：{self.sn}。")

    def finish(self):
        if self.target_error:
            return
        sample = None if self.backup_active else self.current_sample()
        if sample is not None and self.observation_ids and not self.error:
            self.capture(sample, final=True)
        self.watching = False
        self.accept()

    def done(self, result):
        self.watching = False
        self.timer.stop()
        super().done(result)
