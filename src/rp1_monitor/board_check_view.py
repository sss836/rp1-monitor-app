"""Human-readable views of TUI evidence; never change the original verdict."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BoardCheckView:
    label: str
    summary: str
    readings: tuple[str, ...]
    action: str

    def text(self):
        parts = [f"测试结果：{self.label}", self.summary]
        if self.readings:
            parts.extend(("", "实际检查结果：", *self.readings))
        if self.action:
            parts.extend(("", f"处理建议：{self.action}"))
        return "\n".join(parts)


SCOPES = {
    "RP001": "主控板当前温度低于设定门槛。",
    "RP002": "镜像身份与内置基准一致。",
    "RP003": "接口清单已读取；尚未配置本机应有网口及链路判定规则。",
    "RP005": "必需基础软件包已安装，已读取相关软件版本。",
    "RP007": "CAN 清单已读取；尚未完成本机接口配置与实际通信判定。",
    "RP008": "IMU 设备命名规则配置正确。",
    "RP011": "USB 清单已读取；尚未配置本机必需设备、数量及速率要求。",
    "RP012": "USB 3.0 Hub 已识别，连接速率符合要求。",
    "RP013": "已识别 USB-CAN，已发现绑定 gs_usb 的 CAN 接口。",
    "RP020": "BMS 类型已配置，通信端口存在。",
    "RP021": "BMS 服务正常，近期有本次启动的数据日志。",
    "RP022": "已读取 BMS 固件及硬件版本。",
    "RP029": "串口清单已读取；尚未配置本机必需端口及实际收发判定规则。",
    "RP034": "已读取板卡序列号。",
}

ACTIONS = {
    "RP001": "核对温度读数、散热和温度传感器读取权限。",
    "RP002": "核对实际镜像与本批次基准；正常更换镜像后应更新允许基准。",
    "RP003": "配置本机应有网口及连接要求后自动判定；无需反复运行清单检查。",
    "RP005": "核对必需软件包及安装状态；版本一致性需对照出厂允许版本表。",
    "RP007": "重新读取接口清单，核对是否能识别 can0、can1、can2、can3；IMU连续数据单独检查。",
    "RP008": "核对 roboparty-base 和 IMU 命名规则文件，再进行 IMU 连续数据检查。",
    "RP011": "配置本机必需 USB 设备及速率；系统根 Hub 不作为外设逐项确认。",
    "RP012": "核对本机是否配置该 Hub，以及连接、供电和协商速率。",
    "RP013": "核对 USB-CAN 型号、USB 连接、gs_usb 驱动绑定和接口；前置项以本次插件报告为准。",
    "RP020": "核对 BMS 类型、端口配置和对应设备。",
    "RP021": "先核对 BMS 端口，再检查服务状态、近期数据和日志读取权限。",
    "RP022": "先核对 BMS 服务及电池类型，再检查版本来源；读不到版本不等于版本错误。",
    "RP029": "按实际用途配置必需串口，并通过对应外设或工装验证通信。",
    "RP034": "核对设备树或系统是否提供板卡序列号；不要用整机 SN 填补缺失值。",
}

# Known plugin messages are translated here, leaving the source report untouched.
MESSAGES = (
    ("Blocked by dependencies", "前置检查未通过，本项没有执行。"),
    ("Missing capability", "缺少所需设备或系统能力，本项没有执行。"),
    ("timed out", "检查超时，未取得完整结果。"),
    ("Factory image identity mismatch", "实际镜像身份与插件内置基准不一致。"),
    ("Cannot read factory image identity", "无法读取镜像身份文件。"),
    ("Invalid release field", "镜像身份字段格式错误。"),
    ("Invalid or duplicate release field", "镜像身份字段格式错误或重复。"),
    ("roboparty-base is not installed", "未安装或无法查询 roboparty-base 软件包。"),
    ("HiPNUC udev rule is missing", "IMU 命名规则缺失或未由指定软件包提供。"),
    ("does not name can_hipnuc", "IMU 命名规则没有配置 can_hipnuc。"),
    ("No readable thermal zone", "未取得可读取的主控温度。"),
    ("No non-loopback network interfaces", "未发现网络接口。"),
    ("No CAN interfaces", "未发现当前插件支持扫描的 CAN 接口。"),
    ("No USB devices", "未发现可读取的 USB 设备。"),
    ("GSUSB device 1209:2323 is not enumerated", "未识别到指定的 USB-CAN 设备 1209:2323。"),
    ("BMS configuration is not installed", "未找到 BMS 配置文件。"),
    ("BMS configuration is missing", "未找到 BMS 配置文件。"),
    ("BMS_TYPE or BMS_PORT is not configured", "BMS 类型或通信端口未配置。"),
    ("Configured BMS port is missing", "配置指定的 BMS 端口不存在。"),
    ("bms.service is not running cleanly", "BMS 服务未处于正常运行状态。"),
    ("has no current invocation ID", "无法确认 BMS 服务本次启动身份。"),
    ("has no live data in the last 30 seconds", "BMS 服务有运行状态，但最近 30 秒没有匹配的数据日志。"),
    ("No BMS version in the current", "BMS 服务本次启动日志中没有匹配的版本信息。"),
    ("No GFCAN BMS version frame", "监听窗口内未收到 BMS 版本帧。"),
    ("has no supported version source", "当前 BMS 类型尚无受支持的版本读取方式。"),
    ("zero firmware and hardware versions", "BMS 固件和硬件版本均为零，版本未确认。"),
    ("No serial devices", "未发现当前插件支持识别的串口设备。"),
    ("Board serial number not available", "系统未提供可读取的板卡序列号。"),
)


def _show(value):
    return "未提供" if value is None or value == "" or isinstance(value, (dict, list)) else str(value)


def _rows(details, key):
    values = details.get(key)
    return [row for row in values if isinstance(row, dict)] if isinstance(values, list) else []


def _readings(identity, details):
    lines = []
    if identity == "IMU_CONTINUOUS":
        for name, stream in (details.get("streams") or {}).items():
            lines.append(f"{name}：{stream['count']}帧，{stream['hz']:.1f} Hz，最大间隔 {stream['max_gap_s'] * 1000:.1f} ms。")
        if "invalid_frames" in details:
            lines.append(f"采样 {details['duration_s']:g} 秒；每路至少50 Hz、最大间隔200 ms；无效目标帧 {details['invalid_frames']}。")
        lines.append("仅验证连续接收及数据有效性，不验证安装方向或绝对姿态精度。")
    elif identity == "IMU_FIRMWARE":
        if details.get("version"):
            lines.append(f"IMU固件版本：{details['version']}；设备ID：{details.get('imu_id', '未知')}。")
        if "query_sent" in details:
            lines.append("已发送版本读取请求。" if details["query_sent"] else "未发送版本读取请求。")
    elif identity == "BMS_RECOVERY":
        if "recovery_s" in details:
            lines.append(f"重启命令完成后 {details['recovery_s']:.1f} 秒恢复数据（要求30秒内）。")
        for key, title in (("before", "重启前"), ("after", "重启后")):
            state = details.get(key) or {}
            if state:
                lines.append(f"{title}：{state.get('ActiveState', '未知')} / {state.get('SubState', '未知')}；启动实例 {state.get('InvocationID', '未知')}。")
        lines.extend((details.get("recent_data_lines") or [])[-5:])
    elif identity == "RP001":
        for zone in _rows(details, "zones"):
            lines.append(f"{_show(zone.get('sensor') or zone.get('zone'))}：{_show(zone.get('temperature_c'))} ℃")
        if "limit_c" in details:
            lines.append(f"插件通过门槛：最高可读温区低于 {_show(details['limit_c'])} ℃")
    elif identity == "RP002":
        mismatches = details.get("mismatches") or {}
        for key, mismatch in mismatches.items() if isinstance(mismatches, dict) else []:
            if not isinstance(mismatch, dict):
                continue
            lines.append(f"{key}：实际 {_show(mismatch.get('actual'))}；基准 {_show(mismatch.get('expected'))}")
        if not lines and details.get("snapshot"):
            lines.append(f"已读取 {len(details['snapshot'])} 个镜像身份字段。")
    elif identity in {"RP003", "RP007"}:
        interfaces = _rows(details, "interfaces")
        if interfaces:
            lines.append(f"已识别 {len(interfaces)} 个接口。")
        for row in interfaces:
            state = {"up": "运行", "down": "未运行", "unknown": "状态未明确"}.get(row.get("operstate"), "状态未提供")
            fields = [f"{_show(row.get('name'))}：{state}"]
            if identity == "RP003" and not str(row.get("name", "")).startswith("can"):
                fields.append({"1": "检测到物理链路", "0": "未检测到物理链路"}.get(str(row.get("carrier")), "链路信息未提供"))
            if identity == "RP007":
                fields.extend((f"CAN 状态 {_show(row.get('can_state'))}", f"波特率 {_show(row.get('bitrate'))} bit/s"))
                if row.get("query_error"):
                    fields.append("详细状态读取失败，见原始证据")
            lines.append("；".join(fields))
    elif identity == "RP005":
        packages = _rows(details, "packages")
        if packages:
            lines.append(f"已读取 {len(packages)} 个相关软件包；必需基础包：{_show(details.get('required'))}。")
        lines.extend(f"{_show(row.get('name'))}：{_show(row.get('version'))}" for row in packages)
    elif identity == "RP008":
        if details.get("rule"):
            lines.append(f"命名规则：{details['rule']}")
        if details.get("package"):
            lines.append(f"所属软件包：{details['package']}")
    elif identity in {"RP011", "RP012", "RP013"}:
        devices = _rows(details, "hubs" if identity == "RP012" else "devices")
        roots = [row for row in devices if str(row.get("device", "")).startswith("usb") and str(row["device"])[3:].isdigit()]
        external = [row for row in devices if row not in roots]
        if devices:
            lines.append(f"已读取 {len(devices)} 条记录：下游设备 {len(external)} 条，系统根 Hub {len(roots)} 条。")
        for row in external:
            lines.append(f"{_show(row.get('name'))}：{_show(row.get('vendor_id'))}:{_show(row.get('product_id'))}；连接速率 {_show(row.get('speed_mbps'))} Mb/s")
            if identity == "RP013":
                lines.append(f"USB 设备：{_show(row.get('device'))}；USB 序列号（sysfs 描述符）：{_show(row.get('serial'))}")
                interfaces = _rows(row, "gs_usb_interfaces")
                names = [str(name) for interface in interfaces for name in interface.get("can_interfaces", [])]
                lines.append("CAN 接口：" + ("、".join(names) or "未生成"))
        if identity == "RP013" and devices:
            lines.append("本项验证驱动绑定与接口枚举；序列号读数不代表唯一性验证或 CAN 通信测试。")
        if roots:
            lines.append("系统根 Hub：" + "、".join(str(row["device"]) for row in roots) + "；不计作外部设备。")
    elif identity == "RP020":
        if details:
            lines.extend((f"BMS 类型：{_show(details.get('type'))}", f"通信端口：{_show(details.get('port'))}"))
    elif identity == "RP021":
        if details.get("ActiveState"):
            active = {"active": "已启动", "inactive": "未启动", "failed": "启动失败"}.get(details["ActiveState"], str(details["ActiveState"]))
            lines.append(f"BMS 服务：{active}；进程状态：{_show(details.get('SubState'))}")
        if "recent_data_lines" in details:
            lines.append(f"本次启动最近 30 秒保留的数据日志：{len(details['recent_data_lines'])} 条。")
    elif identity == "RP022":
        for key, label in (("software_version", "固件版本"), ("hardware_version", "硬件版本")):
            if key in details:
                lines.append(f"{label}：{_show(details[key])}")
    elif identity == "RP029":
        ports = details.get("ports", [])
        if ports:
            lines.append(f"已识别 {len(ports)} 个串口：" + "、".join(str(port) for port in ports))
            lines.append("本项仅识别端口，未打开串口或进行收发。")
    elif identity == "RP034" and details.get("serial"):
        lines.append(f"板卡序列号：{details['serial']}")
    return tuple(lines)


def board_check_view(identity, report=None):
    """Explain measured evidence without promoting inventories to factory passes."""
    if not report:
        return BoardCheckView("未检查", "尚未取得本项检查结果。", (), "连接机器人后运行本项；已有本地记录不会因此删除。")
    status = report.get("result")
    message = report.get("message", "")
    details = report.get("details") or {}
    try:
        readings = _readings(identity, details)
    except (TypeError, ValueError, KeyError, AttributeError):
        readings = ("测量详情格式与当前显示器不匹配；原始证据保留，可展开查看。",)
    if status == "pass" and identity == "IMU_FIRMWARE":
        label, summary, action = "未完成", "已读取IMU固件版本，需按允许版本判断合格状态。", "设置该型号允许的IMU版本。"
    elif status == "pass":
        label, summary, action = "通过", (message if identity in {"IMU_CONTINUOUS", "IMU_FIRMWARE", "BMS_RECOVERY"} else SCOPES.get(identity, "符合本项插件判据。")), ""
    elif status == "needs_review":
        label, summary, action = "未完成", SCOPES.get(identity, "已读取数据，但缺少明确的合格判定规则。"), ACTIONS.get(identity, "补充本机判定规则。")
        if identity not in {"RP003", "RP007", "RP011", "RP029"}:
            summary = "插件要求复核，本项尚未形成合格结论。"
    else:
        label = "不通过" if status == "fail" else "未完成"
        summary = next((text for pattern, text in MESSAGES if pattern in message), "")
        if not summary and any("\u4e00" <= char <= "\u9fff" for char in message):
            summary = message
        if not summary:
            summary = {
                "fail": "未满足本项检查条件；请结合实际读数核对。",
                "error": "执行或读取出错，未取得完整结果。",
                "skip": "本项未执行；请核对设备条件及前置检查。",
            }.get(status, "未取得完整的检查结果。")
        action = ACTIONS.get(identity, "核对连接和设备配置，处理后重测本项。")
    if message and any("\u4e00" <= char <= "\u9fff" for char in message) and message != summary:
        readings += (f"检查说明：{message}",)
    return BoardCheckView(label, summary, readings, action)
