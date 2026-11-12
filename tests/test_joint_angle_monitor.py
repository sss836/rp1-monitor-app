import io
import json
import math
from contextlib import contextmanager
from uuid import uuid4

import pytest

from rp1_monitor import calibration_mapping, joint_angle_monitor, postzero_reader


def test_read_only_sweeps_use_offsets_and_drop_missing_ankle_samples(monkeypatch, tmp_path):
    monitor, reader = joint_angle_monitor, postzero_reader
    binding = calibration_mapping._encoder_observation_binding(
        calibration_mapping.version_bound_mapping(24), policy_gains=False
    )
    binding["source_verified"] = True
    motors = binding["motors"]
    knee = next(row for row in binding["rows"] if row["name"] == "left_knee_joint")
    ankle = next(row for row in binding["rows"] if row["name"] == "left_ankle_pitch_joint")
    opened, queries, frames = [], [], []

    class Clock:
        ns = 1_000_000_000

        def monotonic_ns(self):
            return self.ns

    class Control:
        def check(self):
            if len(frames) == 2:
                raise reader.Cancelled()

        def pause(self, seconds):
            self.check()
            clock.ns += round(seconds * 1_000_000_000)

    @contextmanager
    def open_can(interface, ids):
        opened.append((interface, ids))
        yield interface

    def query(interface, motor_id, control, guard):
        motor = next(item for item in motors if (item["interface"], item["motor_id"]) == (interface, motor_id))
        index = motor["index"]
        sent = clock.ns
        queries.append((interface, motor_id, sent))
        clock.ns += 1_000_000
        if len(queries) > 24 and index in {knee["motor_index"], ankle["motor_indices"][0]}:
            return {"status": "missing", "query_sent_monotonic_ns": sent}
        target = math.radians(34) * knee["sign"] if index == knee["motor_index"] else 0
        return {"status": "fresh", "query_sent_monotonic_ns": sent, "rx_monotonic_ns": clock.ns,
                "angle_deg": math.degrees(target - motor["software_offset_rad"])}

    clock, output = Clock(), io.StringIO()
    write_event = reader.emit

    def emit(destination, event):
        write_event(destination, event)
        if event["type"] == "frame":
            frames.append(event)

    monkeypatch.setattr(monitor, "time", clock)
    monkeypatch.setattr(monitor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(monitor, "REALTIME_LOCK", str(tmp_path / "runtime.lock"))
    monkeypatch.setattr(reader, "MOTOR_CONTROL_LOCK", str(tmp_path / "motor.lock"))
    monkeypatch.setattr(reader, "SessionControl", lambda *_: Control())
    monkeypatch.setattr(reader, "restrict_affinity", lambda: [1])
    monkeypatch.setattr(reader, "check_processes", lambda **_: None)
    monkeypatch.setattr(reader, "load_configuration", lambda dof: (motors, "test-digest"))
    monkeypatch.setattr(calibration_mapping, "encoder_observation_mapping", lambda *_: binding)
    monkeypatch.setattr(reader, "open_can", open_can)
    monkeypatch.setattr(reader, "query_once", query)
    monkeypatch.setattr(reader, "emit", emit)

    assert monitor.run_session(24, uuid4(), stdin=object(), output=output) == 0
    metadata, first, second, complete = map(json.loads, output.getvalue().splitlines())
    assert [event["type"] for event in (metadata, first, second, complete)] == ["metadata", "frame", "frame", "complete"]
    assert len(opened) == 4 and len(queries) == 48
    assert min(after[2] - before[2] for before, after in zip(queries, queries[1:])) >= monitor.QUERY_INTERVAL_NS
    assert len(metadata["joint_names"]) == len(set(metadata["joint_names"])) == 24
    assert all(value is not None for value in first["positions_deg"])
    index = metadata["joint_names"].index("left_knee_joint")
    assert first["positions_deg"][index] == pytest.approx(34)
    missing = {"left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint"}
    assert {name for name, angle in zip(metadata["joint_names"], second["positions_deg"]) if angle is None} == missing
    assert {name for name, stamp in zip(metadata["joint_names"], second["rx_monotonic_ns"]) if stamp is None} == missing
    assert complete["cancelled"] is True
