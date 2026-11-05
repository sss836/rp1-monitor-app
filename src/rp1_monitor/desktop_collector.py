"""Native SSH lifecycle controls for the robot's independent telemetry collector."""

import json
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .ssh_transport import HostKeyRequired

_WORKERS = set()
_MANAGED_ROOT = "~/.local/share/rp1-collector-managed"
_UPLOAD_ROOT = "~/.cache/rp1-collector-upload"
_LABELS = {"status": "刷新状态", "install": "安装采集端", "update": "更新采集端",
           "start": "启动采集端", "stop": "停止采集端", "uninstall": "卸载并清除机器人数据"}


def label(value):
    widget = QLabel(value)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


class CollectorWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    host_key_required = Signal(str, str)

    def __init__(self, target, password, known_hosts, confirmed_fingerprint, action, bundle, robot_id):
        # A pending operation must survive the owner widget until finished.
        super().__init__()
        self.target, self.password = target, password
        self.known_hosts, self.confirmed_fingerprint = known_hosts, confirmed_fingerprint
        self.action, self.bundle, self.robot_id = action, bundle, robot_id

    def run(self):
        manager = None
        try:
            from .collector_manager import CollectorManager

            manager = CollectorManager(self.target, self.password, self.known_hosts,
                                       confirmed_fingerprint=self.confirmed_fingerprint)
            result = manager.run(self.action, bundle_path=self.bundle, robot_id=self.robot_id,
                                 confirm_delete=self.action == "uninstall")
            if not isinstance(result, dict):
                raise ValueError("机器人采集端返回格式无效；请刷新状态核对实际结果")
            self.completed.emit(result)
        except HostKeyRequired as error:
            self.host_key_required.emit(error.host, error.fingerprint)
        except Exception as error:
            detail = str(error)
            if self.password:
                detail = detail.replace(self.password, "[已隐藏]")
            self.failed.emit(detail or "操作未完成；请刷新状态核对机器人")
        finally:
            try:
                if manager is not None:
                    manager.close()
            finally:
                self.password = ""


class CollectorDialog(QDialog):
    def __init__(self, context=None, parent=None):
        super().__init__(parent)
        context = context or {}
        self.worker = None
        self.fingerprint = ""
        self.snapshot = None
        self.operation_succeeded = False
        self.known_hosts = Path(context.get("known_hosts", Path("runtime/station/known_hosts")))
        self.setObjectName("collectorDialog")
        self.setWindowTitle("机器人采集端管理")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("""
            QDialog#collectorDialog { background:#eef5ff; color:#142441; }
            QDialog#collectorDialog QLabel { background:transparent; color:#142441; }
            QDialog#collectorDialog QLineEdit, QDialog#collectorDialog QPlainTextEdit {
                background:#ffffff; color:#142441; border:1px solid #a7bcd8; border-radius:5px; padding:7px; }
            QDialog#collectorDialog QPushButton { min-height:28px; }
            QDialog#collectorDialog QPushButton#removeCollector { background:#a9323e; color:white; }
            QDialog#collectorDialog QPushButton#removeCollector:disabled { background:#e4ebf5; color:#65778e; }
        """)
        self.resize(880, 720)
        body = QVBoxLayout(self)
        body.setSpacing(12)
        body.addWidget(label("管理机器人上的独立采集程序。网线 SSH 即可操作；启动采集后，再返回设备设置连接监控。"))
        form = QFormLayout()
        self.target = QLineEdit(context.get("target", ""))
        self.target.setPlaceholderText("ssh robo@192.168.137.250")
        self.target.setAccessibleName("采集端管理SSH目标")
        form.addRow("SSH目标", self.target)
        self.robot_id = QLineEdit(context.get("robot_id", ""))
        self.robot_id.setPlaceholderText("机器人编号；安装时必填")
        self.robot_id.setAccessibleName("采集端机器人编号")
        form.addRow("采集端机器人编号", self.robot_id)
        form.addRow("", label("刷新状态可读取已安装编号；测试样品编号在整机测试中填写。"))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("仅当前窗口使用，关闭后清除，不保存")
        self.password.setAccessibleName("采集端管理SSH密码")
        form.addRow("SSH密码", self.password)
        self.fingerprint_label = label("首次连接请核对主机指纹；已登记的机器人仍校验原指纹。")
        form.addRow("主机指纹", self.fingerprint_label)
        self.trust = QCheckBox("已核对上方主机指纹")
        self.trust.hide()
        form.addRow("身份确认", self.trust)
        self.trust_label = form.labelForField(self.trust)
        self.trust_label.hide()
        self.bundle = QLineEdit()
        self.bundle.setPlaceholderText("默认使用随上位机交付的离线包，也可手动选择 ZIP")
        self.bundle.setAccessibleName("采集端离线安装包")
        self.browse = QPushButton("选择离线包…")
        self.browse.clicked.connect(self.choose_bundle)
        bundle_row = QHBoxLayout()
        bundle_row.addWidget(self.bundle, 1)
        bundle_row.addWidget(self.browse)
        form.addRow("安装 / 更新包", bundle_row)
        body.addLayout(form)
        body.addWidget(label("管理目录：" + _MANAGED_ROOT + "\n卸载会清除该目录内的程序、配置、缓存、日志和测试数据，以及本工具上传缓存；电脑中已经接收的结果保留。"))
        self.buttons = {}
        for actions in (("status", "install", "update"), ("start", "stop", "uninstall")):
            row = QHBoxLayout()
            for action in actions:
                button = QPushButton(_LABELS[action] + ("…" if action != "status" else ""))
                button.setAccessibleName(_LABELS[action])
                if action == "uninstall":
                    button.setObjectName("removeCollector")
                button.clicked.connect(lambda _=False, value=action: self.request(value))
                row.addWidget(button)
                self.buttons[action] = button
            body.addLayout(row)
        self.status = label("尚未读取机器人状态。未安装或 8891 未开启时也可以通过 SSH 安装和启动。")
        body.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("采集端操作进度")
        body.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setAccessibleName("采集端操作结果")
        body.addWidget(self.log, 1)
        close_row = QHBoxLayout()
        close_row.addStretch()
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        close_row.addWidget(self.close_button)
        body.addLayout(close_row)
        self.target.textChanged.connect(self.target_changed)
        self.robot_id.textChanged.connect(lambda: setattr(self, "snapshot", None))

    def target_changed(self):
        self.snapshot = None
        self.fingerprint = ""
        self.trust.setChecked(False)
        self.trust.hide()
        self.trust_label.hide()
        self.password.clear()
        self.robot_id.clear()
        self.fingerprint_label.setText("SSH目标已变更；请核对新机器人编号并重新输入密码。")
        self.status.setText("SSH目标已变更，原机器人状态已清除。")
        self.log.clear()

    def choose_bundle(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择机器人采集端离线包", "", "采集端离线包 (*.zip)")
        if path:
            self.bundle.setText(path)

    def confirmation(self, action):
        target = self.target.text().strip()
        robot_id = self.robot_id.text().strip() or "以已安装配置为准"
        descriptions = {
            "install": "创建独立采集目录、虚拟环境和配置，安装离线依赖。安装后由你点击启动。",
            "update": "更新受管采集程序和独立依赖，保留已有配置及测试数据。运行中的采集端须先停止。",
            "start": "在机器人后台启动采集，监听 8891；关闭 SSH 后继续运行。本操作不设置开机自启。",
            "stop": "停止本工具管理的采集进程，记录暂时中断；已有文件保留。",
            "uninstall": "停止本工具管理的采集进程，并删除机器人上本工具的程序、配置、缓存、日志和全部测试数据。此操作不可恢复；电脑中已接收的结果保留。",
        }
        paths = [_MANAGED_ROOT, _UPLOAD_ROOT]
        if action == "uninstall" and self.snapshot:
            paths = self.snapshot.get("deletion_paths") or paths
        detail = (f"SSH：{target}\n机器人编号：{robot_id}\n\n{descriptions[action]}\n\n"
                  + "机器人内部操作范围：\n" + "\n".join(str(value) for value in paths)
                  + "\n\n本入口仅管理采集端。卸载同时清理已识别且编号匹配的旧采集安装；不会根据目录名称猜测归属。")
        if action in {"install", "update"}:
            detail += "\n离线包：" + (self.bundle.text().strip() or "随上位机交付的默认包")
        box = QMessageBox(QMessageBox.Icon.Warning if action == "uninstall" else QMessageBox.Icon.Question,
                          _LABELS[action], detail,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def request(self, action):
        if self.worker:
            return
        if not self.target.text().strip() or not self.password.text():
            self.status.setText("请填写本次操作的 SSH目标和密码。")
            return
        if action != "status" and not self.robot_id.text().strip():
            self.status.setText("请填写这台机器人的编号，或先刷新状态读取已安装配置。")
            return
        if action == "uninstall" and (not self.snapshot or
                                      self.snapshot.get("robot_id") != self.robot_id.text().strip()):
            self.status.setText("卸载前请先刷新状态，核对本机器人编号和将删除的实际目录。")
            return
        if self.fingerprint and not self.trust.isChecked():
            self.status.setText("请核对主机指纹并确认后，再点击操作。")
            return
        if action != "status" and not self.confirmation(action):
            return
        bundle = self.bundle.text().strip()
        if action in {"install", "update"} and bundle and not Path(bundle).is_file():
            self.status.setText("离线包不存在，请重新选择。")
            return
        worker = CollectorWorker(self.target.text().strip(), self.password.text(), self.known_hosts,
                                 self.fingerprint if self.trust.isChecked() else "", action,
                                 Path(bundle) if bundle else None, self.robot_id.text().strip())
        self.worker = worker
        self.operation_succeeded = False
        _WORKERS.add(worker)
        self.set_busy(True)
        self.status.setText(_LABELS[action] + "进行中，请保持网线连接；完成前请勿关闭窗口。")
        self.log.appendPlainText(_LABELS[action] + "：正在建立 SSH 连接并核对机器人身份。")
        worker.completed.connect(self.completed)
        worker.failed.connect(self.failed)
        worker.host_key_required.connect(self.need_host_key)
        worker.finished.connect(self.worker_finished)
        worker.start()

    def completed(self, result):
        self.operation_succeeded = True
        self.log.appendPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("robot_id") and not self.robot_id.text().strip():
            self.robot_id.setText(str(result["robot_id"]))
        self.snapshot = result
        if "installed" in result:
            installed = "已安装" if result["installed"] else "未安装"
            running = "运行中" if result.get("running") else "未运行"
            build = str(result.get("bundle_sha256") or "")[:12]
            version = str(result.get("version") or "未知") + (f" / {build}" if build else "")
            self.status.setText(f"{installed} · {running} · 版本 {version} · "
                                + str(result.get("message", "操作回执已收到，请按状态继续。")))
        else:
            self.status.setText(str(result.get("message", "操作回执已收到，请刷新状态核对。")))
        installed_id = result.get("robot_id")
        entered_id = self.robot_id.text().strip()
        if installed_id and entered_id and entered_id != installed_id:
            self.status.setText(f"已读取机器人状态；采集端已安装编号为 {installed_id}，当前填写的是 {entered_id}。"
                                "请核对后修改上方编号，再执行启动等操作；本次没有修改机器人编号。")

    def failed(self, detail):
        self.operation_succeeded = False
        self.snapshot = None
        self.status.setText("操作未确认完成：" + detail)
        self.log.appendPlainText("操作未确认完成：" + detail)

    def need_host_key(self, host, fingerprint):
        self.operation_succeeded = False
        self.fingerprint = fingerprint
        self.fingerprint_label.setText(f"{host}\n{fingerprint}")
        self.trust.setChecked(False)
        self.trust.show()
        self.trust_label.show()
        self.status.setText("尚未执行操作。请核对上方主机指纹，确认后重新点击操作。")

    def worker_finished(self):
        worker, self.worker = self.worker, None
        _WORKERS.discard(worker)
        worker.deleteLater()
        self.set_busy(False)

    def set_busy(self, busy):
        for widget in (self.target, self.robot_id, self.password, self.bundle, self.browse,
                       self.trust, self.close_button, *self.buttons.values()):
            widget.setEnabled(not busy)
        self.progress.setRange(0, 0 if busy else 1)
        self.progress.setValue(1 if not busy and self.operation_succeeded else 0)

    def shutdown(self):
        if self.worker:
            self.status.setText("正在完成采集端操作，请等待结果后关闭。")
            return False
        self.password.clear()
        return True

    def reject(self):
        if self.shutdown():
            super().reject()

    def closeEvent(self, event):
        if not self.shutdown():
            event.ignore()
        else:
            super().closeEvent(event)
