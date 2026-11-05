"""Read-only human research archive detail; no network, SQL or approval actions."""

import json
import math
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

BEIJING = timezone(timedelta(hours=8), "北京时间")
UNKNOWN = "未知 / 未提供"
CONTEXT_LABELS = {
    "campaign_id": "测试计划", "cycle_id": "循环", "segment_id": "阶段",
    "asset_id": "实际样品", "configuration_id": "配置",
    "test_case_version_id": "用例版本", "station_id": "工位",
}
VERDICTS = {"passed": "通过", "failed": "不通过", "blocked": "待核实 / 受阻", "unconfigured": "未配置"}
QUALITY = {"VALID": "有效", "PARTIAL": "部分缺失", "INVALID": "无效", "MISSING": "缺失"}
PROCESSING = {"READY": "解析就绪", "FAILED": "解析失败", "PROCESSING": "正在解析",
              "QUEUED": "等待解析", "WAITING": "等待上传"}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return UNKNOWN if value is None or value == "" else str(value)


def _number(value, unit):
    if value is None:
        return UNKNOWN
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return "原值无效（见执行身份）"
    return f"{value} {unit}"


def _time(value):
    if value is None or value == "":
        return UNKNOWN
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "未注明时区（见执行身份）"
        return parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"
    except (ValueError, TypeError, AttributeError, OverflowError):
        return "原时间格式无效（见执行身份）"


def _label(value, name=""):
    label = QLabel(_text(value))
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _scroll(widget):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(widget)
    return scroll


class ResearchArchiveView(QWidget):
    """Render the actual archive DETAIL/manifest contract without deriving facts.

    ``facts`` and ``current_verification.declared_*`` are independent sources.
    Classification is the server's flat seven-UUID object. Quality is from
    ``manifest.quality_status``, ``analysis.quality`` and ``monitor_validation``.
    """

    def __init__(self, archive, parent=None):
        super().__init__(parent)
        self.archive = _mapping(archive)
        self.facts = _mapping(self.archive.get("facts"))
        self.verification = _mapping(self.archive.get("current_verification"))
        self.manifest = _mapping(self.archive.get("manifest"))
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("研发归档详情")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
        self.tabs.addTab(_scroll(self._overview()), "测试详情")
        self.tabs.addTab(_scroll(self._identity()), "执行身份 / 原值")
        self.tabs.currentChanged.connect(self._show_raw)

    @staticmethod
    def _section(layout, title, description):
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow(_label(description))
        layout.addWidget(group)
        return form

    @staticmethod
    def _row(form, title, value, name):
        label = _label(value, name)
        label.setAccessibleName(title)
        form.addRow(_label(title), label)

    def _overview(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        summary = self._section(layout, "测试档案", "原采集事实、人员核实与解析质量分别展示，互不代填。")
        for key, title in (("actual_object", "实际对象"), ("test_name", "测试名称"),
                           ("station_label", "归档工位"), ("owner_name", "平台提交人")):
            self._row(summary, title, self.archive.get(key), "archive_" + key)
        self._row(summary, "档案来源", {"HISTORY": "历史原始记录归档", "HMI": "上位机采集归档", "OTHER": "其他测试归档"}.get(self.archive.get("origin"), UNKNOWN), "archive_origin")
        facts = self._section(layout, "原采集事实", "这些是归档原值。缺失保持未知，不用监控时长或人工声明补成设备实测事实。")
        self._measurements(facts, self.facts, "fact", "")
        self._row(facts, "原机器状态", {"unknown": "未知", "completed": "已完成", "failed": "失败", "stopped": "已停止", "interrupted": "已中断"}.get(self.facts.get("machine_status"), UNKNOWN), "fact_machine_status")
        self._row(facts, "原自动结论", VERDICTS.get(self.facts.get("automatic_verdict"), UNKNOWN), "fact_automatic_verdict")
        for key, title in (("data_quality", "原数据质量"), ("clock_quality", "原时钟质量")):
            self._row(facts, title, QUALITY.get(self.facts.get(key), UNKNOWN), "fact_" + key)

        manual = self._section(layout, "人工核实声明", "以下由人员核实填写，不是设备自动实测；人员结论与平台审批、MTBF统计资格分别确定。")
        self._row(manual, "实际测试人员", self.verification.get("tester"), "declared_tester")
        self._row(manual, "人工结论", VERDICTS.get(self.verification.get("verdict"), UNKNOWN), "declared_verdict")
        self._row(manual, "人工核实工位", self.verification.get("station_name") or self.verification.get("station_code"), "declared_station")
        self._measurements(manual, self.verification, "declared", "declared_")
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setObjectName("verification_notes")
        self.notes.setAccessibleName("人工核实备注（原文）")
        self.notes.setMinimumHeight(100)
        self.notes.setMaximumHeight(220)
        self.notes.setPlainText(_text(self.verification.get("notes")))
        manual.addRow(_label("核实备注（原文）"), self.notes)

        parsed = self._section(layout, "文件解析质量", "解析就绪或质量有效，不代表硬件合格、有效运行暴露或MTBF资格。")
        analysis = _mapping(self.manifest.get("analysis"))
        quality = _mapping(analysis.get("quality"))
        validation = _mapping(self.manifest.get("monitor_validation"))
        self._row(parsed, "解析状态", PROCESSING.get(self.archive.get("processing_state"), UNKNOWN), "parse_state")
        self._row(parsed, "解析质量", QUALITY.get(self.manifest.get("quality_status"), UNKNOWN), "parse_quality")
        for key, title in (("row_count", "CSV数据行"), ("valid_time_rows", "时间有效行"),
                           ("invalid_time_rows", "时间无效行"), ("non_monotonic", "时间非递增行")):
            self._row(parsed, title, _number(quality.get(key), "行"), "parse_" + key)
        self._row(parsed, "最大采样间隔", _number(quality.get("max_gap_seconds"), "秒"), "parse_max_gap_seconds")
        for key, title in (("verified_sample_count", "已校验原点"), ("missing_sequence_count", "缺失采样序号"),
                           ("disconnected_rows", "断线空点")):
            self._row(parsed, title, _number(validation.get(key), "个"), "parse_" + key)
        hashes = validation.get("payload_hashes_verified")
        self._row(parsed, "原点哈希核对", "逐点匹配" if hashes is True else "未通过" if hashes is False else UNKNOWN, "parse_payload_hashes_verified")
        layout.addStretch()
        return page

    def _measurements(self, form, source, name, prefix):
        for key, title in (("started_at", "测试开始（北京时间）"), ("ended_at", "测试结束（北京时间）")):
            self._row(form, title, _time(source.get(prefix + key)), name + "_" + key)
        for key, title, unit in (("active_seconds", "有效运行时长", "秒"), ("completed_cycles", "完成循环", "次"),
                                 ("exception_count", "异常次数", "次")):
            self._row(form, title, _number(source.get(prefix + key), unit), name + "_" + key)

    def _identity(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        identity = self._section(layout, "执行身份", "供核对原始身份与版本；不会创建、修改或替换执行上下文。")
        for key, title in (("id", "归档ID"), ("execution_uuid", "执行UUID"), ("execution_id", "正式执行ID"),
                           ("filename", "原文件名"), ("sha256", "原文件SHA256"),
                           ("revision", "归档修订"), ("submission_revision", "送审修订"),
                           ("submission_status", "审批状态（原值）"), ("processing_state", "解析状态（原值）")):
            self._row(identity, title, self.archive.get(key), "identity_" + key)
        classification = _mapping(self.archive.get("classification"))
        for key, title in CONTEXT_LABELS.items():
            self._row(identity, title + " UUID", classification.get(key), "identity_" + key)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.raw.setObjectName("archive_original_fields")
        self.raw.setAccessibleName("原采集事实、人工核实与解析原值")
        self.raw.setPlaceholderText("打开此页时显示可核对的原始字段。")
        layout.addWidget(self.raw, 1)
        return page

    def _show_raw(self, index):
        if index != 1 or self.raw.toPlainText():
            return
        fields = {key: self.archive.get(key) for key in ("facts", "current_verification", "classification", "processing_state", "processing_error")}
        fields["manifest"] = {key: self.manifest.get(key) for key in ("quality_status", "monitor_recording", "monitor_validation", "analysis")}
        self.raw.setPlainText(json.dumps(fields, ensure_ascii=False, indent=2, default=str))
