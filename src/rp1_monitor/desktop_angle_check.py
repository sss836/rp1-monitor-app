"""Angle observations from the ROS preview, read-only encoders, or a manual measurement."""

import json
import math
from copy import deepcopy
from datetime import datetime, timezone

from PySide6.QtCore import QIODevice, QSaveFile

from .manual_calibration_check import MAX_ENCODER_FRAME_GAP_S, ManualCalibrationCheck


class EncoderAngleCheck:
    """Check actual joint positions using the robot's monotonic receive clock."""

    def __init__(self, plan, *, hold_s, position_span_deg):
        self.engine = ManualCalibrationCheck(
            plan, {'hold_s': hold_s, 'position_span_deg': position_span_deg},
            max_frame_gap_s=MAX_ENCODER_FRAME_GAP_S,
        )
        self._origin_ns = self._last_rx_ns = None
        self.pin_confirmation_pending = False
        self.last_evidence = None
        self._peak_evidence = None
        self.passed_evidence = {}
        self.failed_evidence = {}

    @property
    def evidence_count(self):
        return len(self.passed_evidence) + sum(len(attempts) for attempts in self.failed_evidence.values())

    def report(self):
        return self.engine.report()

    def feed(self, joint, position_rad, rx_monotonic_ns, evidence):
        index = self.engine.current_index
        if index is None:
            return self.report()
        current = self.engine.results[index]
        if current['status'] == 'failed':
            return self.report()
        previous_peak = current['attempts'][-1]['peak_angle_deg']
        if (joint != current['joint'] or type(position_rad) not in (int, float)
                or not -math.pi <= position_rad <= math.pi or type(rx_monotonic_ns) is not int
                or not 0 < rx_monotonic_ns <= 2**63 - 1 or not isinstance(evidence, dict)):
            return self.invalidate()
        if self._last_rx_ns is not None:
            if rx_monotonic_ns < self._last_rx_ns:
                return self.invalidate()
            if rx_monotonic_ns == self._last_rx_ns:
                return self.report()
        if self._origin_ns is None:
            self._origin_ns = rx_monotonic_ns
        self._last_rx_ns = rx_monotonic_ns
        elapsed_s = (rx_monotonic_ns - self._origin_ns) / 1_000_000_000

        if self.pin_confirmation_pending:
            # Treat the first new response as the confirmation boundary so an
            # in-flight read can never contribute to the post-confirmation hold.
            self.engine.confirm_pin(elapsed_s)
            self.pin_confirmation_pending = False
        positions, sample_times = [None] * self.engine.dof, [None] * self.engine.dof
        joint_index = current['joint_index']
        positions[joint_index], sample_times[joint_index] = position_rad, elapsed_s
        measurement = deepcopy(evidence) | {'position_rad': position_rad, 'rx_monotonic_ns': rx_monotonic_ns}
        report = self.engine.update(elapsed_s, positions, None, sample_times_s=sample_times)
        outcome = report['results'][index]
        if outcome['status'] == 'passed':
            self.passed_evidence[index] = measurement
        elif outcome['status'] == 'failed':
            self.failed_evidence.setdefault(index, {})[outcome['effective_attempt_id']] = measurement
        if outcome['status'] != 'active':
            self._peak_evidence = None
        elif outcome['direction'] in ('lower', 'upper') and outcome['attempts'][-1]['peak_angle_deg'] != previous_peak:
            self._peak_evidence = measurement
        self.last_evidence = measurement if report['current_index'] == index else None
        return report

    def invalidate(self):
        self.last_evidence = None
        if self.engine.phase == 'failed':
            return self.report()
        return self.engine.invalidate_measurement()

    def clear_site_confirmation(self):
        self.pin_confirmation_pending = False
        if not self.report()['paused']:
            self.engine.pause(True)
            self.engine.pause(False)
        return self.invalidate()

    def confirm_pin(self):
        report = self.report()
        current = report['current']
        if (not current or current.get('check_mode') != 'pin_zero' or current['status'] != 'active'
                or report['paused'] or current['pin_confirmed_at_s'] is not None or self.pin_confirmation_pending):
            raise ValueError('仅能在腰部插销零位检查中确认已插好销钉')
        self.pin_confirmation_pending = True
        return report

    def skip(self):
        self.pin_confirmation_pending = False
        self.last_evidence = None
        self._peak_evidence = None
        return self.engine.skip()

    def retest(self, index):
        report = self.engine.retest(index)
        self.pin_confirmation_pending = False
        self.last_evidence = None
        self._peak_evidence = None
        self.passed_evidence.pop(index, None)
        return report

    def can_mark_target_not_reached(self):
        return self._peak_evidence is not None and self.engine.can_mark_target_not_reached()

    def mark_target_not_reached(self):
        if not self.can_mark_target_not_reached():
            return self.report()
        index = self.engine.current_index
        report = self.engine.mark_target_not_reached()
        attempt_id = report['results'][index]['effective_attempt_id']
        self.failed_evidence.setdefault(index, {})[attempt_id] = self._peak_evidence
        self.last_evidence = self._peak_evidence = None
        return report


class AngleObservations:
    def __init__(self, pose, identity):
        self.pose, self.identity = pose, identity
        self.records = []
        self.encoder_samples = []

    def reading(self, dof, joint):
        pose = self.pose(dof)
        if (pose['robot_id'], pose['source_kind'], pose['stream_id']) != self.identity:
            return None
        age = pose['topic_age_ms']
        raw_age = pose['topic_ages_ms']['/joint_states']
        if (not pose['connected'] or not pose['mapping_valid'] or pose['source_kind'] != 'ros2'
                or age is None or age > pose['max_age_ms'] or raw_age is None):
            return None
        for row in pose['rows']:
            if (row['name'] == joint and row['status'] == 'fresh' and row['raw_age_ms'] is not None
                    and math.isclose(row['raw_age_ms'], raw_age, rel_tol=0, abs_tol=.001)):
                return {'source': 'ros2_joint_states', 'position_rad': row['position_rad'],
                        'frame_sampled_at': pose['sampled_at'], 'receive_age_ms': row['raw_age_ms']}
        return None

    def capture(self, row, measurement):
        ref = math.degrees(row['requested_rad'])
        degrees = self.degrees(measurement)
        record = {'joint': row['joint'], 'direction': row['direction'], 'reference_deg': ref,
                  'difference_deg': degrees - ref, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                  **measurement}
        self.records.append(record)
        return record

    @staticmethod
    def degrees(measurement):
        return (measurement['position_deg'] if 'position_deg' in measurement
                else math.degrees(measurement['position_rad']))

    def payload(self, sn, dof, *, automatic=None, run_id=None):
        robot, source, stream = self.identity
        data = {'kind': 'angle_observations', 'sn': sn, 'robot_id': robot, 'preview_source': source,
                'stream_id': stream, 'dof': dof, 'records': self.records,
                'encoder_samples': self.encoder_samples}
        if automatic is not None:
            if run_id is not None:
                data.update(run_id=run_id, saved_at=datetime.now(timezone.utc).isoformat())
            data['automatic_encoder_check'] = {
                'source': 'version_bound_encoder_observation',
                'hardware_acceptance': False,
                'report': automatic.report(),
                'passing_samples': automatic.passed_evidence,
                'failing_samples': automatic.failed_evidence,
            }
        return deepcopy(data)

    def export(self, path, sn, dof, *, automatic=None, run_id=None):
        data = self.payload(sn, dof, automatic=automatic, run_id=run_id)
        raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
        file = QSaveFile(str(path))
        if not file.open(QIODevice.OpenModeFlag.WriteOnly) or file.write(raw) != len(raw) or not file.commit():
            raise OSError(file.errorString())
