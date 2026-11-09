"""Offscreen local-only summary rejudgment, no hardware or platform calls."""

import os
from contextlib import closing

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QInputDialog

from rp1_monitor.desktop_result_summary import ResultSummaryPanel
from rp1_monitor.factory_store import FactoryStore


def test_cancel_rejudgment_keeps_draft_and_original(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    with closing(FactoryStore(tmp_path / "factory")) as store:
        store.save_estop_pairing("SYNTHETIC-UI", "fail")
        panel = ResultSummaryPanel(store)
        panel.set_sn("SYNTHETIC-UI")
        panel._selected_key = "SYS-012"
        panel._edit_field("verdict", "通过")
        monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("", False))
        panel.save()
        assert not panel.busy
        assert panel._drafts[panel.sn]["SYS-012"]["fields"]["verdict"] == "通过"
        assert next(row for row in store.report_rows(panel.sn) if row["row_key"] == "SYS-012")["verdict"] == "不通过"
        assert "未保存" in panel.notice.text()
        panel.close()
        panel.deleteLater()
        app.processEvents()
