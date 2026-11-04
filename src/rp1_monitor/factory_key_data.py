"""Bounded statistics and exception snapshots; not a performance verdict."""

from collections import deque
from copy import deepcopy

SCHEMA = "rp1.factory.key-data.v1"
MINIMUM_TEST_SECONDS = 2 * 60 * 60


class KeyDataSummary:
    def __init__(self):
        self.metrics = {}
        self.latest = {}
        self.history = deque(maxlen=2)
        self.exceptions = []
        self.exception_count = 0
        self._alarms = {}

    def observe(self, group, payload, elapsed):
        self.latest[group] = dict(elapsed_seconds=elapsed, **payload)
        for key, value in payload["values"].items():
            stat = self.metrics.setdefault(key, dict(
                unit=payload["units"][key], observations=0, valid=0, missing=0,
                minimum=None, maximum=None, mean=None, maximum_age_ms=None,
                first_valid_elapsed=None, last_valid_elapsed=None, maximum_valid_gap_seconds=None))
            stat["observations"] += 1
            age = payload["ages_ms"].get(key)
            if age is not None:
                stat["maximum_age_ms"] = max(stat["maximum_age_ms"] or 0, age)
            if value is None:
                stat["missing"] += 1
                continue
            stat["valid"] += 1
            stat["minimum"] = value if stat["minimum"] is None else min(stat["minimum"], value)
            stat["maximum"] = value if stat["maximum"] is None else max(stat["maximum"], value)
            stat["mean"] = value if stat["mean"] is None else stat["mean"] + (value - stat["mean"]) / stat["valid"]
            if stat["first_valid_elapsed"] is None:
                stat["first_valid_elapsed"] = elapsed
            if stat["last_valid_elapsed"] is not None:
                gap = elapsed - stat["last_valid_elapsed"]
                stat["maximum_valid_gap_seconds"] = max(stat["maximum_valid_gap_seconds"] or 0, gap)
            stat["last_valid_elapsed"] = elapsed
            if key.endswith(".fault_code") or key == "battery.protection":
                previous = self._alarms.get(key, 0)
                self._alarms[key] = value
                if value and value != previous:
                    self.exception(dict(source="feedback", metric=key, value=value,
                                        source_at=payload.get("source_at")), elapsed)

    def exception(self, event, elapsed):
        self.exception_count += 1
        if len(self.exceptions) < 3:
            self.exceptions.append(dict(event=deepcopy(event), elapsed_seconds=elapsed,
                                        before=deepcopy(list(self.history)),
                                        nearest=deepcopy(self.latest), after=[]))

    def trend(self, elapsed):
        snapshot = dict(elapsed_seconds=elapsed, groups=deepcopy(self.latest))
        for window in self.exceptions:
            if elapsed > window["elapsed_seconds"] and len(window["after"]) < 2:
                window["after"].append(deepcopy(snapshot))
        self.history.append(snapshot)

    def snapshot(self, duration):
        return dict(schema_version=SCHEMA, recording_mode="key_data", duration_seconds=duration,
                    minimum_test_seconds=MINIMUM_TEST_SECONDS,
                    duration_satisfied=duration > MINIMUM_TEST_SECONDS,
                    observation_hz=dict(motion=10, health=1), trend_hz=dict(motion=1, health=1),
                    metrics=self.metrics, exception_count=self.exception_count,
                    exception_windows=self.exceptions,
                    exception_windows_omitted=max(0, self.exception_count - len(self.exceptions)),
                    performance_verdict="unverified",
                    note="统计为采集端观察值，观察次数不等于独立反馈帧数；趋势为1Hz关键快照。异常保留最多3组前后快照，更多异常次数仍记录；缺失不补零，不自动判定性能合格。")
