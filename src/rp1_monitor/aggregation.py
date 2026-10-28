"""Observe every incoming sample; render frequency never controls aggregation."""

import math
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .models import (
    DataQuality,
    HealthSourceStatus,
    MetricRollup,
    PreviewFrame,
    TelemetryWindow,
    metric_max_age_ms,
    metric_spec,
)

TOPICS = ("/joint_states", "/action", "/imu")


class Accumulator:
    def __init__(self, robot_id: str, source_kind: str):
        self.robot_id = robot_id
        self.source_kind = source_kind
        self.stream_id = uuid4()
        self.sequence = 0
        self._lock = threading.RLock()
        self._anchor_ns = time.monotonic_ns()
        self._anchor_utc = datetime.now(timezone.utc)
        self._start_ns = self._anchor_ns
        self._latest = {}
        self._buckets = {}
        self._tracking_squares = {}
        self._topics = {}
        self._counts = {}
        self._stamps = {}
        self._invalid = 0
        self._joint_names = []
        self._target_alignment = "unverified"
        self._mapping_note = ""
        self._health_sources = {}
        self._health_sensors = {}
        self._processes = []
        self._process_valid = set()
        self._process_received_ns = None
        self.device_profile = None

    def observe_processes(self, observations, values):
        with self._lock:
            # Clear old instance values atomically; a restart must warm CPU up again.
            self._process_valid = set(values)
            self._processes = list(observations)
            self._process_received_ns = time.monotonic_ns()
            self.observe("process.health", values)

    def process_snapshot(self, now):
        elapsed = (now - self._process_received_ns) / 1e6 if self._process_received_ns else 0
        return [
            row.model_copy(
                update={"age_ms": row.age_ms + elapsed if row.age_ms is not None else None}
            )
            for row in self._processes
        ]

    def set_health_source(self, name, state, detail, *, reported_faults=None, observed_at=None):
        with self._lock:
            self._health_sources[name] = HealthSourceStatus(
                state=state,
                detail=detail,
                reported_faults=reported_faults or [],
                observed_at=observed_at,
            )

    def set_health_sensors(self, sensors):
        with self._lock:
            self._health_sensors = dict(sensors)

    def set_mapping(self, names, alignment, note):
        with self._lock:
            self._joint_names = list(names)
            self._target_alignment = alignment
            self._mapping_note = note

    def at(self, monotonic_ns: int) -> datetime:
        return self._anchor_utc + timedelta(seconds=(monotonic_ns - self._anchor_ns) / 1e9)

    def observe(
        self, topic: str, values: dict[str, float], publisher_stamp=None, *, event_age_ms=0
    ):
        with self._lock:
            now = time.monotonic_ns()
            observed = now - int(event_age_ms * 1e6)
            self._topics[topic] = max(self._topics.get(topic, observed), observed)
            self._counts[topic] = self._counts.get(topic, 0) + 1
            self._stamps[topic] = publisher_stamp
            for key, value in values.items():
                if key.endswith(".tracking_rms"):
                    # A last target and a last feedback value are not a paired sample.
                    self._invalid += 1
                    continue
                if not math.isfinite(value):
                    self._invalid += 1
                    continue
                metric_spec(key)
                if key in self._latest and observed < self._latest[key][1]:
                    continue
                self._latest[key] = (float(value), observed)
                bucket = self._buckets.setdefault(
                    key, [0, float(value), float(value), float(value)]
                )
                bucket[0] += 1
                bucket[1] = float(value)
                bucket[2] = min(bucket[2], value)
                bucket[3] = max(bucket[3], value)

    def observe_tracking(self, pairs: dict[int, tuple[float, float]], *, event_age_ms=0):
        """Record explicit same-sample simulation pairs; ROS has no synchronizer yet.

        This cannot be called with two cached topic last values. The concrete
        simulated source passes the feedback and policy target from one loop.
        Name alignment alone never enables RMS for the independent ROS topics.
        """
        if (
            self.source_kind != "simulated"
            or not math.isfinite(event_age_ms)
            or not 0 <= event_age_ms <= 1000
        ):
            return False
        with self._lock:
            observed = time.monotonic_ns() - int(event_age_ms * 1e6)
            for joint, (feedback, target) in pairs.items():
                error = feedback - target
                squared = error * error
                if not isinstance(joint, int) or not 0 <= joint < 64 or not math.isfinite(squared):
                    self._invalid += 1
                    continue
                key = f"joint.{joint}.tracking_rms"
                if key in self._latest and observed < self._latest[key][1]:
                    continue
                total_squares = self._tracking_squares.get(key, 0.0) + squared
                if not math.isfinite(total_squares):
                    self._invalid += 1
                    continue
                magnitude = abs(error)
                bucket = self._buckets.setdefault(key, [0, magnitude, magnitude, magnitude])
                bucket[0] += 1
                self._tracking_squares[key] = total_squares
                bucket[1] = math.sqrt(self._tracking_squares[key] / bucket[0])
                bucket[2] = min(bucket[2], magnitude)
                bucket[3] = max(bucket[3], magnitude)
                self._latest[key] = (bucket[1], observed)
        return True

    def preview(self) -> PreviewFrame:
        with self._lock:
            now = time.monotonic_ns()
            ages = {key: (now - received) / 1e6 for key, (_, received) in self._latest.items()}
            return PreviewFrame(
                device_profile=self.device_profile,
                robot_id=self.robot_id,
                stream_id=self.stream_id,
                source_kind=self.source_kind,
                sampled_at=self.at(now),
                values={
                    key: value
                    if ages[key] <= metric_max_age_ms(key)
                    and (not key.startswith("process.") or key in self._process_valid)
                    and not (
                        self.source_kind == "ros2"
                        and key.endswith(".policy_target")
                        and self._target_alignment == "unverified"
                    )
                    else None
                    for key, (value, _) in self._latest.items()
                },
                ages_ms=ages,
                topic_ages_ms={
                    topic: (now - self._topics[topic]) / 1e6 if topic in self._topics else None
                    for topic in set(TOPICS) | self._topics.keys()
                },
                catalog=[metric_spec(key) for key in sorted(self._latest)],
                joint_names=list(self._joint_names),
                target_alignment=self._target_alignment,
                mapping_note=self._mapping_note,
                health_sources=dict(self._health_sources),
                health_sensors=dict(self._health_sensors),
                processes=self.process_snapshot(now),
            )

    def flush(self) -> TelemetryWindow:
        with self._lock:
            now = time.monotonic_ns()
            self.sequence += 1
            metrics = {}
            for key, (_, received) in self._latest.items():
                count, last, low, high = self._buckets.get(key, [0, None, None, None])
                tracking = key.endswith(".tracking_rms")
                if tracking and (now - received) / 1e6 > 1000:
                    count, last, low, high = 0, None, None, None
                is_range = metric_spec(key)["aggregation"] == "range"
                metrics[key] = MetricRollup(
                    last=last,
                    minimum=low if is_range else None,
                    maximum=high if is_range else None,
                    count=count,
                    age_ms=(now - received) / 1e6,
                    sum_squares=self._tracking_squares.get(key) if tracking and count else None,
                    pairing="same_sample" if tracking and count else None,
                )
            end = self.at(now)
            result = TelemetryWindow(
                robot_id=self.robot_id,
                source_kind=self.source_kind,
                stream_id=self.stream_id,
                sequence=self.sequence,
                window_start=self.at(self._start_ns),
                window_end=end,
                metrics=metrics,
                quality=DataQuality(
                    device_profile=self.device_profile,
                    topic_counts=dict(self._counts),
                    topic_ages_ms={
                        topic: (now - self._topics[topic]) / 1e6 if topic in self._topics else None
                        for topic in set(TOPICS) | self._topics.keys()
                    },
                    publisher_timestamps=dict(self._stamps),
                    invalid_values=self._invalid,
                    clock_status="WALL_CLOCK_CHANGED"
                    if abs((datetime.now(timezone.utc) - end).total_seconds()) > 2
                    else "HOST_ANCHORED",
                    joint_names=list(self._joint_names),
                    target_alignment=self._target_alignment,
                    mapping_note=self._mapping_note,
                    health_sources=dict(self._health_sources),
                    health_sensors=dict(self._health_sensors),
                    processes=self.process_snapshot(now),
                    health_values={
                        key: value
                        for key, (value, _) in self._latest.items()
                        if metric_max_age_ms(key) > 1000
                        and (not key.startswith("process.") or key in self._process_valid)
                    },
                    health_ages_ms={
                        key: (now - received) / 1e6
                        for key, (_, received) in self._latest.items()
                        if metric_max_age_ms(key) > 1000
                    },
                ),
            )
            self._start_ns = now
            self._buckets.clear()
            self._tracking_squares.clear()
            self._counts.clear()
            self._invalid = 0
            return result
