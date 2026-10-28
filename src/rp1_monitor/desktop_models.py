"""Window-local model credentials and a provider/model selector."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .desktop_chat import text_label
from .model_providers import PRESETS


class ModelDialog(QDialog):
    test_requested = Signal()
    catalog_requested = Signal()

    def __init__(self, parent, server, token):
        super().__init__(parent)
        self.setWindowTitle("模型连接 · RP1")
        self.setMinimumWidth(570)
        self.setStyleSheet(
            "QDialog {background:#f8faff;} QLabel {color:#526780;} QLineEdit,QComboBox {min-height:28px; background:white; border:1px solid #cfdbed; border-radius:7px; padding:7px;}"
        )
        self.items = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(text_label("连接你的模型", "welcomeTitle"))
        layout.addWidget(
            text_label("选择厂商，填入模型 ID 与 API Key。Key 仅保留在本窗口，关闭后不保存。")
        )
        form = QFormLayout()
        form.setSpacing(14)
        form.addRow("中央服务", server)
        form.addRow("只读凭据", token)
        self.provider = QComboBox()
        self.provider.setAccessibleName("模型服务商")
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setAccessibleName("模型 ID")
        self.model.lineEdit().setPlaceholderText("填写该平台实际可用的模型 ID")
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setAccessibleName("模型 API Key")
        self.key.setPlaceholderText("留空时读取所选服务的服务端环境变量")
        self.endpoint = text_label("读取服务端模型配置以确认实际地址")
        self.endpoint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("模型服务商", self.provider)
        form.addRow("模型 ID", self.model)
        form.addRow("API Key", self.key)
        form.addRow("请求地址", self.endpoint)
        layout.addLayout(form)
        self.enabled = QCheckBox("用于诊断对话（发送问题、最近对话与所选证据）")
        layout.addWidget(self.enabled)
        self.status = text_label("连接测试只验证接口与工具调用，不发送机器人数据。")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.refresh = QPushButton("刷新服务端配置")
        self.test = QPushButton("测试连接")
        clear = QPushButton("清除 Key")
        close = QPushButton("完成")
        self.refresh.clicked.connect(self.catalog_requested)
        self.test.clicked.connect(self.test_requested)
        clear.clicked.connect(self.key.clear)
        close.clicked.connect(self.accept)
        for button in (self.refresh, self.test, clear):
            button.setObjectName("contextAction")
            actions.addWidget(button)
        actions.addStretch()
        close.setObjectName("chatSend")
        actions.addWidget(close)
        layout.addLayout(actions)
        self.provider.currentIndexChanged.connect(self.change_provider)
        self.load_catalog(
            {
                "items": [
                    {
                        "id": "server",
                        "name": "服务端默认",
                        "models": [],
                        "endpoint": "读取服务端配置以确认实际地址",
                    }
                ]
                + [
                    {
                        "id": key,
                        "name": item["name"],
                        "models": item["models"],
                        "endpoint": item["baseUrl"]
                        + (
                            "/responses"
                            if item["api"] == "openai-responses"
                            else "/chat/completions"
                        ),
                    }
                    for key, item in PRESETS.items()
                ]
            }
        )

    def change_provider(self):
        self.key.clear()
        item = self.items.get(self.provider.currentData(), {})
        self.model.clear()
        for model in item.get("models", []):
            self.model.addItem(model["id"])
        self.model.setEditText(item["models"][0]["id"] if item.get("models") else "")
        self.endpoint.setText(item.get("endpoint", "地址待确认"))
        self.status.setText("已切换服务；请使用该厂商及地域对应的 API Key。")

    def load_catalog(self, catalog):
        chosen = self.provider.currentData()
        model = self.model.currentText()
        self.items = {item["id"]: item for item in catalog["items"]}
        self.provider.blockSignals(True)
        self.provider.clear()
        for item in catalog["items"]:
            self.provider.addItem(item["name"], item["id"])
        self.provider.setCurrentIndex(max(0, self.provider.findData(chosen)))
        self.provider.blockSignals(False)
        # Refresh metadata without discarding a key or typed model for the same provider.
        key = self.key.text() if chosen == self.provider.currentData() else ""
        self.change_provider()
        if chosen == self.provider.currentData():
            self.key.setText(key)
            if model:
                self.model.setEditText(model)

    def connection(self):
        return {
            "provider": self.provider.currentData(),
            "model": self.model.currentText().strip(),
            "api_key": self.key.text().strip(),
        }
