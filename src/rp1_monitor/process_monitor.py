"""Low frequency, read-only /proc sampling. No argv, environment or process control."""

import math
import os
import threading
import time
from pathlib import Path

from .models import ProcessObservation
from .profiles import ProcessWatch

SIMULATED_WATCHES = [
    ProcessWatch(key="demo_motion", label="模拟运控进程", comm="sim_motion"),
    ProcessWatch(key="demo_driver", label="模拟驱动进程", comm="sim_driver"),
]


def read_proc(path):
    # Avoid interning every transient PID through pathlib in the polling hot path.
    with open(path, encoding="utf-8") as handle:
        return handle.read(4096)


def parse_stat(text):
    """comm may contain spaces and ')'; stat fields start after its final ')' ."""
    left, right = text.index("("), text.rindex(")")
    fields = text[right + 1 :].split()
    result = {
        "pid": int(text[:left].strip()),
        "comm": text[left + 1 : right],
        "kernel_state": fields[0],
        "cpu_ticks": int(fields[11]) + int(fields[12]),
        "threads": int(fields[17]),
        "start_ticks": int(fields[19]),
        "rss_pages": int(fields[21]),
    }
    if len(result["kernel_state"]) != 1 or any(
        result[key] < 0 for key in ("cpu_ticks", "threads", "start_ticks", "rss_pages")
    ):
        raise ValueError("invalid process counters")
    return result


class ProcessProbe:
    def __init__(self, watches, proc_root=Path("/proc")):
        self.watches = watches
        self.proc_root = Path(proc_root)
        self.clock_ticks = os.sysconf("SC_CLK_TCK")
        self.page_bytes = os.sysconf("SC_PAGE_SIZE")
        # At most one previous counter and identity per configured watch, no history.
        self.previous = {}
        self.identities = {}
        self.restarts = {watch.key: 0 for watch in watches}

    def sample(self, observed_at, now=None):
        now = time.monotonic() if now is None else now
        matches = {watch.comm: [] for watch in self.watches}
        incomplete = False
        try:
            with os.scandir(self.proc_root) as entries:
                for entry in entries:
                    if not entry.name.isdecimal():
                        continue
                    try:
                        comm = read_proc(
                            os.path.join(self.proc_root, entry.name, "comm")
                        ).removesuffix("\n")
                        if comm in matches and len(matches[comm]) < 2:
                            matches[comm].append(int(entry.name))
                    except FileNotFoundError:
                        continue  # Normal exit during enumeration.
                    except (OSError, UnicodeError):
                        incomplete = True
        except OSError:
            incomplete = True
        observations, values = [], {}
        for watch in self.watches:
            prefix = f"process.{watch.key}."
            row = ProcessObservation(
                **watch.model_dump(),
                state="unknown",
                detail="无法完整读取进程目录；状态未知",
                observed_at=observed_at,
                age_ms=0,
            )
            pids = matches[watch.comm]
            if incomplete:
                self.previous.pop(watch.key, None)
            elif len(pids) > 1:
                row.state, row.detail = "ambiguous", "存在多个同名进程；不自动选择或合并资源"
                self.previous.pop(watch.key, None)
            elif not pids:
                row.state, row.detail = "stopped", "当前可见PID命名空间内未发现匹配comm"
                values[prefix + "alive"] = 0
                self.previous.pop(watch.key, None)
            else:
                try:
                    stat = parse_stat(read_proc(os.path.join(self.proc_root, str(pids[0]), "stat")))
                    if stat["comm"] != watch.comm or stat["pid"] != pids[0]:
                        raise ValueError("process changed during read")
                    identity = (stat["pid"], stat["start_ticks"])
                    prior_identity = self.identities.get(watch.key)
                    if prior_identity is not None and identity != prior_identity:
                        self.restarts[watch.key] += 1
                    self.identities[watch.key] = identity
                    row.pid, row.start_ticks = identity
                    row.kernel_state = stat["kernel_state"]
                    row.state = "zombie" if row.kernel_state in {"Z", "X", "x"} else "running"
                    row.detail = (
                        "僵尸/退出状态；不计为存活"
                        if row.state == "zombie"
                        else "存活；CPU等待两次有效采样"
                    )
                    values[prefix + "alive"] = int(row.state == "running")
                    if row.state == "running":
                        values[prefix + "rss_mib"] = stat["rss_pages"] * self.page_bytes / 1024**2
                        values[prefix + "threads"] = stat["threads"]
                        previous = self.previous.get(watch.key)
                        if previous and previous[0] == identity and now > previous[2]:
                            ticks = stat["cpu_ticks"] - previous[1]
                            if ticks >= 0:
                                values[prefix + "cpu_percent"] = (
                                    ticks / self.clock_ticks / (now - previous[2]) * 100
                                )
                                row.detail = "CPU单核100%；RSS为内核近似值；存活不代表功能健康"
                        self.previous[watch.key] = (identity, stat["cpu_ticks"], now)
                    else:
                        self.previous.pop(watch.key, None)
                except (OSError, ValueError, IndexError, UnicodeError):
                    row.detail = "读取失败、权限不足或采样时进程变化；状态未知"
                    self.previous.pop(watch.key, None)
            # Counter belongs to this collector stream; unknown samples do not increment it.
            values[prefix + "observed_restarts"] = self.restarts[watch.key]
            observations.append(row)
        return observations, values


class ProcessSource:
    def __init__(self, accumulator, watches):
        self.accumulator = accumulator
        self.probe = ProcessProbe(watches)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="process-observer", daemon=True)

    def start(self):
        if self.probe.watches:
            self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            rows, values = self.probe.sample(self.accumulator.at(time.monotonic_ns()))
            self.accumulator.observe_processes(rows, values)
            self.stop_event.wait(2)

    def stop(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=3)


def simulate_processes(accumulator, watches, elapsed):
    rows, values = [], {}
    for index, watch in enumerate(watches):
        # Deliberate 6s outage every minute exercises missing data and restart views.
        stopped = index == 1 and 30 <= elapsed % 60 < 36
        row = ProcessObservation(
            **watch.model_dump(),
            state="stopped" if stopped else "running",
            detail="模拟进程事件与资源曲线，非操作系统实测；每分钟模拟一次驱动退出/重启",
            observed_at=accumulator.at(time.monotonic_ns()),
            age_ms=0,
        )
        prefix = f"process.{watch.key}."
        values[prefix + "alive"] = int(not stopped)
        values[prefix + "observed_restarts"] = int((elapsed + 24) // 60) if index == 1 else 0
        if not stopped:
            values.update(
                {
                    prefix + "cpu_percent": 12 + index * 5 + 3 * math.sin(elapsed / 4),
                    prefix + "rss_mib": 68 + index * 25 + 2 * math.sin(elapsed / 8),
                    prefix + "threads": 4 + index * 2,
                }
            )
        rows.append(row)
    accumulator.observe_processes(rows, values)


def build_process_view(observations, values, ages, *, available=True):
    items = []
    for raw in observations:
        row = raw.model_dump(mode="json") if isinstance(raw, ProcessObservation) else dict(raw)
        fresh = available and row.get("age_ms") is not None and row["age_ms"] <= 6000
        prefix = f"process.{row['key']}."
        if not fresh:
            row.update(
                state="unknown",
                detail="进程观测未提供或已过期，当前状态未知",
                pid=None,
                start_ticks=None,
                kernel_state=None,
            )
        for quantity in ("alive", "cpu_percent", "rss_mib", "threads", "observed_restarts"):
            key = prefix + quantity
            row[quantity] = values.get(key) if fresh and ages.get(key, math.inf) <= 6000 else None
        row["label_state"] = {
            "running": "存活",
            "stopped": "未发现",
            "zombie": "僵尸/退出",
            "ambiguous": "同名冲突",
            "unknown": "未知",
        }[row["state"]]
        items.append(row)
    return {
        "items": items,
        "poll_seconds": 2,
        "stale_after_ms": 6000,
        "detail": "CPU单核100%，多线程可超过100%；RSS近似值含共享页；重启数仅为本采集会话观察到的实例变化。",
    }
