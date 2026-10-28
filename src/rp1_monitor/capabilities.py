"""Deterministic monitoring capability assessment, shared by every client."""

from .profiles import RobotProfile


def evaluate_capabilities(snapshot: dict, profile: RobotProfile) -> list[dict]:
    health = snapshot.get("health", {})
    quality = snapshot.get("quality", {})
    sources = snapshot.get("health_sources", quality.get("health_sources", {}))
    online = snapshot.get("online", snapshot.get("upload", {}).get("collector_connected", True))
    simulated = snapshot.get("source_kind") == "simulated"
    items = []
    for key, label, section, required in (
        ("battery_basic", "电池电量、电压、电流", "battery", ("soc", "voltage", "current")),
        ("battery_temperature", "电池温度", "battery", ("temperature",)),
        ("battery_protection", "电池保护位", "battery", ("protection",)),
        ("host_temperature", "主机温度", "host", ("max_temperature",)),
        ("motor_health", "电机温度与故障", "motors", ()),
    ):
        data = health.get(section, {})
        ready = all(data.get(field) is not None for field in required)
        if section == "motors":
            ready = bool(data.get("total")) and data.get("available") == data.get("total")
            if profile.motors:
                ready = ready and data.get("total") == len(profile.motors)
        source = sources.get(
            "motors"
            if section == "motors"
            else "host"
            if section == "host"
            else "rp_server"
            if "rp_server" in sources
            else "bms",
            {},
        )
        declared = (
            profile.bms_type != "unknown"
            if section == "battery"
            else bool(profile.motors)
            if section == "motors"
            else True
        )
        if not online:
            state, reason = "stale", "采集端离线或当前观测已过期"
        elif ready:
            state, reason = "observed", "当前有有效观测；不等于健康验收通过"
        elif source.get("state") == "error":
            state, reason = "error", source.get("detail", "数据源错误")
        elif source.get("state") == "unsupported":
            state, reason = "unsupported", source.get("detail", "来源尚未提供此字段")
        else:
            state, reason = "missing", source.get("detail", "尚未收到有效反馈，或反馈已过期")
        if section == "motors" and profile.motors and not ready:
            reason += f"；档案要求{len(profile.motors)}台，当前完整反馈{data.get('available', 0)}台"
        if ready and section == "battery" and "rp_server" in sources:
            reason += "；缓存快照，硬件字段采样时间未知"
        items.append(
            {
                "key": key,
                "label": label,
                "state": state,
                "ready": ready and online,
                "declared": declared,
                "reason": reason,
                "simulated": simulated,
                "observed": {field: data.get(field) for field in required},
            }
        )
    items.append(
        {
            "key": "physical_mapping",
            "label": "物理关节映射",
            "state": "verified" if profile.physical_mapping_verified else "unverified",
            "ready": profile.physical_mapping_verified,
            "declared": bool(profile.motors),
            "reason": "依据设备档案；仍须与现场机器人版本一致",
            "simulated": simulated,
            "observed": {},
        }
    )
    return items
