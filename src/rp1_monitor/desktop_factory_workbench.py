"""Factory case navigation, reusing the panel's existing tools and records."""

import time

from PySide6.QtCore import QSignalBlocker, QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .factory_test_catalog import CATEGORY_NAMES, MOTION_CASE_CODES, test_catalog


def _label(text="", name=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    if name:
        widget.setObjectName(name)
    return widget


class _Disclosure(QPushButton):
    """Use a bound Qt receiver rather than a signal closure owning its sender."""

    def __init__(self, text, content):
        super().__init__("▸  " + text)
        self.caption = text
        self.content = content
        self.setObjectName("workbenchDisclosure")
        self.setCheckable(True)
        self.toggled.connect(self._expanded)

    def _expanded(self, checked):
        self.content.setVisible(checked)
        self.setText(("▾  " if checked else "▸  ") + self.caption)


_SHORT_NAMES = {
    "SYS-002": "灯板确认", "SYS-003": "电机固件", "SYS-004": "电机参数",
    "SYS-012": "急停对频", "SYS-011": "UFO特技",
}
_DESCRIPTIONS = {
    "SYS-001": "打开现有硬件自检工具。17项检查逐项归档到当前SN。",
    "SYS-002": "按原流程点亮灯板，在这里记录现场实际观察。",
    "SYS-003": "使用现有电机检查工具读取固件版本，结果保存到当前SN。",
    "SYS-004": "先检查实际与期望编码范围，再选择需要更新的电机。",
    "SYS-012": "现场完成远程急停开关对频后，直接记录是否通过。",
    "SYS-005": "保留现有标零工具，完成操作后返回当前SN确认流程状态。",
    "SYS-006": "使用现有标定检查工具，逐关节结果自动关联开始时选定的SN。",
    "SYS-007": "由人员操作独立急停遥控器，完成后确认现场测试结果。",
    "SYS-008": "开始换电并监听SOC，保存变化截图，再确认主控与通信状态。",
    "SYS-009": "记录站立测试的数据与关键证据，策略由人员在机器人端手动启动。",
    "SYS-010": "行走、跑步分别记录，保留各自的原始类型与测试时长。",
    "SYS-011": "24轴使用UFO特技，策略由人员在机器人端手动启动。",
}
_HELP = {
    "SYS-001": "在硬件自检页点击“一键执行17项基础自检”。原14项、IMU连续数据、IMU固件版本、BMS重启恢复依次运行，中文逐项结果自动保存。已经完成的自检也可在原工具中关联当前SN。",
    "SYS-002": "上位机只记录实际观察，不发送灯板控制命令。灯板已亮并不代表其他硬件项目通过。",
    "SYS-003": "固件版本按实际读取值记录；没有定义允许版本时，不自动判定固件合规。固件与参数共用现有检查窗口。",
    "SYS-004": "仅更新人员选择且有差异的KP、KD、POS、SPD、TOR编码范围。更新前保存原值，逐项读回；不改ID、零位、CAN超时或其他Pn，不使能、不发送动作。操作前停止deploy、标零及其他电机工具。",
    "SYS-012": "本页只保存人员点击的通过／不通过结论，不发送对频、急停或电源命令。",
    "SYS-005": "前往“角度与标定”中的现有标零流程。工具使用设备设置中的主界面连接，跳转只定位入口，不自动执行。返回后确认完成或中断；逐电机写零结果仍需后续标定检查。",
    "SYS-006": "工具使用设备设置中的主界面连接，跳转不自动执行。腰部插销确认、髋pitch零位及其他关节检查按现有流程进行。逐关节结果在报告中查看；跳过、未测不算合格。也可导入已有检查JSON。",
    "SYS-007": "独立急停按钮切断电机电源，主控应保持供电；电机反馈中断不能混同主控掉电。本页只记录人工结论，不发送急停或电源命令；本项不单独要求两小时。",
    "SYS-008": "要求主控全程不断电、无重启，换电后供电、BMS通信及运行正常。允许全程连接网线；SOC变化不能自动证明已换电或通过。缺失、过期不补零，断流标未知；缺少SOC仍可记录人工结论。",
}


class FactoryWorkbench(QWidget):
    """One case per page; report status comes only from already loaded rows."""

    def __init__(self, host, record):
        super().__init__(host)
        self.host = host
        self.setObjectName("factoryWorkbench")
        self.current_code = ""
        self._rows = []
        self._rows_sn = ""
        self._report_signature = None
        self._busy = False
        self._walk_type = "walking"
        self.case_items = {}
        self.case_pages = {}
        self.status_labels = {}
        self.connection_labels = {}
        self.feedback_labels = {}
        self.evidence_labels = {}
        self.motor_confirmation_buttons = {}
        self.motor_confirmation_labels = {}
        self.motion_slots = {}
        self.catalog = {row["code"]: row for row in test_catalog(host.model.currentData())}
        host.tool_buttons = {}
        host.motion_record_box = record
        record.setParent(self)
        record.hide()
        record.setTitle("")
        record.setObjectName("workbenchRecord")

        # These existing controls keep their callbacks and data lifetime. Only
        # their visual parent changes; no recorder or connection is recreated.
        host.functional_check_box.setParent(self)
        host.functional_check_box.hide()
        record.layout().removeWidget(host.manual_deploy_guide)
        host.manual_deploy_guide.setParent(self)
        host.manual_deploy_guide.hide()
        for button in record.findChildren(QPushButton):
            button.setObjectName("workbenchQuiet")
        host.start_button.setObjectName("workbenchPrimary")
        host.motion_notice.setTextFormat(Qt.TextFormat.PlainText)
        host.remote_table.setMinimumHeight(160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter)
        navigation = QFrame()
        navigation.setObjectName("workbenchPane")
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(14, 16, 14, 12)
        nav_layout.addWidget(_label("测试清单", "workbenchSection"))
        self.tree = QTreeWidget()
        self.tree.setObjectName("workbenchTree")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setMinimumWidth(265)
        nav_layout.addWidget(self.tree)
        self.pages = QStackedWidget()
        self.pages.setObjectName("workbenchPages")
        self.pages.setMinimumWidth(0)
        self.splitter.addWidget(navigation)
        self.splitter.addWidget(self.pages)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([305, 1000])

        for category, title in CATEGORY_NAMES.items():
            section = QTreeWidgetItem([title, ""])
            section.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = QFont()
            font.setBold(True)
            section.setFont(0, font)
            section.setSizeHint(0, QSize(0, 42))
            self.tree.addTopLevelItem(section)
            for row in self.catalog.values():
                if row["category"] != category:
                    continue
                code = row["code"]
                item = QTreeWidgetItem([f"{code}  {_SHORT_NAMES.get(code, row['name'])}", "未测"])
                item.setData(0, Qt.ItemDataRole.UserRole, code)
                item.setSizeHint(0, QSize(0, 43))
                section.addChild(item)
                self.case_items[code] = item
                self._build_page(row)
            section.setExpanded(True)
        self.tree.currentItemChanged.connect(self._tree_changed)
        host.notice.text_changed.connect(self._notice_changed)
        self.setStyleSheet(_STYLE)
        self.select_case("SYS-001")
        self.refresh_status([])

    @property
    def current_page(self):
        return self.pages.currentWidget()

    def _button(self, layout, text, action, *, primary=False):
        button = self.host._button(layout, text, action)
        button.setObjectName("workbenchPrimary" if primary else "workbenchQuiet")
        button.setMinimumHeight(35)
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        return button

    def _disclosure(self, layout, text, content):
        toggle = _Disclosure(text, content)
        content.hide()
        layout.addWidget(toggle)
        layout.addWidget(content)

    def _build_page(self, row):
        host, code = self.host, row["code"]
        page = QFrame()
        page.setObjectName("workbenchPane")
        body = QVBoxLayout(page)
        body.setContentsMargins(24, 22, 24, 24)
        body.setSpacing(14)
        top = QHBoxLayout()
        top.addWidget(_label(f"{code} / {CATEGORY_NAMES[row['category']]}", "workbenchEyebrow"), 1)
        badge = _label("未测", "workbenchBadge")
        badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        top.addWidget(badge)
        self.status_labels[code] = badge
        body.addLayout(top)
        body.addWidget(_label(row["name"], "workbenchTitle"))
        body.addWidget(_label(_DESCRIPTIONS[code], "workbenchMuted"))
        actions = QHBoxLayout()
        actions.setSpacing(10)
        if code in ("SYS-001", "SYS-005", "SYS-006"):
            target, caption = {
                "SYS-001": ("board", "前往基础自检（17项）"),
                "SYS-005": ("calibration", "前往标定"),
                "SYS-006": ("calibration_check", "前往标定检查"),
            }[code]
            host.tool_buttons[target] = self._button(actions, caption,
                lambda _checked=False, route=target, item=row["item"]: host.tool_requested.emit(route, item), primary=True)
        elif code in ("SYS-003", "SYS-004"):
            button = self._button(actions,
                "查看电机固件版本…" if code == "SYS-003" else "SYS-004 · 电机参数检查 / 更新…",
                host.open_motor_parameters, primary=True)
            if code == "SYS-004":
                host.parameter_button = button
            else:
                host.firmware_button = button
        elif code == "SYS-002":
            host.light_confirm_button = self._button(actions, "SYS-002 · 确认灯板是否亮灯", host.confirm_light, primary=True)
        elif code in ("SYS-007", "SYS-008"):
            key = "remote_estop" if code == "SYS-007" else "battery_hot_swap"
            button = host.functional_buttons[key]
            button.setParent(page)
            button.setObjectName("workbenchPrimary")
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            actions.addWidget(button)
            button.show()
        if row["category"] != "MOTION":
            actions.addStretch()
            body.addLayout(actions)
        body.addWidget(_label("01 前往测试工具  →  02 在工具内执行  →  03 返回当前SN查看结果"
            if code in ("SYS-001", "SYS-003", "SYS-004", "SYS-005", "SYS-006")
            else "01 现场执行测试  →  02 记录实际结果  →  03 查看与导出报告"
            if row["category"] != "MOTION" else "01 开始记录  →  02 机器人端手动启动策略  →  03 读取与保存数据", "workbenchHint"))
        if row["category"] == "MOTION":
            connection = _label("未连接机器人", "workbenchConnection")
            self.connection_labels[code] = connection
            body.addWidget(connection)
            if code == "SYS-011":
                self.stunt_notice = _label("", "workbenchMuted")
                body.addWidget(self.stunt_notice)
            slot = QWidget()
            slot_layout = QVBoxLayout(slot)
            slot_layout.setContentsMargins(0, 0, 0, 0)
            body.addWidget(slot)
            self.motion_slots[code] = slot_layout
        else:
            body.addWidget(_label("测试返回后的确认", "workbenchSection"))
            evidence = _label("尚未记录结果。", "workbenchMuted")
            body.addWidget(evidence)
            self.evidence_labels[code] = evidence
            if code in ("SYS-003", "SYS-004"):
                confirmation_note = _label("人工复核：尚未确认。", "workbenchSection")
                body.addWidget(confirmation_note)
                self.motor_confirmation_labels[code] = confirmation_note
                confirmation = QHBoxLayout()
                self.motor_confirmation_buttons[code] = {}
                for verdict, caption in (("通过", "确认无问题，标记通过"), ("不通过", "标记不通过")):
                    button = self._button(confirmation, caption,
                        lambda _checked=False, value=code, result=verdict: host.confirm_motor_check(value, result),
                        primary=verdict == "通过")
                    button.setAccessibleName(f"{code} {caption}")
                    self.motor_confirmation_buttons[code][verdict] = button
                confirmation.addStretch()
                body.addLayout(confirmation)
                body.addWidget(_label("先读取检查结果，再点击确认。结论保存到当前SN，并进入报告。", "workbenchMuted"))
            elif code == "SYS-002":
                host.light_stage_note = _label("灯板现场确认：尚未记录。")
                body.addWidget(host.light_stage_note)
            elif code == "SYS-005":
                host.calibration_confirmation_note = _label("整机标定：尚未确认。")
                body.addWidget(host.calibration_confirmation_note)
                self._button(body, "标定结束，确认状态", host.confirm_calibration)
            elif code == "SYS-006":
                host.calibration_stage_note = _label("标定检查：尚未关联；先选SN，已有结果可导入。")
                body.addWidget(host.calibration_stage_note)
                self._button(body, "导入已有标定检查 JSON", host.import_calibration_check)
            elif code == "SYS-012":
                host.estop_pairing_note = _label("未测试；请先选择整机SN。")
                body.addWidget(host.estop_pairing_note)
                host.estop_pairing_box = page
                pairing = QHBoxLayout()
                host.estop_pairing_buttons = {}
                for value, caption in (("pass", "通过"), ("fail", "不通过")):
                    button = self._button(pairing, caption,
                        lambda _checked=False, status=value: host.record_estop_pairing(status), primary=value == "pass")
                    button.setAccessibleName(f"{code} 远程急停开关对频测试 {caption}")
                    host.estop_pairing_buttons[value] = button
                pairing.addStretch()
                body.addLayout(pairing)
        feedback = _label("", "workbenchFeedback")
        feedback.hide()
        body.addWidget(feedback)
        self.feedback_labels[code] = feedback
        results = QHBoxLayout()
        self._button(results, "查看结果 / 添加附件", lambda _checked=False, value=code: host.show_case_result(value))
        if code == "SYS-001":
            host.tool_buttons["hardware_notes"] = self._button(results, "查看灯板 / 电机参数说明",
                lambda: host.tool_requested.emit("hardware_notes", 1))
        results.addStretch()
        body.addLayout(results)
        help_widget = _label(_HELP.get(code, host.manual_deploy_guide.text()), "workbenchHelp")
        self._disclosure(body, "操作说明、通过标准与连接要求", help_widget)
        body.addStretch(1)
        self.case_pages[code] = page
        self.pages.addWidget(page)

    def _tree_changed(self, current, _previous):
        if current is not None:
            code = current.data(0, Qt.ItemDataRole.UserRole)
            if code:
                self.select_case(code)

    def select_case(self, code):
        busy = self._busy or self.host.busy or getattr(getattr(self.host, "result_summary", None), "busy", False)
        if code not in self.case_pages or (busy and code != self.current_code):
            return False
        host, row = self.host, self.catalog[code]
        self.current_code = code
        host.selected_stage = row["item"]
        if self.tree.currentItem() is not self.case_items[code]:
            with QSignalBlocker(self.tree):
                self.tree.setCurrentItem(self.case_items[code])
        self.pages.setCurrentWidget(self.case_pages[code])
        if row["category"] == "MOTION":
            desired = {"SYS-009": "standing", "SYS-010": self._walk_type, "SYS-011": "stunt"}[code]
            with QSignalBlocker(host.test_type):
                host.test_type.setCurrentIndex(host.test_type.findData(desired))
            self.motion_slots[code].addWidget(host.motion_record_box)
            host.motion_record_box.show()
            host.motion_result_button.setText(f"保存{row['name']}测试结果")
        self._notice_changed(host.notice.text())
        self.update_model_controls()
        return True

    def select_stage(self, number):
        code = {1: "SYS-001", 2: "SYS-005", 3: "SYS-006", 5: "SYS-011"}.get(number)
        if number == 4:
            code = "SYS-009"
        return self.select_case(code) if code else False

    def select_category(self, category):
        code = next((code for code, row in self.catalog.items() if row["category"] == category), None)
        return self.select_case(code) if code else False

    def motion_type_changed(self):
        kind = self.host.test_type.currentData()
        if kind in ("walking", "running"):
            self._walk_type = kind
        code = MOTION_CASE_CODES.get(kind)
        if code:
            self.select_case(code)

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.tree.setEnabled(not self._busy)

    def update_model_controls(self):
        host = self.host
        supported = host.model.currentData() != 28
        self.stunt_notice.setText("24轴使用UFO特技。" if supported else "28轴不适用UFO特技，无需开始记录；已有历史结果仍可查看。")
        enabled = not self._busy and not host.busy and not host.closing
        host.start_button.setEnabled(enabled and not (self.current_code == "SYS-011" and not supported))
        for button in host.estop_pairing_buttons.values():
            button.setEnabled(enabled and bool(host.sn))
        self._update_confirmation_controls()
        self.refresh_status()

    def _update_confirmation_controls(self):
        idle = not self._busy and not self.host.busy and not self.host.closing
        rows = {row.get("row_key"): row for row in self._rows} if self._rows_sn == self.host.sn else {}
        for code, buttons in self.motor_confirmation_buttons.items():
            available = idle and bool(self.host.sn) and bool(rows.get(code, {}).get("evidence"))
            for button in buttons.values():
                button.setEnabled(available)
                button.setToolTip("保存本项人工结论" if available else "先打开SN并读取本项检查结果")

    def _notice_changed(self, text):
        for code, label in self.feedback_labels.items():
            if code == self.current_code:
                label.setText("最近操作提示：" + text)
                label.setVisible(bool(text) and self.catalog[code]["category"] != "MOTION")
            else:
                label.hide()

    def refresh_status(self, rows=None):
        if rows is not None:
            self._rows = list(rows)
            self._rows_sn = self.host.sn
            self._report_signature = None
        signature = (self.host.sn, self.host.model.currentData())
        if self._report_signature == signature:
            self._refresh_connection()
            return
        self._report_signature = signature
        by_code = {row.get("case_code"): row for row in self._rows
                   if row.get("level") != "子项" and self._rows_sn == self.host.sn}
        colors = {"通过": "#20815b", "不通过": "#b33b46", "待复核": "#986923", "中断": "#986923"}
        for code, item in self.case_items.items():
            row = by_code.get(code, {}) if self.host.sn else {}
            verdict = str(row.get("verdict") or "未测试")
            if code == "SYS-011" and self.host.model.currentData() == 28:
                verdict = "不适用"
            simulated = "simulated" in str(row.get("source_kind", "")).lower()
            text = "未测" if verdict == "未测试" else verdict
            if simulated:
                text += " · 模拟"
            color = colors.get(verdict, "#7b899e")
            item.setText(1, text)
            item.setForeground(1, QColor(color))
            item.setToolTip(0, f"{self.catalog[code]['name']}\n{row.get('actual_result') or '尚未保存结果'}")
            self.status_labels[code].setText(text)
            self.status_labels[code].setStyleSheet(f"color:{color};background:#f5f8fc;border:1px solid #dce4ef;border-radius:5px;padding:6px 10px;")
            if code in self.motor_confirmation_labels:
                confirmed = row.get("edited") and verdict in ("通过", "不通过")
                self.motor_confirmation_labels[code].setText(f"已保存人工结论：{verdict}。" if confirmed else "人工复核：尚未确认。")
            if code in self.evidence_labels:
                summary = str(row.get("actual_result") or "尚未记录结果。")
                if not self.host.sn:
                    summary = "请先打开或新建SN，测试结果将保存到该样品。"
                elif simulated:
                    summary = "SIMULATED · 模拟数据，非实机验收。\n" + summary
                self.evidence_labels[code].setToolTip(summary)
                if len(summary) > 260:
                    summary = summary[:260].rstrip() + "… 完整结果在报告中查看。"
                self.evidence_labels[code].setText(summary)
        self._update_confirmation_controls()
        self._refresh_connection()

    def _refresh_connection(self):
        frame = self.host.live_frame
        received = self.host.live_frame_received
        if frame is None or received is None:
            text = "未连接机器人 · 请在设备设置中连接"
        else:
            age = time.monotonic() - received
            robot = getattr(frame, "robot_id", "") if not isinstance(frame, dict) else frame.get("robot_id", "")
            source = getattr(frame, "source_kind", "") if not isinstance(frame, dict) else frame.get("source_kind", "")
            upload = getattr(frame, "upload", {}) if not isinstance(frame, dict) else frame.get("upload", {})
            connected = upload.get("collector_connected") if isinstance(upload, dict) else getattr(upload, "collector_connected", None)
            if age > 3 or connected is False:
                text = f"{robot or '机器人'} · 数据已过期，请核对连接"
            elif source == "simulated":
                text = f"{robot or '模拟来源'} · SIMULATED 模拟数据，非实机验收"
            else:
                text = f"{robot or '机器人'} · 正在接收数据 · ROS2来源仍待实机验收"
        for label in self.connection_labels.values():
            if label.text() != text:
                label.setText(text)


_STYLE = """
QWidget#factoryWorkbench { background:transparent; }
QWidget#factoryWorkbench QFrame#workbenchPane { background:white;border:1px solid #d7e1ef;border-radius:8px; }
QWidget#factoryWorkbench QTreeWidget#workbenchTree { background:white;border:none;outline:0;font-size:13px; }
QTreeWidget#workbenchTree::item { border-bottom:1px solid #edf1f7;padding:5px 4px; }
QTreeWidget#workbenchTree::item:selected { background:#e6edff;color:#2852c8;border-radius:4px; }
QWidget#factoryWorkbench QLabel#workbenchTitle { color:#172c49;font-size:24px;font-weight:700; }
QWidget#factoryWorkbench QLabel#workbenchSection { color:#314967;font-weight:600; }
QWidget#factoryWorkbench QLabel#workbenchEyebrow { color:#4168d6;font-weight:600;font-size:13px; }
QWidget#factoryWorkbench QLabel#workbenchMuted { color:#687e9b; }
QWidget#factoryWorkbench QLabel#workbenchHint { color:#4d6d98;background:#edf3ff;border:1px solid #dbe6fa;border-radius:6px;padding:12px; }
QWidget#factoryWorkbench QLabel#workbenchConnection { color:#627a96;font-size:12px; }
QWidget#factoryWorkbench QLabel#workbenchFeedback { color:#3c608b;background:#f3f7fd;border:1px solid #dbe5f4;border-radius:5px;padding:9px; }
QWidget#factoryWorkbench QLabel#workbenchHelp { color:#617791;padding:5px 12px; }
QWidget#factoryWorkbench QPushButton#workbenchQuiet { background:white;color:#3f5e87;border:1px solid #cbd8ec;border-radius:5px;padding:7px 14px;font-weight:500; }
QWidget#factoryWorkbench QPushButton#workbenchQuiet:hover { background:#f0f5fd; }
QWidget#factoryWorkbench QPushButton#workbenchPrimary { background:#3559e6;color:white;border:1px solid #3559e6;border-radius:5px;padding:7px 16px;font-weight:600; }
QWidget#factoryWorkbench QPushButton#workbenchPrimary:hover { background:#294bce; }
QWidget#factoryWorkbench QPushButton:disabled { background:#edf1f7;color:#97a4b6;border-color:#d7e0ed; }
QWidget#factoryWorkbench QPushButton#workbenchDisclosure { background:#f7f9fd;color:#5b7392;border:1px solid #dce5f1;border-radius:5px;text-align:left;padding:10px 13px;font-weight:500; }
QWidget#factoryWorkbench QGroupBox#workbenchRecord { border:none;background:transparent;margin:0;padding:0; }
QWidget#factoryWorkbench QSplitter::handle { background:transparent;width:6px; }
"""
