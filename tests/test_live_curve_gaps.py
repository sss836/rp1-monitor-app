"""Real-time plots must show observed values and preserve unknown intervals."""

import math
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rp1_monitor.desktop import MonitorWindow


def test_live_curve_respects_metric_cadence_and_missing_data(monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MonitorWindow("http://127.0.0.1:1")
    monkeypatch.setattr(window.joint_inspection_panel, "set_frame", lambda _: None)
    monkeypatch.setattr(window.operations_panel, "set_frame", lambda _: None)
    monkeypatch.setattr(window, "render_health", lambda *_args, **_kwargs: None)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    temp = "host.thermal.0.temperature"
    joint = "joint.0.position"

    def frame(seconds, values, ages=None, **overrides):
        data = {
            "robot_id": "SIM-1", "source_kind": "simulated", "stream_id": "stream-1",
            "sampled_at": (start + timedelta(seconds=seconds)).isoformat(),
            "values": values, "ages_ms": ages if ages is not None else {key: 50 for key in values},
            "catalog": [], "mapping_note": "", "upload": {"collector_connected": True},
        }
        data.update(overrides)
        window.on_frame(data)

    frame(0, {temp: 47.0, joint: 1.0})
    frame(0.8, {temp: 48.0, joint: 1.1})
    assert [value for _, value in window.buffers[temp]] == [47.0, 48.0]
    assert math.isnan(window.buffers[joint][-2][1])

    frame(0.9, {temp: None, joint: 1.2})
    assert math.isnan(window.buffers[temp][-1][1])
    frame(1.0, {joint: 1.3})  # Omitted is unknown, not a held or zero value.
    assert math.isnan(window.buffers[temp][-1][1])
    frame(1.1, {temp: 49.0, joint: 1.4}, ages={temp: 5001, joint: 20})
    assert math.isnan(window.buffers[temp][-1][1])  # An expired value is not plotted.
    frame(1.2, {temp: 50.0, joint: 1.5}, upload={"collector_connected": False})
    assert math.isnan(window.buffers[temp][-1][1])
    frame(1.3, {temp: 51.0, joint: 1.6})
    frame(2.8, {temp: 52.0, joint: 1.7})
    assert math.isnan(window.buffers[temp][-2][1])  # A real preview gap remains visible.
    assert window.buffers[temp][-1][1] == 52.0

    frame(2.9, {temp: 53.0}, stream_id="stream-2")
    assert math.isnan(window.buffers[temp][-2][1])
    frame(3.0, {temp: 54.0}, source_kind="ros2", stream_id="stream-2")
    assert [value for _, value in window.buffers[temp]] == [54.0]
    window.close()
    app.processEvents()
