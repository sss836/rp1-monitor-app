"""Shared, robot-scoped read operations for UI, diagnostics and AI tools."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .capabilities import evaluate_capabilities
from .diagnostics import build_report
from .profiles import RobotProfile
from .report_store import save_report


def query_interval(start=None, end=None):
    end = end or datetime.now(timezone.utc)
    start = start or end - timedelta(minutes=10)
    if start.tzinfo is None or end.tzinfo is None or not 0 < (end - start).total_seconds() <= 86400:
        raise ValueError("诊断查询必须带时区，范围大于0且不超过24小时")
    return start, end


class MonitorService:
    def __init__(self, store, profiles=None):
        self.store = store
        self.profiles = profiles or {}

    def snapshot(self, robot_id):
        snapshot = next(
            (item for item in self.store.snapshots()["items"] if item["robot_id"] == robot_id), None
        )
        if snapshot is None:
            raise LookupError("机器人不存在")
        return snapshot

    def profile(self, snapshot):
        robot_id = snapshot["robot_id"]
        raw = self.profiles.get(robot_id) or snapshot.get("quality", {}).get("device_profile")
        profile = RobotProfile.model_validate(raw) if raw else RobotProfile(robot_id=robot_id)
        if profile.robot_id != robot_id:
            raise ValueError("设备档案身份不一致")
        return profile

    def capabilities(self, robot_id):
        snapshot = self.snapshot(robot_id)
        profile = self.profile(snapshot)
        return {
            "robot_id": robot_id,
            "profile": profile.model_dump(),
            "monitoring_configuration": snapshot.get("monitoring_configuration"),
            "items": evaluate_capabilities(snapshot, profile),
        }

    def evidence(self, robot_id, metrics=(), start=None, end=None):
        start, end = query_interval(start, end)
        snapshot = self.snapshot(robot_id)
        monitor = getattr(self.store, "alert_monitor", None)
        if monitor:
            snapshot["alerts"] = monitor.current(robot_id)
        joints = sorted(
            {
                int(key.split(".")[1])
                for key in metrics
                if key.startswith("joint.")
                and key.endswith((".motor_temperature", ".mos_temperature"))
            }
        )
        if joints:
            snapshot["temperature_events"] = [
                self.temperature_alerts(robot_id, joint, snapshot) for joint in joints
            ]
        curves = [self.store.curves(robot_id, metric, start, end, 300) for metric in metrics]
        return build_report(
            snapshot,
            self.profile(snapshot),
            curves=curves,
            query={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "metrics": list(metrics),
                "point_budget_per_metric": 300,
            },
        )

    def temperature_alerts(self, robot_id, joint, snapshot=None):
        from .central_tables import alert_events, alert_heads

        keys = [
            f"limit.joint.{joint}.{quantity}"
            for quantity in ("motor_temperature", "mos_temperature")
        ]
        with self.store._read_lock, self.store.engine.connect() as connection:
            heads = (
                connection.execute(
                    select(alert_heads)
                    .where((alert_heads.c.robot_id == robot_id) & alert_heads.c.rule_key.in_(keys))
                    .order_by(alert_heads.c.rule_key)
                )
                .mappings()
                .all()
            )
            events = (
                connection.execute(
                    select(alert_events)
                    .where(
                        (alert_events.c.robot_id == robot_id) & alert_events.c.rule_key.in_(keys)
                    )
                    .order_by(alert_events.c.id.desc())
                    .limit(20)
                )
                .mappings()
                .all()
            )
        now = datetime.now(timezone.utc)
        snapshot = snapshot or self.snapshot(robot_id)
        policy = self.store.monitoring_policy(snapshot)
        revision = (snapshot.get("monitoring_configuration") or {}).get("revision")

        def document(row):
            return {
                key: value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
                if isinstance(value, datetime)
                else value
                for key, value in row.items()
            }

        return {
            "joint": joint,
            "heads": [
                {
                    **document(row),
                    "evaluation_fresh": row["policy_sha256"] == policy.digest
                    and row["evidence"].get("configuration_revision") == revision
                    and 0
                    <= (
                        now
                        - row["evaluated_at"].replace(
                            tzinfo=row["evaluated_at"].tzinfo or timezone.utc
                        )
                    ).total_seconds()
                    <= 5,
                }
                for row in heads
            ],
            "events": [document(row) for row in events],
            "meaning": "触发/恢复时间来自已持久化的监测事件；观测中断不等于温度恢复，告警开启时长不等于已证实连续超温时长。",
        }

    def joint_temperature(self, robot_id, joint, minutes=10):
        snapshot = self.snapshot(robot_id)
        row = next(
            (
                item
                for item in snapshot.get("health", {}).get("motor_map", {}).get("rows", [])
                if item["joint"] == joint
            ),
            None,
        )
        if row is None:
            raise LookupError("该机器人未登记或未观测到此关节")
        end = datetime.now(timezone.utc)
        if (snapshot.get("transport") or {}).get("freshness_verified") and snapshot.get(
            "captured_at"
        ):
            received = datetime.fromisoformat(snapshot["received_at"])
            end = datetime.fromisoformat(snapshot["captured_at"]) + max(end - received, timedelta())
        start = end - timedelta(minutes=minutes)
        curves = [
            self.store.curves(robot_id, f"joint.{joint}.{quantity}", start, end, 300)
            for quantity in ("motor_temperature", "mos_temperature")
        ]
        return {
            "robot_id": robot_id,
            "source_kind": snapshot["source_kind"],
            "joint": joint,
            "as_of_at": end.isoformat(),
            "current": row,
            "configuration_revision": (snapshot.get("monitoring_configuration") or {}).get(
                "revision"
            ),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "curves": curves,
            "alerts": self.temperature_alerts(robot_id, joint, snapshot),
            "meaning": "曲线为原始摘要窗口最后值及适用的极值，保留缺失断点。限值为当前配置，历史告警各自保存当时策略；不据此推断发热根因。",
        }

    def record(self, robot_id, metrics=(), start=None, end=None):
        return save_report(self.store, self.evidence(robot_id, metrics, start, end))

    def compare_temperatures(self, robot_id, start, end):
        start, end = query_interval(start, end)
        snapshot = self.snapshot(robot_id)
        metrics = [
            item["key"]
            for item in snapshot.get("catalog", [])
            if item["key"].endswith(".motor_temperature")
        ][:64]
        results = []
        for metric in metrics:
            intervals = []
            for begin, finish in ((start, end), (start - (end - start), start)):
                curve = self.store.curves(robot_id, metric, begin, finish, 300)
                points = curve.get("points", [])
                valid = [point for point in points if point.get("last") is not None]
                intervals.append(
                    {
                        "start": begin.isoformat(),
                        "end": finish.isoformat(),
                        "summary_points": len(points),
                        "valid_points": len(valid),
                        "first_observed": valid[0]["last"] if valid else None,
                        "last_observed": valid[-1]["last"] if valid else None,
                        "endpoint_delta": valid[-1]["last"] - valid[0]["last"]
                        if len(valid) >= 2
                        else None,
                        "missing_windows": sum(point.get("missing_windows", 0) for point in points),
                        "gaps": sum(point.get("gap_count", 0) for point in points),
                    }
                )
            results.append(
                {"metric": metric, "unit": "°C", "current": intervals[0], "previous": intervals[1]}
            )
        return {
            "robot_id": robot_id,
            "source_kind": snapshot["source_kind"],
            "items": results,
            "meaning": "各时间段首末有效摘要点之差；不是高频温升曲线，不推断动作或发热根因",
        }
