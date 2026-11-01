"""Desktop-only model preview and offline evidence analysis; no robot control."""

import json
import math
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .desktop_limit_execution import LimitExecutionPanel
from .joint_limits import MAX_RECORDING_BYTES, MAX_URDF_BYTES, analyze_recording


def _text(value):
    label = QLabel(value)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _read_text(path, maximum):
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError(f"文件超过 {maximum // (1024 * 1024)} MiB")
    return data.decode("utf-8")


class LimitAnalysisPanel(QFrame):
    """Show a local simulation and analyze one recorded joint at a time."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("healthBanner")
        self.urdf_path = None
        self.recording_path = None
        layout = QVBoxLayout(self)
        title = _text("单侧限位检查 · 仿真与离线记录")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addWidget(_text(
            "先观看 simulated 仿真，再选择电脑上的 URDF 和单关节记录进行离线分析；"
            "当前没有真机检查执行器，不会发送电机指令。"
        ))
        tolerance_row = QHBoxLayout()
        tolerance_label = _text("本次参考容差 / ±°")
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setAccessibleName("本次参考容差，单位度")
        self.tolerance.setRange(0.001, 180.0)
        self.tolerance.setDecimals(3)
        self.tolerance.setSingleStep(0.1)
        self.tolerance.setValue(3.0)
        self.tolerance.valueChanged.connect(self._tolerance_changed)
        tolerance_label.setBuddy(self.tolerance)
        tolerance_row.addWidget(tolerance_label)
        tolerance_row.addWidget(self.tolerance)
        tolerance_row.addStretch()
        layout.addLayout(tolerance_row)
        layout.addWidget(_text(
            "容差仅用于本次比较，不修改记录文件；参考为所选本地 URDF，"
            "模型版本、关节映射与实物机械基准仍需确认。"
        ))
        row = QHBoxLayout()
        self.preview_button = QPushButton("观看仿真视频")
        self.preview_button.setObjectName("secondary")
        self.preview_button.clicked.connect(self.open_preview)
        self.urdf_button = QPushButton("选择 URDF…")
        self.urdf_button.setObjectName("secondary")
        self.urdf_button.clicked.connect(self.choose_urdf)
        self.recording_button = QPushButton("选择记录…")
        self.recording_button.setObjectName("secondary")
        self.recording_button.clicked.connect(self.choose_recording)
        self.analyze_button = QPushButton("分析记录")
        self.analyze_button.setObjectName("secondary")
        self.analyze_button.setEnabled(False)
        self.analyze_button.clicked.connect(self.analyze)
        for button in (self.preview_button, self.urdf_button, self.recording_button, self.analyze_button):
            button.setMinimumHeight(40)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.paths_label = _text("URDF：未选择  ·  记录：未选择")
        layout.addWidget(self.paths_label)
        self.result_label = _text("等待选择文件。每份记录对应一个关节，可包含单侧数据。")
        self.result_label.setAccessibleName("限位记录分析结果")
        layout.addWidget(self.result_label)
        self.execution_panel = LimitExecutionPanel(self)
        layout.addWidget(self.execution_panel)

    def _tolerance_changed(self):
        if hasattr(self, "result_label"):
            self.result_label.setText("容差已更改，请重新分析；前次结果已清除。")

    def open_preview(self):
        path = (Path(__file__).resolve().parents[2] / "runtime/mujoco-preview/"
                "single-side-shorter-20261030/single-side-both.mp4")
        if not path.is_file():
            self.result_label.setText("未找到本机仿真视频；请先生成模拟预览。没有进行真机操作。")
            return
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.result_label.setText("已打开 simulated 仿真视频；模型运动不代表真实机器人动作。")
        else:
            self.result_label.setText("无法打开视频播放器；仿真文件位于：" + str(path))

    def _selected(self):
        self.paths_label.setText(
            f"URDF：{self.urdf_path or '未选择'}  ·  记录：{self.recording_path or '未选择'}"
        )
        self.analyze_button.setEnabled(bool(self.urdf_path and self.recording_path))
        self.result_label.setText("文件已更新，请重新分析；前次结果已清除。")

    def choose_urdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择已展开的 URDF", "", "URDF (*.urdf *.xml)")
        if path:
            self.urdf_path = Path(path)
            self._selected()

    def choose_recording(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择单关节限位记录", "", "记录 (*.json)")
        if path:
            self.recording_path = Path(path)
            self._selected()

    def analyze(self):
        if not self.urdf_path or not self.recording_path:
            return
        try:
            urdf = _read_text(self.urdf_path, MAX_URDF_BYTES)
            recording = json.loads(_read_text(self.recording_path, MAX_RECORDING_BYTES))
            if not isinstance(recording, dict):
                raise ValueError("记录必须是 JSON 对象")
            # The input file is never changed. The override applies only to this comparison.
            recording["tolerance_rad"] = math.radians(self.tolerance.value())
            result = analyze_recording(recording, urdf)
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            self.result_label.setText("分析失败：" + str(error) + "\n未产生标零结论。")
            return
        source = "simulated 模拟数据 · 非实机" if result["source_kind"] == "simulated" else "ROS2 来源 · 实机仍待验收"
        lines = [f"{source} · 机器人 {result['robot_id']} · 关节 {result['joint_name']}",
                 f"标零记录：{result['calibration_id']}",
                 f"参考：所选本地 URDF · 本次用户指定容差：±{self.tolerance.value():g}°（未修改记录）"]
        flags = [("关节映射", result["mapping_verified"]),
                 ("力矩反馈", result["torque_feedback_verified"]),
                 ("机械参考", result["mechanical_reference_verified"])]
        lines.append("记录声明：" + "、".join(f"{name}{'已确认' if checked else '待确认'}" for name, checked in flags)
                     + "（来自记录，未独立核验）")
        states = {"candidate_obstruction": "候选阻挡", "aborted": "分析中止", "incomplete": "证据不足"}
        for direction in result["directions"]:
            side = "下限侧" if direction["direction"] == "lower" else "上限侧"
            lines.append(f"{side}：{states.get(direction['status'], direction['status'])} · {direction['reason']}")
        comparison = result["single_endpoint_comparison"]
        if comparison:
            delta = math.degrees(comparison["observed_minus_urdf_rad"])
            reference = comparison["within_reference_tolerance"]
            condition = "待确认" if reference is None else "在本次容差内" if reference else "超出本次容差"
            lines.append(f"候选位置 − URDF 模型端点：{delta:+.2f}° · 参考比较：{condition}")
        lines.append("候选阻挡不等于机械止挡；不能据此判定标零通过。仅离线分析，未控制机器人。")
        self.result_label.setText("\n".join(lines))
