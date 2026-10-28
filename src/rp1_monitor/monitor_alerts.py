"""Durable, three-valued monitoring alerts; unknown never means recovered."""

import threading
from datetime import datetime, timezone

from sqlalchemy import insert, select, update

from .central_data import register_policy
from .central_tables import alert_events, alert_heads, relay_status
from .models import RelayStatus
from .storage import robots, utc


def accept_relay_status(store, message: RelayStatus, device, now=None):
    now = now or datetime.now(timezone.utc)
    if (message.robot_id, message.source_kind) != (device["robot_id"], device["source_kind"]):
        raise PermissionError("设备凭据只能写绑定机器人及来源的上位机状态")
    if abs((now - message.observed_at).total_seconds()) > 5:
        raise ValueError("上位机状态已过期或时钟偏差超过5秒")
    with store._lock, store.engine.begin() as connection:
        registered = (
            connection.execute(
                select(robots).where(robots.c.robot_id == message.robot_id).with_for_update()
            )
            .mappings()
            .first()
        )
        if (
            not registered
            or not registered["enabled"]
            or registered["token_digest"] != device["token_digest"]
        ):
            raise PermissionError("设备凭据已撤销")
        old = (
            connection.execute(
                select(relay_status).where(relay_status.c.robot_id == message.robot_id)
            )
            .mappings()
            .first()
        )
        if (
            old
            and old["session_id"] == str(message.session_id)
            and message.sequence <= old["sequence"]
        ):
            return {"accepted": False, "reason": "duplicate_or_old_sequence"}
        if old and message.observed_at < utc(old["observed_at"]):
            return {"accepted": False, "reason": "older_observation"}
        values = dict(
            received_at=now,
            observed_at=message.observed_at,
            session_id=str(message.session_id),
            sequence=message.sequence,
            payload=message.model_dump(mode="json"),
        )
        if old:
            connection.execute(
                update(relay_status)
                .where(relay_status.c.robot_id == message.robot_id)
                .values(**values)
            )
        else:
            connection.execute(insert(relay_status).values(robot_id=message.robot_id, **values))
    return {"accepted": True}


def conditions(snapshot, status, policy, now=None):
    now = now or datetime.now(timezone.utc)

    def rule(key, bad, detail, **facts):
        return {"rule_key": key, "bad": bad, "detail": detail, "facts": facts}

    age = snapshot.get("age_seconds")
    valid = snapshot.get("online") and not snapshot.get("clock_unverified")
    result = [
        rule(
            "collector.offline",
            age > 5 if age is not None else None,
            "实时摘要超过5秒未到达；不等同于机器人断电",
            age_seconds=age,
        ),
        rule(
            "telemetry.stale",
            not snapshot["data_fresh"] if valid else None,
            "运动数据过期、缺失或映射未确认",
            data_fresh=snapshot.get("data_fresh"),
        ),
    ]
    for item in snapshot.get("health", {}).get("processes", {}).get("items", []):
        bad = item["state"] != "running" or item.get("kernel_state") in {"T", "t"}
        result.append(
            rule(
                f"process.{item['key']}.unavailable",
                None if item["state"] == "unknown" else bad,
                f"{item['label']}：{item['label_state']}；{item['detail']}",
                state=item["state"],
                kernel_state=item.get("kernel_state"),
                observed_at=item.get("observed_at"),
            )
        )
    fresh_status = status is not None and status["fresh"]
    payload = status["payload"] if fresh_status else {}
    status_facts = {
        "status_observed_at": payload.get("observed_at"),
        "status_received_at": utc(status["received_at"]).isoformat() if status else None,
        "status_session_id": payload.get("session_id"),
        "status_sequence": payload.get("sequence"),
        "robot_observed_at": payload.get("robot_observed_at"),
        "robot_disk_observed_at": payload.get("robot_disk_observed_at"),
    }
    result.append(
        rule(
            "relay.collector_disconnected",
            not payload["collector_connected"] if fresh_status else None,
            "上位机报告机器人网线/SSH预览链路失联",
            **status_facts,
        )
    )
    for label, prefix in (("relay", ""), ("robot", "robot_")):
        pending = payload.get(prefix + "pending_windows")
        result.append(
            rule(
                label + ".backlog",
                pending >= policy.pending_windows_warning if pending is not None else None,
                "待传摘要积压达到配置值",
                pending_windows=pending,
                threshold=policy.pending_windows_warning,
                **status_facts,
            )
        )
        free, reserve = (
            payload.get(prefix + "free_disk_bytes"),
            payload.get(prefix + "reserve_bytes"),
        )
        if label == "robot":
            stamp = payload.get("robot_disk_observed_at")
            if (
                not stamp
                or not 0
                <= (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
                <= policy.status_max_age_seconds
            ):
                free = None
        result.append(
            rule(
                label + ".disk_low",
                free < reserve if free is not None and reserve is not None else None,
                "缓存所在文件系统剩余空间低于配置保留值",
                free_disk_bytes=free,
                reserve_bytes=reserve,
                **status_facts,
            )
        )
    for metric, limit in policy.temperature_limits_c.items():
        quality = snapshot.get("quality", {})
        value = quality.get("health_values", {}).get(metric)
        sample_age = quality.get("health_ages_ms", {}).get(metric)
        value = (
            value
            if valid
            and sample_age is not None
            and sample_age + ((age or 0) + max(0, snapshot.get("clock_offset_seconds") or 0)) * 1000
            <= 5000
            else None
        )
        result.append(
            rule(
                "limit." + metric,
                value > limit if value is not None else None,
                "温度超过人工配置限值；不推断发热根因",
                metric=metric,
                value=value,
                threshold_c=limit,
            )
        )
    if not snapshot.get("enabled", True):
        for item in result:
            item["bad"] = None
            item["detail"] = "设备已停用，当前监测状态未知；已有告警不自动恢复"
    return result


class AlertMonitor:
    def __init__(self, store):
        self.store = store
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="central-alert-observer", daemon=True)
        self.error = ""

    def statuses(self, now):
        with self.store._read_lock, self.store.engine.connect() as connection:
            rows = connection.execute(select(relay_status)).mappings().all()
        return {
            row["robot_id"]: {
                **row,
                "fresh": 0
                <= (now - utc(row["observed_at"])).total_seconds()
                <= self.store.policy.status_max_age_seconds
                and (now - utc(row["received_at"])).total_seconds()
                <= self.store.policy.status_max_age_seconds,
            }
            for row in rows
        }

    def tick(self, now=None):
        now = now or datetime.now(timezone.utc)
        snapshots = self.store.snapshots()["items"]
        statuses = self.statuses(now)
        with self.store._lock, self.store.engine.begin() as connection:
            # Register all actual criteria before locking robot rows, matching ingest lock order.
            policies = {
                snapshot["robot_id"]: self.store.monitoring_policy(snapshot)
                for snapshot in snapshots
            }
            for _, policy in sorted(
                {policy.digest: policy for policy in policies.values()}.items()
            ):
                register_policy(connection, policy, now)
            for snapshot in sorted(snapshots, key=lambda row: row["robot_id"]):
                robot_id = snapshot["robot_id"]
                policy = policies[robot_id]
                connection.execute(
                    select(robots.c.robot_id).where(robots.c.robot_id == robot_id).with_for_update()
                ).first()
                items = conditions(snapshot, statuses.get(robot_id), policy, now=now)
                known = (
                    connection.execute(
                        select(alert_heads.c.rule_key).where(alert_heads.c.robot_id == robot_id)
                    )
                    .scalars()
                    .all()
                )
                current_keys = {item["rule_key"] for item in items}
                items.extend(
                    {
                        "rule_key": key,
                        "bad": None,
                        "detail": "监测名单或规则已移除；当前观测未知，已有告警不自动恢复",
                        "facts": {},
                    }
                    for key in known
                    if key not in current_keys
                )
                for item in items:
                    self.evaluate(connection, snapshot, item, now, policy=policy)
        self.error = ""

    def evaluate(self, connection, snapshot, item, now, policy=None):
        policy = policy or self.store.monitoring_policy(snapshot)
        robot_id, key, bad = snapshot["robot_id"], item["rule_key"], item["bad"]
        where = (alert_heads.c.robot_id == robot_id) & (alert_heads.c.rule_key == key)
        old = connection.execute(select(alert_heads).where(where)).mappings().first()
        if old and utc(old["evaluated_at"]) >= now:
            return
        same_policy = (
            old
            and old["policy_sha256"] == policy.digest
            and (now - utc(old["evaluated_at"])).total_seconds() <= 5
            and old["evidence"].get("stream_id") == snapshot.get("stream_id")
        )
        since = utc(old["condition_since"]) if same_policy and old["condition_since"] else now
        if since > now:
            since = now
        is_open = bool(old and old["is_open"])
        opened_at = old["opened_at"] if old else None
        if bad is None:
            state, since = "unknown", None
        elif bad:
            state = (
                "active"
                if is_open or (now - since).total_seconds() >= policy.alert_hold_seconds
                else "pending"
            )
            if state == "active" and not is_open:
                opened_at = now
            is_open = is_open or state == "active"
        else:
            state, since, is_open = "normal", None, False
        evidence = {
            **item,
            "robot_id": robot_id,
            "source_kind": snapshot["source_kind"],
            "captured_at": snapshot.get("captured_at"),
            "clock_unverified": snapshot.get("clock_unverified"),
            "sequence": snapshot.get("sequence"),
            "stream_id": snapshot.get("stream_id"),
            "policy_sha256": policy.digest,
            "configuration_revision": snapshot.get("monitoring_configuration", {}).get("revision"),
            "episode_opened_at": utc(opened_at).isoformat() if opened_at else None,
            "condition_since": since.isoformat() if since else None,
        }
        event = None
        if is_open and not (old and old["is_open"]):
            event = "opened"
        elif old and old["is_open"] and not is_open:
            event = "recovered"
        elif old and old["state"] != state and state == "unknown":
            event = "observation_lost"
        elif old and old["state"] == "unknown" and state != "unknown":
            event = "observation_restored"
        values = dict(
            state=state,
            is_open=is_open,
            condition_since=since,
            opened_at=opened_at,
            evaluated_at=now,
            policy_sha256=policy.digest,
            evidence=evidence,
        )
        if old:
            connection.execute(update(alert_heads).where(where).values(**values))
        else:
            connection.execute(
                insert(alert_heads).values(robot_id=robot_id, rule_key=key, **values)
            )
        if event:
            connection.execute(
                insert(alert_events).values(
                    robot_id=robot_id,
                    rule_key=key,
                    occurred_at=now,
                    event_kind=event,
                    policy_sha256=policy.digest,
                    evidence=evidence,
                )
            )

    def current(self, robot_id):
        with self.store._read_lock, self.store.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(alert_heads)
                    .where(alert_heads.c.robot_id == robot_id)
                    .order_by(alert_heads.c.rule_key)
                )
                .mappings()
                .all()
            )
        now = datetime.now(timezone.utc)
        configuration = self.store.effective_monitoring_configuration(robot_id)
        policy = self.store.monitoring_policy(
            {
                "robot_id": robot_id,
                "source_kind": configuration["source_kind"],
                "monitoring_configuration": configuration,
            }
        )
        return {
            "items": [
                {
                    **{
                        key: utc(value).isoformat() if isinstance(value, datetime) else value
                        for key, value in row.items()
                    },
                    "evaluation_fresh": 0 <= (now - utc(row["evaluated_at"])).total_seconds() <= 5
                    and row["policy_sha256"] == policy.digest
                    and row["evidence"].get("configuration_revision") == configuration["revision"],
                    "evaluation_note": "等待按当前配置重新评估；既有告警不自动恢复"
                    if row["policy_sha256"] != policy.digest
                    or row["evidence"].get("configuration_revision") != configuration["revision"]
                    else "",
                }
                for row in rows
            ],
            "worker_error": self.error,
            "effective_policy_sha256": policy.digest,
            "configuration_revision": configuration["revision"],
            "temperature_limits_configured": bool(policy.temperature_limits_c),
        }

    def events(self, robot_id, before_id=None, limit=50):
        query = select(alert_events).where(alert_events.c.robot_id == robot_id)
        if before_id is not None:
            query = query.where(alert_events.c.id < before_id)
        with self.store._read_lock, self.store.engine.connect() as connection:
            rows = (
                connection.execute(query.order_by(alert_events.c.id.desc()).limit(limit + 1))
                .mappings()
                .all()
            )
        return {
            "items": [
                {**row, "occurred_at": utc(row["occurred_at"]).isoformat()} for row in rows[:limit]
            ],
            "next_before_id": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                self.error = "中央告警评估失败；当前规则状态可能过期，请检查服务日志与数据库"
                import logging

                logging.getLogger(__name__).exception("central alert evaluation failed")
            self.stop_event.wait(1)

    def start(self):
        if self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="central-alert-observer", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
