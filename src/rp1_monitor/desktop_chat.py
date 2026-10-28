"""Small native conversation widgets, with selectable text and explicit evidence."""

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

CHAT_STYLE = """
QWidget#operations { background:transparent; color:#20314d; font-size:13px; }
QFrame#conversation { background:#ffffff; border:1px solid #d9e3f1; border-radius:16px; }
QFrame#chatHeader { background:transparent; border:0; border-bottom:1px solid #e7edf5; border-radius:0; }
QLabel#assistantMark { background:#2b59e8; color:white; border-radius:12px; font-size:16px; font-weight:700; }
QLabel#chatTitle { color:#192d4c; font-size:17px; font-weight:700; }
QLabel#chatMuted { color:#6a7b93; font-size:12px; }
QLabel#chatEyebrow { color:#5273ac; font-size:10px; font-weight:600; }
QLabel#welcomeTitle { color:#1b3153; font-size:27px; font-weight:700; }
QLabel#welcomeCopy { color:#64758f; font-size:13px; }
QLabel#sourceChip { background:#fff4df; color:#87591b; border:1px solid #eedab7; border-radius:6px; padding:5px 8px; font-size:11px; }
QPushButton#chatQuiet { background:transparent; color:#61738d; border:0; border-radius:6px; padding:7px 9px; font-size:12px; }
QPushButton#chatQuiet:hover { background:#edf3fc; color:#2b59e8; }
QPushButton#suggestion { background:#f8faff; color:#35537e; border:1px solid #e1e9f6; border-radius:10px; padding:14px; text-align:left; font-size:13px; }
QPushButton#suggestion:hover { background:#edf3ff; border-color:#b6cafa; }
QFrame#assistantMessage { background:#f4f7fc; border:1px solid #e6edf6; border-radius:12px; }
QFrame#userMessage { background:#eaf0ff; border:1px solid #d9e4ff; border-radius:12px; }
QLabel#messageBody { color:#253b5a; font-size:14px; background:transparent; border:0; }
QLabel#messageMeta { color:#74839a; font-size:11px; background:transparent; border:0; }
QPushButton#evidenceToggle { background:transparent; color:#416cb8; border:0; text-align:left; padding:3px 0; font-size:11px; }
QPlainTextEdit#evidenceText { background:white; border:1px solid #dce6f3; border-radius:6px; color:#536781; font-size:11px; padding:8px; }
QFrame#composer { background:#ffffff; border:1px solid #cbd9ee; border-radius:12px; }
QPlainTextEdit#chatInput { background:transparent; border:0; padding:4px; font-size:14px; color:#253b5a; }
QPushButton#chatSend { background:#2b59e8; color:white; border:0; border-radius:8px; padding:9px 16px; font-weight:600; }
QPushButton#chatSend:hover { background:#2149c9; }
QPushButton#chatSend:disabled { background:#dfe7f5; color:#8697b1; }
QFrame#chatContext { background:#f7f9fd; border:1px solid #dce5f2; border-radius:14px; }
QLabel#contextTitle { color:#304766; font-size:13px; font-weight:700; }
QLabel#contextRobot { color:#213b62; font-size:19px; font-weight:700; }
QTableWidget#contextTable { background:transparent; border:0; color:#526780; gridline-color:transparent; font-size:11px; }
QTableWidget#contextTable::item { border-bottom:1px solid #e7edf6; padding:5px 0; }
QPushButton#contextAction { background:#ffffff; border:1px solid #d7e1f0; border-radius:7px; color:#41608d; padding:9px; text-align:left; font-size:12px; }
QPushButton#contextAction:hover { border-color:#9bb7ec; background:#eef4ff; }
QComboBox, QSpinBox, QLineEdit { background:white; border:1px solid #d4dfef; border-radius:6px; padding:6px; font-size:12px; min-height:22px; }
QCheckBox { color:#526780; font-size:12px; spacing:7px; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid #b6c8e2; border-radius:4px; background:white; }
QCheckBox::indicator:checked { background:#2b59e8; border-color:#2b59e8; }
QScrollArea { background:transparent; border:0; }
QScrollBar:vertical { background:transparent; width:6px; margin:2px; }
QScrollBar::handle:vertical { background:#d0dced; min-height:26px; border-radius:3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QPushButton:focus, QComboBox:focus, QSpinBox:focus, QLineEdit:focus { border:1px solid #2b59e8; }
"""


def text_label(text, name="chatMuted"):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


class ChatInput(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class ChatMessage(QFrame):
    def __init__(self, role, text, meta, evidence=None):
        super().__init__()
        self.setObjectName("userMessage" if role == "user" else "assistantMessage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.setSpacing(9)
        self.meta = text_label(meta, "messageMeta")
        layout.addWidget(self.meta)
        self.body = text_label(text, "messageBody")
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.body)
        if evidence is not None:
            self.evidence_toggle = QPushButton("查看查询证据  ›")
            self.evidence_toggle.setObjectName("evidenceToggle")
            self.evidence_toggle.setCheckable(True)
            self.evidence = QPlainTextEdit()
            self.evidence.setObjectName("evidenceText")
            self.evidence.setReadOnly(True)
            self.evidence.setPlainText(json.dumps(evidence, ensure_ascii=False, indent=2))
            self.evidence.setAccessibleName("本条回答的原始查询证据")
            self.evidence.setFixedHeight(180)
            self.evidence.hide()
            self.evidence_toggle.toggled.connect(self.evidence.setVisible)
            self.evidence_toggle.toggled.connect(
                lambda opened: self.evidence_toggle.setText(
                    "收起查询证据  ⌄" if opened else "查看查询证据  ›"
                )
            )
            layout.addWidget(self.evidence_toggle)
            layout.addWidget(self.evidence)


def message_row(message, role):
    row = QFrame()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    if role == "user":
        longest = max(message.body.text().splitlines(), key=len, default="")
        natural_width = message.body.fontMetrics().horizontalAdvance(longest) + 38
        message.setMaximumWidth(max(220, min(620, natural_width)))
        layout.addStretch(1)
    layout.addWidget(message, 6)
    if role != "user":
        layout.addStretch(1)
    return row
