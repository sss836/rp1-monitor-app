"""Offline evidence and boundary tests for the read-only mechanical zero check."""

import pytest

from rp1_monitor.zero_reference_check import ZeroReferenceCheck


def _sample(check, sequence, raw, *, skew_ns=35_000_000):
    row = check.report()["current"]
    stamp = sequence * 1_000_000_000
    frame = dict(sequence=sequence, frame_monotonic_ns=stamp, raw_angles_deg=[None] * check.dof,
                 raw_rx_monotonic_ns=[None] * check.dof, raw_received_at=[None] * check.dof)
    for offset, (index, angle) in enumerate(zip(row["motor_indices"], raw)):
        if angle is not None:
            frame["raw_angles_deg"][index] = angle
            frame["raw_rx_monotonic_ns"][index] = stamp - 250_000_000 + offset * skew_ns
            frame["raw_received_at"][index] = "2026-11-12T01:00:00.123456789Z"
    return frame


@pytest.mark.parametrize("dof", [24, 28])
def test_official_zero_reference_respects_offsets_signs_and_ankle_pair(dof):
    rows = {row["joint"]: row for row in ZeroReferenceCheck(dof).report()["results"]}
    assert len(rows) == dof
    assert rows["waist_roll_joint"]["logical_reference_deg"] == 0
    assert rows["waist_yaw_joint"]["logical_reference_deg"] == pytest.approx(-166.74, abs=0.01)
    assert rows["left_shoulder_pitch_joint"]["logical_reference_deg"] == pytest.approx(165.55, abs=0.01)
    assert rows["right_ankle_pitch_joint"]["logical_reference_deg"] == pytest.approx(35.0, abs=0.01)
    assert rows["right_ankle_roll_joint"]["logical_reference_deg"] == pytest.approx(15.0, abs=0.01)
    assert rows["right_ankle_pitch_joint"]["motor_indices"] == rows["right_ankle_roll_joint"]["motor_indices"]
    if dof == 28:
        assert rows["right_wrist_pitch_joint"]["logical_reference_deg"] == 0
        assert rows["left_wrist_yaw_joint"]["logical_reference_deg"] == 0


def test_pass_after_three_unique_post_confirmation_sweeps_and_auto_advance():
    check = ZeroReferenceCheck(24)
    check.feed(_sample(check, 1, [100]))
    check.arm()
    assert check.feed(_sample(check, 2, [100]))["current"]["sample_count"] == 0
    first = _sample(check, 3, [2.5])
    assert check.feed(first)["current"]["sample_count"] == 1
    assert check.feed(first)["current"]["sample_count"] == 1
    assert check.feed(_sample(check, 4, [2.75]))["current"]["sample_count"] == 2
    result = check.feed(_sample(check, 5, [3.0]))
    assert result["current_index"] == 1 and result["phase"] == "awaiting_confirmation"
    row = result["results"][0]
    assert row["status"] == "passed" and row["raw_span_deg"] == [0.5]
    assert [sample["sequence"] for sample in row["samples"]] == [3, 4, 5]
    assert row["samples"][0]["raw_received_at"] == ["2026-11-12T01:00:00.123456789Z"]


def test_raw_out_of_tolerance_cannot_pass_and_report_uses_circular_delta():
    check = ZeroReferenceCheck(24)
    index = next(row["index"] for row in check.report()["results"] if row["joint"] == "left_shoulder_pitch_joint")
    check.retest(index)
    check.arm()
    for sequence in range(1, 5):
        result = check.feed(_sample(check, sequence, [20.0]))
    row = result["current"]
    assert result["current_index"] == index and row["status"] == "failed"
    assert row["logical_measured_deg"] < -170 and row["logical_delta_deg"] == pytest.approx(20.0)
    assert check.feed(_sample(check, 5, [0.0]))["current"]["status"] == "failed"
    result = check.continue_failed()
    assert result["current_index"] != index
    saved = result["results"][index]
    assert saved["status"] == "failed" and saved["raw_angles_deg"] == [20.0]
    assert [attempt["status"] for attempt in saved["attempts"]] == ["failed"]


def test_stale_pair_and_gap_reset_window_and_pause_preserves_partial_evidence():
    check = ZeroReferenceCheck(24)
    index = next(row["index"] for row in check.report()["results"] if row["joint"] == "left_ankle_pitch_joint")
    check.retest(index)
    check.arm()
    check.feed(_sample(check, 1, [0.0, 0.0]))
    assert check.feed(_sample(check, 2, [0.0, 0.0]))["current"]["sample_count"] == 1
    assert check.feed(_sample(check, 3, [0.0, None]))["current"]["sample_count"] == 0
    assert check.feed(_sample(check, 4, [0.0, 0.0], skew_ns=100_000_001))["current"]["sample_count"] == 0
    assert check.feed(_sample(check, 5, [0.0, 0.0], skew_ns=100_000_000))["current"]["sample_count"] == 1
    assert check.feed(_sample(check, 7, [0.0, 0.0]))["current"]["sample_count"] == 1
    paused = check.pause()
    assert paused["phase"] == "awaiting_confirmation" and paused["current"]["sample_count"] == 0
    attempt = paused["current"]["attempts"][-1]
    assert attempt["status"] == "interrupted" and attempt["samples"][0]["sequence"] == 7


def test_span_over_half_degree_fails_even_with_all_angles_inside_raw_tolerance():
    check = ZeroReferenceCheck(24)
    check.arm()
    for sequence, value in enumerate((0.0, -0.25, 0.0, 0.26), start=1):
        result = check.feed(_sample(check, sequence, [value]))
    assert result["phase"] == "failed" and result["current"]["raw_span_deg"] == [0.51]


def test_stopped_confirmed_pose_without_feedback_keeps_one_incomplete_attempt():
    check = ZeroReferenceCheck(24)
    check.arm()
    check.feed(_sample(check, 1, [None]))
    check.pause()
    attempts = check.pause()["current"]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "interrupted"
    assert attempts[0]["samples"] == [] and attempts[0]["sample_count"] == 0
    assert attempts[0]["raw_angles_deg"] is None and attempts[0]["logical_measured_deg"] is None
