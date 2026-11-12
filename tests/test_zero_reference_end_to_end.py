import json
from contextlib import closing
from uuid import uuid4

import pytest

from rp1_monitor import calibration_mapping, joint_angle_monitor
from rp1_monitor.factory_store import FactoryStore
from rp1_monitor.postzero import CONFIGURATION_SHA256
from rp1_monitor.postzero_transport import _JointMonitorEvents
from rp1_monitor.zero_reference_check import ZeroReferenceCheck


def test_all_24_axes_recheck_from_stream_and_archive_independently(tmp_path):
    sn, robot, dof = "TEST-ZERO", "RP1-TEST-REPLAY", 24
    session = str(uuid4())
    digest = CONFIGURATION_SHA256[dof]
    binding = calibration_mapping._encoder_observation_binding(
        calibration_mapping.version_bound_mapping(dof), policy_gains=False
    )
    binding["source_verified"] = True
    events = _JointMonitorEvents(session, dof)

    def event(kind, **fields):
        return events.read(json.dumps({"type": kind, "session_id": session, **fields}).encode())

    event("metadata", dof=dof, joint_names=binding["joint_names"], configuration_sha256=digest)
    check = ZeroReferenceCheck(dof)
    sequence, previous_ankles = 0, {}
    for index in range(dof):
        assert check.report()["current_index"] == index
        check.arm()
        for round_index in range(4):
            sequence += 1
            sweep_start = sequence * 1_000_000_000
            # The first sweep might have started before the operator's confirmation.
            raw = 12.0 if round_index == 0 else 0.0
            samples = {motor: {"angle_deg": raw, "rx_monotonic_ns": sweep_start + (motor + 1) * 35_000_000}
                       for motor in range(dof)}
            degrees, times = joint_angle_monitor._logical_positions(binding, samples, previous_ankles)
            check.feed(event("frame", sequence=sequence, frame_monotonic_ns=sweep_start + 900_000_000,
                             sweep_s=0.9, positions_deg=degrees, rx_monotonic_ns=times,
                             raw_angles_deg=[raw] * dof,
                             raw_rx_monotonic_ns=[sample["rx_monotonic_ns"] for sample in samples.values()],
                             raw_received_at=["2026-11-12T00:00:00.000000001Z"] * dof))

    report = check.report()
    assert report["phase"] == "complete"
    assert all(row["status"] == "passed" and row["sample_count"] == 3 for row in report["results"])
    by_joint = {row["joint"]: row for row in report["results"]}
    assert by_joint["left_shoulder_pitch_joint"]["logical_measured_deg"] == pytest.approx(165.550425)
    assert by_joint["waist_yaw_joint"]["logical_measured_deg"] == pytest.approx(-166.742178)
    assert by_joint["left_ankle_pitch_joint"]["logical_measured_deg"] == pytest.approx(35.001488)
    assert by_joint["left_ankle_pitch_joint"]["raw_angles_deg"] == [0.0, 0.0]
    assert all(sample["raw_angles_deg"] == [0.0, 0.0]
               for sample in by_joint["left_ankle_pitch_joint"]["samples"])

    data = dict(kind="zero_reference_check", sn=sn, robot_id=robot, dof=dof, preview_source="ros2",
                stream_id=str(uuid4()), run_id=str(uuid4()), configuration_sha256=digest,
                saved_at="2026-11-12T00:10:00+00:00", report=report, end_reason="离线测试回放")
    with closing(FactoryStore(tmp_path / "factory")) as store:
        store.save_zero_reference_check(sn, data, robot)
        archived = store.zero_reference_check(sn)
        assert archived["report"] == report
        assert store.calibration_check(sn) is None
        assert store.angle_calibration_check(sn) is None
