"""Read-only component history; statistics aggregate raw windows before decimation."""

import math
import re

from sqlalchemy import and_, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from .models import metric_max_age_ms


def component_windows(store, robot_id, start, end):
    """Parse each large PostgreSQL JSON evidence row once for this bounded read.

    The original JSON and its SHA256 remain untouched. MATERIALIZED prevents
    PostgreSQL from inlining and repeating the cast for every aggregate/CASE.
    SQLite keeps its existing JSON expressions.
    """
    from .storage import history

    if store.engine.dialect.name != "postgresql":
        return history
    return (
        select(
            history.c.id,
            history.c.robot_id,
            history.c.window_start,
            history.c.window_end,
            cast(history.c.payload, JSONB).label("payload"),
        )
        .where(
            history.c.robot_id == robot_id,
            history.c.window_end >= start,
            history.c.window_end < end,
        )
        .cte("component_windows")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )


def metric_expressions(document, payload, metric):
    """RMS evidence is mandatory even for historical data imported by older code."""
    item = document[metric]
    value, low, high = (item[name].as_float() for name in ("last", "minimum", "maximum"))
    count = func.coalesce(item["count"].as_integer(), 0)
    squares = item["sum_squares"].as_float()
    if metric.endswith(".tracking_rms"):
        quality = payload["quality"]
        ages = quality["topic_ages_ms"]
        valid = and_(
            count > 0,
            squares >= 0,
            item["pairing"].as_string() == "same_sample",
            item["age_ms"].as_float() >= 0,
            item["age_ms"].as_float() <= metric_max_age_ms(metric),
            or_(
                payload["source_kind"].as_string() == "simulated",
                and_(
                    quality["target_alignment"]
                    .as_string()
                    .in_(("matched_names", "rp1_named_index")),
                    ages["/joint_states"].as_float() >= 0,
                    ages["/joint_states"].as_float() <= 1000,
                    ages["/action"].as_float() >= 0,
                    ages["/action"].as_float() <= 1000,
                ),
            ),
        )
        return (
            case((valid, value)),
            case((valid, low)),
            case((valid, high)),
            case((valid, count), else_=0),
            case((valid, squares)),
        )
    return value, low, high, count, squares


def raw_statistics(store, robot_id, metrics, start, end):
    source = component_windows(store, robot_id, start, end)
    columns = []
    for index, metric in enumerate(metrics):
        value, low, high, count, squares = metric_expressions(
            source.c.payload["metrics"], source.c.payload, metric
        )
        valid = count > 0
        columns.extend(
            [
                func.min(case((valid, func.coalesce(low, value)))).label(f"lo_{index}"),
                func.max(case((valid, func.coalesce(high, value)))).label(f"hi_{index}"),
                func.sum(count).label(f"n_{index}"),
                func.sum(squares).label(f"sse_{index}"),
            ]
        )
    query = select(*columns).where(
        source.c.robot_id == robot_id,
        source.c.window_end >= start,
        source.c.window_end < end,
    )
    with store._read_lock, store.engine.connect() as connection:
        row = connection.execute(query).mappings().one()
    return {
        metric: {
            "minimum": row[f"lo_{index}"],
            "maximum": row[f"hi_{index}"],
            "samples": row[f"n_{index}"] or 0,
            "sum_squares": row[f"sse_{index}"],
        }
        for index, metric in enumerate(metrics)
    }


def component_history(store, robot_id, component, joint, start, end, max_points, thermal=None):
    from .storage import latest, robots

    with store._read_lock, store.engine.connect() as connection:
        robot = (
            connection.execute(
                select(robots.c.source_kind, latest.c.payload)
                .select_from(robots.outerjoin(latest, latest.c.robot_id == robots.c.robot_id))
                .where(robots.c.robot_id == robot_id)
            )
            .mappings()
            .first()
        )
    if robot is None:
        raise LookupError("机器人不存在")
    if thermal is not None and (
        component != "host" or not re.fullmatch(r"host\.thermal\.\d{1,2}\.temperature", thermal)
    ):
        raise ValueError("热区参数仅允许主机已登记的thermal温度指标")
    thermal_metrics, selected_thermal = [], None
    if component == "battery":
        metrics = [f"battery.{name}" for name in ("soc", "temperature", "voltage", "current")]
    elif component == "joints":
        metrics = [
            f"joint.{joint}.{name}"
            for name in (
                "motor_temperature",
                "mos_temperature",
                "torque",
                "tracking_rms",
            )
        ]
    else:
        payload = robot["payload"] or {}
        catalog = set(payload.get("metrics", {})) | set(
            payload.get("quality", {}).get("health_sensors", {})
        )
        thermal_metrics = sorted(
            (key for key in catalog if re.fullmatch(r"host\.thermal\.\d{1,2}\.temperature", key)),
            key=lambda key: int(key.split(".")[2]),
        )
        if thermal is not None and thermal not in thermal_metrics:
            raise ValueError("所选主机热区尚未登记或已不在当前采集清单中")
        selected_thermal = thermal or (thermal_metrics[0] if thermal_metrics else None)
        metrics = [
            "host.cpu.usage",
            "host.memory.usage",
            *([selected_thermal] if selected_thermal else []),
        ]
    statistic_metrics = (
        ["host.cpu.usage", "host.memory.usage", *thermal_metrics]
        if component == "host"
        else metrics
    )
    summaries = raw_statistics(store, robot_id, statistic_metrics, start, end)

    def extreme(metric, name):
        return summaries[metric][name]

    def peak(metric):
        values = [extreme(metric, name) for name in ("minimum", "maximum")]
        return (
            max(abs(value) for value in values if value is not None)
            if any(value is not None for value in values)
            else None
        )

    if component == "battery":
        stats = {
            "current_peak_abs": peak("battery.current"),
            "current_min": extreme("battery.current", "minimum"),
            "current_max": extreme("battery.current", "maximum"),
            "voltage_min": extreme("battery.voltage", "minimum"),
            "voltage_max": extreme("battery.voltage", "maximum"),
            "soc_min": extreme("battery.soc", "minimum"),
            "soc_max": extreme("battery.soc", "maximum"),
            "temperature_max": extreme("battery.temperature", "maximum"),
        }
    elif component == "joints":
        summary = summaries[f"joint.{joint}.tracking_rms"]
        stats = {
            "motor_temperature_max": extreme(f"joint.{joint}.motor_temperature", "maximum"),
            "mos_temperature_max": extreme(f"joint.{joint}.mos_temperature", "maximum"),
            "torque_peak_abs": peak(f"joint.{joint}.torque"),
            "tracking_rms": math.sqrt(summary["sum_squares"] / summary["samples"])
            if summary["samples"] and summary["sum_squares"] is not None
            else None,
            "tracking_samples": summary["samples"],
        }
    else:
        temperatures = [
            summaries[key]["maximum"]
            for key in statistic_metrics
            if key.startswith("host.thermal.")
        ]
        stats = {
            "cpu_peak": extreme("host.cpu.usage", "maximum"),
            "memory_peak": extreme("host.memory.usage", "maximum"),
            "temperature_peak": max(
                (value for value in temperatures if value is not None), default=None
            ),
        }
    return {
        "robot_id": robot_id,
        "source_kind": robot["source_kind"],
        "component": component,
        "joint": joint if component == "joints" else None,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "curves": [
            store.curves(robot_id, metric, start, end, max_points, component=True)
            for metric in metrics
        ],
        "stats": stats,
        "thermal_metrics": thermal_metrics,
        "selected_thermal": selected_thermal,
        "meaning": "统计来自查询范围内全部原始摘要窗口的极值，不取下采样最后值；缺失保持未知。"
        "跟踪RMS为有效同采样反馈相对策略目标的误差平方和/配对数开方，策略目标不是最终电机目标；"
        "ROS独立话题未建立精确配对时RMS未知。",
    }
