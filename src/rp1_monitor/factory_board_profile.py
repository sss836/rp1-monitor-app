"""Shared RP1 board criteria, independent of TUI verdicts and motor count."""

import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .board_check_view import BoardCheckView, board_check_view

IMAGE_IDENTITY_203 = {
    "BOARD": "robopi2",
    "VENDOR": "roboparty-os",
    "KERNEL_TARGET": "current",
    "VERSION": "26.11.0-trunk-2.0.3",
    "REVISION": "26.11.0-trunk-2.0.3",
    "IMAGE_UUID": "e8713364-c43f-4b1b-b816-3ebf2939d39c",
    "BUILD_REPOSITORY_COMMIT": "4b8460569-dirty",
}


class USBRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    vendor_id: str = Field(pattern=r"^[0-9a-f]{4}$")
    product_id: str = Field(pattern=r"^[0-9a-f]{4}$")
    count: int = Field(ge=1, le=16)
    min_speed_mbps: float = Field(gt=0, le=100000, allow_inf_nan=False)


class BoardTestProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=100)
    ethernet_interfaces: list[str] = Field(min_length=1, max_length=8)
    can_interfaces: dict[str, int] = Field(min_length=1, max_length=16)
    can_bitrates: dict[str, int] = Field(default_factory=dict, max_length=16)
    usb_devices: list[USBRequirement] = Field(min_length=1, max_length=32)
    serial_ports: list[str] = Field(default_factory=list, max_length=32)
    follow_bms_serial: bool = True
    bms_firmware_versions: list[str] = Field(default_factory=list, max_length=32)
    motor_firmware_versions: list[str] = Field(default_factory=list, max_length=32)
    imu_firmware_versions: list[str] = Field(default_factory=list, max_length=32)
    image_identity: dict[str, str] = Field(default_factory=lambda: dict(IMAGE_IDENTITY_203))

    @field_validator("ethernet_interfaces", "serial_ports", "bms_firmware_versions", "motor_firmware_versions", "imu_firmware_versions")
    @classmethod
    def unique_names(cls, values):
        if len(values) != len(set(values)) or any(not value.strip() or len(value) > 100 or any(ord(c) < 32 for c in value) for value in values):
            raise ValueError("配置值不能为空、重复或包含控制字符")
        return values

    @model_validator(mode="after")
    def valid_can(self):
        if (self.image_identity.keys() != IMAGE_IDENTITY_203.keys()
                or any(not value.strip() or len(value) > 100 or any(ord(c) < 32 for c in value)
                       for value in self.image_identity.values())):
            raise ValueError("镜像身份基准必须包含完整有效字段")
        if any(not name or len(name) > 32 or not name.replace("_", "").isalnum() or mtu not in (16, 72) for name, mtu in self.can_interfaces.items()):
            raise ValueError("CAN接口名称无效，MTU应为16或72")
        if any(name not in self.can_interfaces or not 0 < value <= 10000000 for name, value in self.can_bitrates.items()):
            raise ValueError("CAN波特率必须绑定已有接口且为有效正数")
        ids = [(row.vendor_id, row.product_id) for row in self.usb_devices]
        if len(ids) != len(set(ids)):
            raise ValueError("USB设备标识不能重复，请用数量字段表示同型号多台设备")
        return self


def default_board_profile():
    return BoardTestProfile.model_validate_json((Path(__file__).parent / "data/factory-board-rp1.json").read_text())


def _outcome(readings, failures, missing, scope):
    if failures:
        return BoardCheckView("不通过", "；".join(failures), tuple(readings), "按上述差异处理后重测本项。")
    if missing:
        return BoardCheckView("未完成", "；".join(missing), tuple(readings), "补齐所列证据后重测；无需逐项人工确认。")
    return BoardCheckView("通过", scope, tuple(readings), "")


def _float(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def factory_board_view(identity, report, profile, reports=None):
    profile = BoardTestProfile.model_validate(profile)
    try:
        return _factory_board_view(identity, report, profile, reports)
    except (TypeError, KeyError, AttributeError, ValueError):
        return BoardCheckView("未完成", "报告字段格式不匹配，无法按内置标准判定。", (), "更新采集端或展开原始证据核对；原始结果已保留。")


def _factory_board_view(identity, report, profile, reports=None):
    """Apply explicit inventory criteria, preserving acquisition errors and raw data."""
    native = board_check_view(identity, report)
    if identity == "RP002" and report and report.get("result") in {"pass", "fail", "needs_review"}:
        details = report.get("details") or {}
        if report.get("result") == "fail" and not details.get("mismatches"):
            return native
        snapshot = details.get("snapshot") or {}
        readings, failures, missing = [], [], []
        for key, expected in profile.image_identity.items():
            actual = snapshot.get(key)
            readings.append(f"{key}：实际 {actual or '未知'}；工厂基准 {expected}")
            if not isinstance(actual, str) or not actual.strip():
                missing.append(f"镜像未提供 {key}")
            elif actual != expected:
                failures.append(f"镜像 {key} 与工厂基准不同")
        return _outcome(readings, failures, missing, f"镜像身份符合工厂基准 {profile.image_identity['REVISION']}。")
    if not report or report.get("result") not in {"pass", "needs_review"}:
        return native
    details, reports = report.get("details") or {}, reports or {}
    readings, failures, missing = [], [], []
    if identity == "IMU_FIRMWARE":
        actual = details.get("version")
        if not isinstance(actual, str) or not actual.strip():
            return _outcome([], [], ["IMU固件版本未读取到"], "")
        readings.append(f"IMU固件版本：{actual}；设备ID：{details.get('imu_id', '未知')}。")
        if not profile.imu_firmware_versions:
            return BoardCheckView("未完成", "已读到IMU固件版本；未设置允许版本，合格状态待确认。", tuple(readings), "在主控测试标准中设置此型号允许的IMU版本。")
        if actual not in profile.imu_firmware_versions:
            return _outcome(readings, [f"IMU固件版本 {actual} 不符合允许版本：{'、'.join(profile.imu_firmware_versions)}"], [], "")
        return _outcome(readings, [], [], "IMU固件版本符合已配置的允许版本。")
    if identity == "RP007":
        # RP007 is an inventory check. Older saved profiles can still contain
        # stricter CAN/IMU settings; they no longer gate this four-port check.
        observations = report.get("factory_observations") or {}
        extra = observations.get("can_interfaces") if isinstance(observations, dict) else None
        extra = extra if isinstance(extra, dict) else {}
        inventories = [details.get("interfaces"), extra.get("interfaces")]
        if not any(isinstance(rows, list) for rows in inventories):
            return _outcome([], [], ["报告未提供可识别的 CAN 接口清单"], "")
        interfaces = {
            row["name"]: row
            for rows in inventories if isinstance(rows, list)
            for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        required = ("can0", "can1", "can2", "can3")
        for name in required:
            row = interfaces.get(name)
            if row is None:
                failures.append(f"未读到 CAN 接口 {name}")
                continue
            readings.append(f"{name}：已识别。")
            fields = [f"{label} {row[key]}" for key, label in (("can_state", "状态"), ("mtu", "MTU"), ("bitrate", "波特率 / bit/s")) if row.get(key) is not None]
            if fields:
                readings.append(f"{name}：{'；'.join(fields)}（仅记录）。")
        readings.append(f"已读到 {sum(name in interfaces for name in required)}/4 路：can0、can1、can2、can3。")
        readings.append("本项只核对四路接口是否存在；状态、模式和波特率仅记录，IMU连续数据单独检查。")
        return _outcome(readings, failures, [], "已读到 can0、can1、can2、can3，4路 CAN 接口齐全。")
    inventory_key = {"RP003": "interfaces", "RP011": "devices", "RP029": "ports"}.get(identity)
    if inventory_key and not isinstance(details.get(inventory_key), list):
        return _outcome([], [], ["报告未提供可识别的设备清单，无法按内置标准判定"], "")
    if identity == "RP003":
        interfaces = {row.get("name"): row for row in details.get("interfaces", []) if isinstance(row, dict)}
        for name in profile.ethernet_interfaces:
            row = interfaces.get(name)
            if row is None:
                failures.append(f"缺少网口 {name}")
            elif not row.get("operstate") or str(row.get("carrier", "")) not in {"0", "1"}:
                missing.append(f"{name} 的运行或链路状态未提供")
            elif row["operstate"] != "up" or str(row["carrier"]) != "1":
                failures.append(f"{name} 未建立网线链路")
            else:
                readings.append(f"{name}：接口运行，已检测到物理链路。")
        return _outcome(readings, failures, missing, "所需网口已识别，已检测到物理链路。")
    if identity == "RP011":
        devices = [row for row in details.get("devices", []) if isinstance(row, dict)]
        for required in profile.usb_devices:
            found = [row for row in devices if str(row.get("vendor_id", "")).lower() == required.vendor_id and str(row.get("product_id", "")).lower() == required.product_id]
            count = len(found)
            readings.append(f"{required.label} {required.vendor_id}:{required.product_id}：{count}/{required.count} 台。")
            if count != required.count:
                failures.append(f"{required.label} 数量应为 {required.count}，实际 {count}")
            for row in found:
                speed = _float(row.get("speed_mbps"))
                if speed is None:
                    missing.append(f"{required.label} 连接速率未提供")
                elif speed < required.min_speed_mbps:
                    failures.append(f"{required.label} 速率 {speed:g}，要求至少 {required.min_speed_mbps:g} Mb/s")
                else:
                    readings.append(f"{required.label}：{speed:g} Mb/s，速率符合要求。")
        return _outcome(readings, failures, missing, "必需 USB 设备齐全，数量与连接速率符合配置。")
    if identity == "RP029":
        ports = {f"/dev/{name}" if not str(name).startswith("/dev/") else str(name) for name in details.get("ports", [])}
        required = set(profile.serial_ports)
        if profile.follow_bms_serial:
            bms = reports.get("RP020") or {}
            bms_details = bms.get("details") or {}
            if bms.get("result") != "pass":
                missing.append("先取得 BMS 端口检查结果，自动确定其串口要求")
            elif bms_details.get("port_type") == "serial" and bms_details.get("port"):
                required.add(bms_details["port"])
            elif bms_details.get("port_type") == "can":
                readings.append("BMS 使用 CAN；本项不要求 BMS 串口。")
            else:
                missing.append("BMS 端口类型或路径未提供")
        for name in sorted(required):
            if name not in ports:
                failures.append(f"缺少必需串口 {name}")
            else:
                readings.append(f"{name}：已识别。")
        readings.append(f"共识别 {len(ports)} 个串口；额外串口不影响判定。")
        readings.append("本项仅检查端口存在；实际收发由外设功能测试验证。")
        return _outcome(readings, failures, missing, "配置要求的串口存在性检查通过。")
    if identity == "RP022" and profile.bms_firmware_versions:
        actual = details.get("software_version")
        if not actual:
            missing.append("BMS 固件版本未提供")
        elif actual not in profile.bms_firmware_versions:
            failures.append(f"BMS 固件版本 {actual} 不符合允许版本：{'、'.join(profile.bms_firmware_versions)}")
        else:
            readings.append(f"BMS 固件版本 {actual} 符合允许版本。")
        return _outcome(readings, failures, missing, "BMS 固件版本核对通过。")
    return native
