"""Connection identity guidance stays readable without changing trust behavior."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rp1_monitor.app_shell import AppMonitorWindow


def test_fingerprint_layout_and_confirmation(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = AppMonitorWindow("http://127.0.0.1:1", factory_directory=tmp_path)
    window.resize(1000, 700)
    window.show()
    window.navigate(4)
    window.connection_mode.setCurrentIndex(2)
    app.processEvents()

    assert window.ssh_fingerprint.isVisible()
    assert window.ssh_fingerprint.height() >= window.ssh_fingerprint.minimumHeight()
    assert "正确机器人" in window.ssh_fingerprint_help.text()
    assert not window.ssh_trust.isVisible()

    window.station_fingerprint = "SHA256:" + "a" * 43
    window.ssh_fingerprint.setText(window.station_fingerprint)
    window.update_connection_mode()
    app.processEvents()
    assert window.ssh_trust.isVisible()
    assert not window.ssh_trust.isChecked()
    assert window.ssh_fingerprint.height() >= window.ssh_fingerprint.sizeHint().height()

    window.ssh_trust.setChecked(True)
    window.ssh_target.setText("robo@another-host")
    assert not window.ssh_trust.isChecked()
    assert not window.station_fingerprint
    window.close()
