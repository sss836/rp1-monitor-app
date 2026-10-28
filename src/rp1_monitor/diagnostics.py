"""Reproducible, structured diagnostic records and portable evidence bundles."""

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

from .capabilities import evaluate_capabilities


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[redacted]"
            if re.search(r"token|secret|password|authorization|credential|api.key", k, re.I)
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(
            r"(?i)(Bearer\s+\S+|sk-[A-Za-z0-9_-]{12,}|https?://[^\s/@]+:[^\s/@]+@)",
            "[redacted]",
            value,
        )
    return value


def checks_for(snapshot, profile):
    capabilities = evaluate_capabilities(snapshot, profile)
    checks = [
        {
            "key": c["key"],
            "result": "pass" if c["ready"] else "attention",
            "detail": c["reason"],
            "next_step": {
                "motor_health": "核对设备档案中的总线/ID/关节映射及现有运控的 /rp1/diagnostics 发布；不要新建电机控制器",
                "battery_temperature": "核对实际BMS协议及现有只读来源是否提供有效温度",
                "physical_mapping": "现场核对关节名、总线、电机ID与实际硬件后更新设备档案",
            }.get(c["key"], "核对来源支持情况、采集进程及反馈时间")
            if not c["ready"]
            else "保留观测；健康判据需按实际规格配置",
        }
        for c in capabilities
    ]
    quality = snapshot.get("quality", {})
    processes = snapshot.get("health", {}).get("processes", {}).get("items", [])
    checks.append(
        {
            "key": "process_monitor",
            "result": "pass"
            if processes and all(p["state"] == "running" for p in processes)
            else "attention",
            "detail": "；".join(
                f"{p['label']}：{p['label_state']}，{p['detail']}" for p in processes
            )
            or "未配置或未收到核心进程观测",
            "next_step": "核对实际comm与可见PID命名空间；只读观测不验证进程功能健康，不执行启停",
        }
    )
    reported_profile = snapshot.get("device_profile", quality.get("device_profile"))
    if reported_profile:
        checks.append(
            {
                "key": "profile_consistency",
                "result": "pass" if reported_profile == profile.model_dump() else "attention",
                "detail": "对比中央登记档案与采集端上报档案",
                "next_step": "不一致时核对实际版本及映射，不能用中央配置冒充实机配置",
            }
        )
    for key, ok, detail in (
        (
            "collector",
            snapshot.get("online", snapshot.get("upload", {}).get("collector_connected", True)),
            "机器人连接与中央上传分别判定",
        ),
        (
            "clock",
            not snapshot.get(
                "clock_unverified", quality.get("clock_status") == "WALL_CLOCK_CHANGED"
            ),
            "核对机器人与中央时钟；接收时间不是硬件采样时间",
        ),
        (
            "mapping",
            bool(snapshot.get("joint_names", quality.get("joint_names", []))),
            "须先收到规范关节名单，才能关联电机诊断",
        ),
        (
            "versions",
            all(
                getattr(profile, k) != "unknown"
                for k in ("hardware_revision", "inference_revision", "driver_revision")
            ),
            "核对硬件与实际运行的运控、驱动版本",
        ),
    ):
        checks.append(
            {
                "key": key,
                "result": "pass" if ok else "attention",
                "detail": detail,
                "next_step": "继续观测" if ok else detail,
            }
        )
    upload = snapshot.get("upload", quality.get("collector_status", {}))
    if upload:
        checks.append(
            {
                "key": "upload",
                "result": "attention"
                if upload.get("last_error")
                or upload.get("error")
                or upload.get("collector_error")
                or upload.get("receiver_error")
                or upload.get("backfill_error")
                or upload.get("pending_windows", 0) > 0
                else "pass",
                "detail": f"待传窗口 {upload.get('pending_windows', 0)}；{upload.get('last_error', '')} {upload.get('receiver_error', '')} {upload.get('backfill_error', '')}",
                "next_step": "有积压时核对中央服务、设备凭据、证书和磁盘；未确认数据继续保留",
            }
        )
    return checks


def build_report(snapshot, profile, *, curves=None, query=None):
    report = {
        "schema_version": 1,
        "report_id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "robot_id": profile.robot_id,
        "source_kind": snapshot.get("source_kind"),
        "validation_scope": "simulated"
        if snapshot.get("source_kind") == "simulated"
        else "software_observation_not_hardware_acceptance",
        "software_version": "0.4.0",
        "profile": profile.model_dump(),
        "capabilities": evaluate_capabilities(snapshot, profile),
        "checks": checks_for(snapshot, profile),
        "snapshot": snapshot,
        "query": query or {},
        "curves": curves or [],
        "limitations": [
            "无温度/电压告警阈值时不作健康通过结论",
            "摘要曲线不能恢复完整高频原始数据",
            "采样时间未知的缓存不能证明硬件数据新鲜",
        ],
    }
    return redact(report)


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def export_bundle(saved):
    report = saved["report"]
    files = {
        "report.json": canonical_bytes(report),
        "README.txt": (
            f"RP1 诊断证据包\n设备: {report['robot_id']}\n来源: {report['source_kind']}\n验证范围: {report['validation_scope']}\nSHA256SUMS 校验所有内容。此包不包含设备凭据或原始系统日志。\n"
        ).encode(),
    }
    hashes = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in files.items()
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("SHA256SUMS", hashes)
    return buffer.getvalue()
