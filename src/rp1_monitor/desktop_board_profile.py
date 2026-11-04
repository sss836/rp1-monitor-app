"""Edit shared board requirements in native controls, without JSON editing."""

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .factory_board_profile import BoardTestProfile


class BoardProfileDialog(QDialog):
    def __init__(self, profile, save, parent=None):
        super().__init__(parent)
        self.save = save
        self.profile = BoardTestProfile.model_validate(profile)
        self.setWindowTitle("RP1 主控测试标准 · 24/28DOF通用")
        self.resize(850, 740)
        layout = QVBoxLayout(self)
        note = QLabel("主控要求共用一份；24/28DOF仅切换电机清单。保存后用于后续测试；已有SN报告保留当时的标准。")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.ethernet = QLineEdit("、".join(self.profile.ethernet_interfaces))
        form.addRow("必需网口（多个用逗号分隔）", self.ethernet)
        self.serial = QLineEdit("、".join(self.profile.serial_ports))
        form.addRow("额外必需串口（完整/dev路径）", self.serial)
        self.follow_bms = QCheckBox("自动要求BMS配置中使用的串口；BMS走CAN时不要求串口")
        self.follow_bms.setChecked(self.profile.follow_bms_serial)
        form.addRow(self.follow_bms)
        self.bms_versions = QLineEdit("、".join(self.profile.bms_firmware_versions))
        form.addRow("允许BMS固件版本（留空仅核对读取）", self.bms_versions)
        self.imu_versions = QLineEdit("、".join(self.profile.imu_firmware_versions))
        form.addRow("允许IMU固件版本（留空待确认）", self.imu_versions)
        self.motor_versions = QLineEdit("、".join(self.profile.motor_firmware_versions))
        form.addRow("允许电机固件版本", self.motor_versions)
        layout.addLayout(form)
        self.can_note = QLabel("CAN清单：读到can0、can1、can2、can3四路就通过；状态、模式、波特率仅记录。IMU连续数据单独检查。")
        self.can_note.setWordWrap(True)
        layout.addWidget(self.can_note)
        layout.addWidget(QLabel("必需USB设备：不固定总记录数，不把系统根Hub算作外设；其他额外设备不影响判定。"))
        self.usb = QTableWidget(len(self.profile.usb_devices), 5)
        self.usb.setHorizontalHeaderLabels(["用途", "厂商ID", "产品ID", "数量", "最低速率 / Mb/s"])
        self.usb.horizontalHeader().setStretchLastSection(True)
        for index, row in enumerate(self.profile.usb_devices):
            for column, value in enumerate((row.label, row.vendor_id, row.product_id, row.count, row.min_speed_mbps)):
                self.usb.setItem(index, column, QTableWidgetItem(str(value)))
        layout.addWidget(self.usb)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save_profile)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _names(text):
        return [value.strip() for value in text.replace("、", ",").replace("，", ",").split(",") if value.strip()]

    def values(self):
        values = self.profile.model_dump(mode="json")
        values.update(ethernet_interfaces=self._names(self.ethernet.text()), serial_ports=self._names(self.serial.text()),
                      follow_bms_serial=self.follow_bms.isChecked(), bms_firmware_versions=self._names(self.bms_versions.text()),
                      motor_firmware_versions=self._names(self.motor_versions.text()),
                      imu_firmware_versions=self._names(self.imu_versions.text()), usb_devices=[])
        for index in range(self.usb.rowCount()):
            label, vendor, product, count, speed = (self.usb.item(index, column).text().strip() for column in range(5))
            values["usb_devices"].append(dict(label=label, vendor_id=vendor.lower(), product_id=product.lower(), count=int(count), min_speed_mbps=float(speed)))
        return BoardTestProfile.model_validate(values).model_dump(mode="json")

    def save_profile(self):
        try:
            self.save(self.values())
        except (ValueError, OSError) as exc:
            self.error.setText(f"标准未保存：{exc}")
            return
        self.accept()
