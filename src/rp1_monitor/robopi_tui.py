"""Opt-in, bounded subprocess adapter for RoboPi TUI's passive JSON interface."""

import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .diagnostics import canonical_bytes, redact
from .models import RobotId

MAX_REPORT_BYTES = 256 * 1024
BMS_VERSION_ADDRESS_SPACE = 192 * 1024 * 1024
PRLIMIT = Path("/usr/bin/prlimit")
PASSIVE_PLUGINS = {
    "RP001": "主控板热区温度",
    "RP002": "镜像出厂身份",
    "RP003": "网络接口清单",
    "RP005": "已安装软件版本",
    "RP007": "CAN 接口清单",
    "RP008": "IMU 设备命名规则",
    "RP011": "USB 设备清单",
    "RP012": "USB 3.0 扩展坞",
    "RP013": "USB-CAN 驱动与接口",
    "RP020": "BMS 端口配置",
    "RP021": "BMS 服务与新日志",
    "RP022": "BMS 固件与硬件版本",
    "RP029": "串口设备清单",
    "RP034": "板卡序列号",
}
PLUGIN_GROUPS = {
    **dict.fromkeys(("RP001", "RP002", "RP005", "RP034"), "主机与版本"),
    **dict.fromkeys(("RP003", "RP007", "RP008"), "通信与 IMU"),
    **dict.fromkeys(("RP011", "RP012", "RP013", "RP029"), "USB 与串口"),
    **dict.fromkeys(("RP020", "RP021", "RP022"), "动力电池"),
}
PLUGIN_NOTES = {
    "RP001": "读取主控板热区，不代表电机温度；沿用插件判据，不替换监控限值。",
    "RP002": "核对出厂镜像身份，不等于当前运行的运控版本。",
    "RP003": "读取接口清单和链路状态，不依赖外网，不发送探测数据。",
    "RP005": "读取已安装软件包；源码安装的程序可能不在包清单中。",
    "RP007": "读取 CAN 状态、波特率和接口统计，不发送帧，不代表电机健康。",
    "RP008": "检查 HiPNUC 的设备命名规则，不发送 IMU 寄存器查询，不证明数据有效。",
    "RP011": "读取已枚举的 USB 设备和速率，不复位设备。",
    "RP012": "仅适用于配有 USB 3.0 扩展坞的配置；不适用时保留原插件结果。",
    "RP013": "检查 GSUSB 驱动绑定及 CAN 接口，前置 RP012；不验证 CAN 通信。",
    "RP020": "核对 BMS 类型、端口配置和设备是否存在，不主动测试通信。",
    "RP021": "读取已有 BMS 服务及近期日志，前置 RP020，不重启服务。",
    "RP022": "按需读取并保留版本，前置 RP021、RP020；每个子进程受 192 MiB 地址空间上限约束，失败不自动重试。",
    "RP029": "仅列串口设备节点，不打开串口，不执行收发或回环。",
    "RP034": "读取板卡序列号，便于设备身份追溯。",
}
RESULT_LABELS = {
    "pass": "符合本项判据",
    "fail": "未符合判据",
    "needs_review": "待人工复核",
    "skip": "已跳过",
    "error": "检查错误",
    "incomplete": "检查未完整完成",
}


def check_metadata(item):
    if (
        item.get("id") not in PASSIVE_PLUGINS
        or item.get("safety") != "passive"
        or item.get("kind", "test") != "test"
        or item.get("version") != "1.0"
        or item.get("lifecycle") not in {"testing", "official"}
        or "csharp" not in item.get("clients", [])
    ):
        raise ValueError("插件编号、版本或只读声明不在已核对范围")
    dependencies = item.get("depends_on", [])
    if not isinstance(dependencies, list) or any(p not in PASSIVE_PLUGINS for p in dependencies):
        raise ValueError("插件包含未经核对的前置依赖")


def validate_native_report(raw):
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 3
        or raw.get("selected") not in PASSIVE_PLUGINS
        or raw.get("client") != "csharp"
        or not isinstance(raw.get("tui_version"), str)
        or not 0 < len(raw["tui_version"]) <= 80
    ):
        raise ValueError("不支持的 RoboPi TUI 报告协议或检查编号")
    UUID(raw["run_id"])
    from datetime import datetime

    start, end = (datetime.fromisoformat(raw[k]) for k in ("started_at", "finished_at"))
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("插件报告时间无效")
    rows = raw.get("results")
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(PASSIVE_PLUGINS):
        raise ValueError("插件结果数量无效")
    seen, counts = set(), dict.fromkeys(("pass", "fail", "needs_review", "skip", "error"), 0)
    for row in rows:
        plugin = row.get("plugin", {})
        check_metadata(plugin)
        identity, status = row.get("id"), row.get("result")
        if (
            identity != plugin["id"]
            or identity in seen
            or status not in counts
            or row.get("status") != status
            or not isinstance(row.get("message"), str)
            or not isinstance(row.get("details", {}), dict)
        ):
            raise ValueError("插件结果或元数据不一致")
        if any(dependency not in seen for dependency in plugin.get("depends_on", [])):
            raise ValueError("插件前置结果缺失或顺序无效")
        seen.add(identity)
        counts[status] += 1
    expected = (
        "fail"
        if counts["fail"]
        else (
            "incomplete" if counts["error"] or counts["skip"] or counts["needs_review"] else "pass"
        )
    )
    if (
        raw.get("selected") not in seen
        or raw.get("summary") != counts
        or raw.get("outcome") != expected
    ):
        raise ValueError("整体结论与逐项结果不一致")
    if len(canonical_bytes(raw)) > MAX_REPORT_BYTES:
        raise ValueError("板端报告超过 256 KiB")
    return raw


class BoardReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1)
    report_type: str = Field(default="robopi_tui", pattern=r"^robopi_tui$")
    report_id: UUID
    robot_id: RobotId
    source_kind: str = Field(pattern=r"^(ros2|simulated)$")
    created_at: AwareDatetime
    validation_scope: str
    board_diagnostics: dict

    @model_validator(mode="after")
    def consistent(self):
        raw = validate_native_report(self.board_diagnostics)
        expected = (
            "simulated"
            if self.source_kind == "simulated"
            else "software_observation_not_hardware_acceptance"
        )
        if str(self.report_id) != raw["run_id"] or self.validation_scope != expected:
            raise ValueError("检查身份或验证范围不一致")
        from datetime import datetime

        if self.created_at != datetime.fromisoformat(raw["finished_at"]):
            raise ValueError("检查完成时间不一致")
        return self


class TuiRunner:
    def __init__(self, executor: Path, *, timeout=45):
        self.executor = Path(executor)
        if not self.executor.is_absolute():
            raise ValueError("RoboPi TUI 执行器必须为绝对路径")
        self.timeout = timeout
        self.stop_event = threading.Event()

    def invoke(self, arguments):
        if self.stop_event.is_set():
            raise ValueError("采集退出，板端检查已取消")
        # Called only on our dedicated worker. Children inherit affinity and nice.
        allowed = os.sched_getaffinity(0) - {0}
        if not allowed:
            raise ValueError("排除 CPU0 后没有可用 CPU，拒绝启动板端检查")
        os.sched_setaffinity(0, allowed)
        os.setpriority(os.PRIO_PROCESS, 0, max(10, os.getpriority(os.PRIO_PROCESS, 0)))
        if not self.executor.is_file():
            raise ValueError("未安装 RoboPi TUI 执行器；不会自动安装或升级软件")
        command = [sys.executable, str(self.executor), "--env", "csharp", "--no-save", *arguments]
        if arguments == ["--run", "RP022"]:
            # The existing plugin buffers its service log internally. Bound this
            # executor and descendants, never the multithreaded monitor itself.
            if not PRLIMIT.is_file():
                raise ValueError(
                    "BMS 版本检查需要 /usr/bin/prlimit 限制内存；未执行插件，不会自动安装"
                )
            command = [
                str(PRLIMIT),
                f"--as={BMS_VERSION_ADDRESS_SPACE}:{BMS_VERSION_ADDRESS_SPACE}",
                "--",
                *command,
            ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        output = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                for name in output:
                    selector.register(getattr(process, name), selectors.EVENT_READ, name)
                deadline = time.monotonic() + self.timeout
                while selector.get_map():
                    if self.stop_event.is_set():
                        raise ValueError("采集退出，板端检查已取消")
                    if time.monotonic() >= deadline:
                        raise ValueError("板端检查超时；检查子进程已请求停止")
                    for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fileobj.fileno(), 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output[key.data].extend(chunk)
                        if sum(map(len, output.values())) > MAX_REPORT_BYTES:
                            raise ValueError("板端检查输出超过 256 KiB，已停止")
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise ValueError("板端检查未按时退出") from exc
            if process.returncode:
                reason = (
                    "；BMS 版本检查受每进程 192 MiB 地址空间限制"
                    if arguments == ["--run", "RP022"]
                    else ""
                )
                raise ValueError("执行器未生成报告，请检查安装版本、权限和板端日志" + reason)
            return json.loads(output["stdout"])
        finally:
            if process.poll() is None:
                # Upstream catches KeyboardInterrupt and terminates its own plugin group.
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def run(self, plugin_id):
        if plugin_id not in PASSIVE_PLUGINS:
            raise ValueError("只允许指定的被动检查，禁止 all、query 和 active")
        catalog = self.invoke(["--list"])
        if not isinstance(catalog, dict) or catalog.get("schema_version") != 3:
            raise ValueError("仅支持 RoboPi TUI schema_version=3")
        plugins = catalog.get("tests", [])
        if not isinstance(plugins, list) or len(plugins) > 100:
            raise ValueError("插件目录格式无效")
        by_id = {p["id"]: p for p in plugins}
        if len(by_id) != len(plugins):
            raise ValueError("插件目录编号重复")
        pending, seen = set(), set()

        def visit(identity):
            if identity in pending:
                raise ValueError("插件依赖存在循环")
            if identity in seen:
                return
            plugin = by_id.get(identity)
            if plugin is None:
                raise ValueError("板端缺少指定检查或前置插件")
            check_metadata(plugin)
            pending.add(identity)
            for dependency in plugin.get("depends_on", []):
                visit(dependency)
            pending.remove(identity)
            seen.add(identity)

        visit(plugin_id)
        raw = validate_native_report(self.invoke(["--run", plugin_id]))
        if {row["id"] for row in raw["results"]} != seen or raw["selected"] != plugin_id:
            raise ValueError("执行结果与已核对的插件依赖不一致")
        return redact(raw)
