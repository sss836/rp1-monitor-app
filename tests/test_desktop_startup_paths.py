"""Exercise the actual run-wrapper ordering without starting a station or device."""

from types import SimpleNamespace

import pytest

from rp1_monitor import desktop
from rp1_monitor.desktop_diagnostics import desktop_failure_diagnostics
from rp1_monitor.station import prepare_state


def test_legacy_prelogin_diagnostics_reproduces_station_ownership_rejection(tmp_path):
    station = tmp_path / "legacy-station"
    station.mkdir(mode=0o700)
    with desktop_failure_diagnostics(station):
        with pytest.raises(ValueError, match="拒绝接管"):
            prepare_state(station)
    assert not (station / "station-owner.json").exists()


def test_actual_run_wrapper_keeps_diagnostics_outside_first_station(tmp_path, monkeypatch):
    station = tmp_path / "station"
    entered = []

    def first_authenticated_stub(args):
        assert not station.exists(), "pre-login diagnostics must not create station state"
        entered.append(prepare_state(args.station_dir))

    monkeypatch.setattr(desktop, "_run_desktop", first_authenticated_stub)
    desktop.run(SimpleNamespace(station_dir=station))
    assert entered == [station.resolve()]
    assert (station / "station-owner.json").is_file()
    assert not (station / "desktop-diagnostics").exists()
    logs = list((tmp_path / "station-desktop-diagnostics").glob("desktop-*.log"))
    assert len(logs) == 1 and "desktop_exit=normal" in logs[0].read_text()
