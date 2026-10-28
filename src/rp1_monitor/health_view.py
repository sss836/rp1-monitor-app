"""One health view for the native app and central dashboard; no invented health score."""

import math
import re
from datetime import datetime, timezone

from .models import metric_max_age_ms


def build_health(values, ages_ms, sources, sensors, joint_names=(), available=True):
    def current(key):
        value, age = values.get(key), ages_ms.get(key)
        return (
            value
            if (
                available
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and age is not None
                and 0 <= age <= metric_max_age_ms(key)
            )
            else None
        )

    normalized_sources = {
        key: value.model_dump() if hasattr(value, "model_dump") else value
        for key, value in sources.items()
    }
    battery = {
        name: current(f"battery.{name}")
        for name in (
            "soc",
            "voltage",
            "current",
            "power_enabled",
            "rail48_enabled",
            "rail5_enabled",
            "temperature",
            "protection",
        )
    }
    battery_ready = all(battery[key] is not None for key in ("soc", "voltage", "current"))
    is_cache = "rp_server" in normalized_sources
    battery.update(
        state="observing" if battery_ready else "incomplete",
        label=("电池缓存快照" if is_cache else "电池观测数据")
        if battery_ready
        else "电池数据未接入 / 已过期",
        detail=normalized_sources.get("bms", {}).get("detail", "尚未收到BMS数据")
        + "；日志记录时间不等于每个字段的硬件采样时间，不能据此判定电池健康。",
        derived_power=battery["voltage"] * battery["current"] if battery_ready else None,
        age_ms=max((ages_ms.get(f"battery.{key}", 0) for key in ("soc", "voltage", "current")))
        if battery_ready
        else None,
        age_label="快照接收年龄"
        if is_cache
        else "日志记录年龄"
        if "bms" in normalized_sources
        else "观测年龄",
    )
    if battery["protection"] is not None and battery["protection"] != 0:
        battery.update(state="fault", label="存在电池保护位上报")
    thermal = [
        {
            "key": key,
            "label": sensors.get(key, key),
            "temperature": current(key),
            "age_ms": ages_ms.get(key),
        }
        for key in sorted(set(values) | set(sensors))
        if key.startswith("host.thermal.")
    ]
    temperatures = [item["temperature"] for item in thermal if item["temperature"] is not None]
    core_indices = sorted({
        int(match.group(1))
        for key in set(values) | set(sensors)
        if (match := re.fullmatch(r"host\.cpu\.([0-9]{1,2})\.usage", key))
    })
    cores = []
    for index in core_indices:
        prefix = f"host.cpu.{index}"
        usage = current(prefix + ".usage")
        group = sensors.get(prefix + ".group", "unknown")
        if group not in {"big", "little"}:
            group = "unknown"
        if sensors.get(prefix + ".state") != "live" or usage is not None and not 0 <= usage <= 100:
            usage = None
        cores.append({
            "index": index, "label": sensors.get(prefix + ".usage", f"CPU {index}"),
            "group": group, "usage_percent": usage, "age_ms": ages_ms.get(prefix + ".usage"),
        })
    host = {
        "state": "observing" if temperatures else "incomplete",
        "label": "主机温度观测" if temperatures else "主机温度未提供 / 已过期",
        "detail": normalized_sources.get("host", {}).get("detail", "等待机器人端Linux采集")
        + "；热区类型来自内核，温度限值尚未配置。",
        "max_temperature": max(temperatures) if temperatures else None,
        "cpu_usage": current("host.cpu.usage"),
        "cores": cores,
        "cpu_topology_note": sensors.get("host.cpu.topology", "采集端尚未提供每核CPU数据。"),
        "memory_usage": current("host.memory.usage"),
        "uptime_seconds": current("host.uptime.seconds"),
        "sensors": thermal,
    }
    joints = sorted(
        set(range(len(joint_names)))
        | {int(key.split(".")[1]) for key in values if key.startswith("joint.")}
    )
    motor_items = [
        {
            "joint": index,
            "name": joint_names[index] if index < len(joint_names) else str(index),
            "temperature": current(f"joint.{index}.motor_temperature"),
            "mos_temperature": current(f"joint.{index}.mos_temperature"),
            "fault_code": current(f"joint.{index}.fault_code"),
        }
        for index in joints
    ]
    faults = [
        {"joint": item["joint"], "code": item["fault_code"]}
        for item in motor_items
        if item["fault_code"] is not None and item["fault_code"] != 0
    ]
    cached_faults = normalized_sources.get("rp_server", {})
    stamp = cached_faults.get("observed_at")
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp)
    if (
        available
        and cached_faults.get("state") == "live"
        and stamp is not None
        and (0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 5)
    ):
        faults += [
            {"joint": f"驱动ID {fault['motor_id']}", "code": fault["code"]}
            for fault in cached_faults.get("reported_faults", [])
            if fault["code"] != 0
        ]
    complete = sum(
        item["temperature"] is not None and item["fault_code"] is not None for item in motor_items
    )
    motor_temperatures = [
        item["temperature"] for item in motor_items if item["temperature"] is not None
    ]
    mos_temperatures = [
        item["mos_temperature"] for item in motor_items if item["mos_temperature"] is not None
    ]
    motor_ready = bool(joints) and complete == len(joints)
    motors = {
        "state": "fault" if faults else "observing" if motor_ready else "incomplete",
        "label": "存在电机故障上报"
        if faults
        else "电机健康数据已接收"
        if motor_ready
        else "电机温度 / 故障数据未完整接入",
        "detail": normalized_sources.get("motors", {}).get(
            "detail", "当前ROS关节话题不包含温度或故障码；需要运控进程发布已有驱动缓存"
        )
        + "；故障码按原值显示，温度限值尚未配置。",
        "available": complete,
        "total": len(joints),
        "max_temperature": max(motor_temperatures) if motor_temperatures else None,
        "max_mos_temperature": max(mos_temperatures) if mos_temperatures else None,
        "faults": faults,
        "items": motor_items,
    }
    missing = []
    if not battery_ready:
        missing.append("电池电量/电压/电流")
    if battery["temperature"] is None:
        missing.append("电池温度")
    if battery["protection"] is None:
        missing.append("电池保护位")
    if not temperatures:
        missing.append("主机温度")
    if not motor_ready:
        missing.append("电机温度/故障")
    battery_fault = battery["protection"] is not None and battery["protection"] != 0
    state = "fault" if faults or battery_fault else "incomplete" if missing else "observing"
    overall = {
        "state": state,
        "label": "存在故障 / 保护上报"
        if faults or battery_fault
        else "健康监测不完整"
        if missing
        else "健康数据观测中",
        "detail": ("待接入或已过期：" + "、".join(missing) + "。" if missing else "")
        + ("电池为缓存快照，字段采样时间未知。" if is_cache else "")
        + "采集在线不代表机器人健康；未配置温度、电压等判据，不给出健康结论。",
    }
    return {
        "overall": overall,
        "battery": battery,
        "host": host,
        "motors": motors,
        "sources": normalized_sources,
    }
