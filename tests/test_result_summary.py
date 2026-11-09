"""Standalone PC summary tests using temporary local evidence only."""

import csv
from contextlib import closing
from pathlib import Path

from rp1_monitor.factory_store import FactoryStore


def test_manual_final_version_survives_restart_and_exports_17_columns(tmp_path):
    directory, sn = tmp_path / "factory", "SYNTHETIC-PC-SN"
    with closing(FactoryStore(directory)) as store:
        store.save_estop_pairing(sn, "fail")
        row = next(row for row in store.report_rows(sn) if row["row_key"] == "SYS-012")
        assert row["verdict"] == "不通过" and "对频" in row["criterion"]
        store.save_report_edits(sn, [dict(row_key=row["row_key"], source_token=row["source_token"],
            reason="检修后核对证据，人工修正", fields=dict(actual_result="第一版人工结果", criterion="本批次人工确认对频正常",
                        operator="测试人员甲", verdict="通过", notes="第一版备注"))], station_id="PC-01")
        store.save_report_edits(sn, [dict(row_key=row["row_key"], source_token=row["source_token"],
            fields=dict(actual_result="最终人工结果", notes="最终备注"))], station_id="PC-01")

    with closing(FactoryStore(directory)) as store:
        row = next(row for row in store.report_rows(sn) if row["row_key"] == "SYS-012")
        assert row["edited"] and row["actual_result"] == "最终人工结果"
        assert store.db.execute("SELECT count(*) FROM report_edits").fetchone()[0] == 1
        assert store.estop_pairing(sn)["latest"]["status"] == "fail"
        path = Path(store.export_result_summary(sn, tmp_path / "export"))
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            exported = list(reader)
            assert reader.fieldnames == ["记录编号", "上位机编号", "样品编号", "机型", "测试体系",
                "测试分类", "记录层级", "测试项目编号", "原始检查ID", "测试内容", "开始时间",
                "结束时间", "测试人员", "实际结果", "通过标准", "是否通过", "备注"]
        final = next(entry for entry in exported if entry["测试项目编号"] == "SYS-012")
        assert final["记录编号"] == row["record_id"]
        assert final["上位机编号"] == "PC-01" and final["样品编号"] == sn
        assert final["实际结果"] == "最终人工结果" and final["是否通过"] == "通过"
        assert final["通过标准"] == "本批次人工确认对频正常"
        assert final["测试人员"] == "测试人员甲" and final["备注"] == "最终备注"


def test_simulated_pairing_export_keeps_marker_after_notes_are_cleared(tmp_path):
    sn = "SYNTHETIC-SIMULATED-SN"
    with closing(FactoryStore(tmp_path / "factory")) as store:
        store.save_board_snapshot(sn, "SIM-PC-TEST", "simulated", {})
        store.save_estop_pairing(sn, "pass")
        row = next(row for row in store.report_rows(sn) if row["row_key"] == "SYS-012")
        assert row["source_kind"] == "simulated"
        store.save_report_edits(sn, [dict(row_key=row["row_key"], source_token=row["source_token"],
                                         fields={"notes": ""})])
        assert next(row for row in store.report_rows(sn) if row["row_key"] == "SYS-012")["notes"] == ""
        path = Path(store.export_result_summary(sn, tmp_path / "export"))
        with path.open(encoding="utf-8-sig", newline="") as stream:
            final = next(row for row in csv.DictReader(stream) if row["测试项目编号"] == "SYS-012")
        assert "simulated" in final["备注"] and "不代表实机验收" in final["备注"]


def test_rejudgment_audit_requires_reason_and_preserves_first_failure(tmp_path):
    import json
    import sqlite3

    import pytest

    sn = "SYNTHETIC-AUDIT"
    with closing(FactoryStore(tmp_path / "factory")) as store:
        store.save_estop_pairing(sn, "fail")
        first = next(row for row in store.report_rows(sn) if row["row_key"] == "SYS-012")
        edit = dict(row_key=first["row_key"], source_token=first["source_token"],
                    edit_token=first["edit_token"], fields={"verdict": "通过"})
        with pytest.raises(ValueError, match="原因"):
            store.save_report_edits(sn, [edit])
        edit["reason"] = "更正人工误判，保留失败原值"
        store.save_report_edits(sn, [edit])
        with pytest.raises(ValueError, match="人工结果已变化"):
            store.save_report_edits(sn, [edit])
        audit = store.db.execute("SELECT * FROM report_edit_history WHERE sn=?", (sn,)).fetchone()
        assert json.loads(audit["before_json"])["verdict"] == "不通过"
        assert json.loads(audit["after_json"])["verdict"] == "通过"
        assert audit["actor"].startswith("local-os:") and audit["changed_at"]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with store.db:
                store.db.execute("DELETE FROM report_edit_history")
        store.save_estop_pairing(sn, "pass")
        second = next(row for row in store.report_rows(sn) if row["row_key"] == "SYS-012")
        assert second["record_id"] != first["record_id"] and not second["edited"]
        versions = store.db.execute("SELECT * FROM report_source_history WHERE sn=? AND row_key='SYS-012' ORDER BY sequence", (sn,)).fetchall()
        assert len(versions) == 2
        assert versions[1]["previous_record_id"] == first["record_id"]
        assert json.loads(versions[0]["snapshot"])["verdict"] == "不通过"
        path = Path(store.export_result_summary(sn, tmp_path / "export"))
        manifest = json.loads(Path(str(path) + ".trace.json").read_text())
        import hashlib
        assert manifest["csv_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert manifest["schema"] == "rp1.summary-trace.v1"
        assert manifest["edit_history"][0]["reason"] == edit["reason"]
        assert manifest["formal_statistics_eligible"] is False


def test_legacy_edits_are_preserved_once_and_labeled_unknown(tmp_path):
    import json
    import sqlite3

    directory = tmp_path / "factory"
    sn = "SYNTHETIC-LEGACY"
    with closing(FactoryStore(directory)) as store:
        row = store.report_rows(sn)[0]
    with sqlite3.connect(directory / "factory-index.sqlite") as db:
        db.execute("INSERT INTO report_edits VALUES(?,?,?,?)", (sn, row["row_key"], row["source_token"], json.dumps({"notes": "legacy"})))
    for _ in range(2):
        with closing(FactoryStore(directory)) as store:
            assert store.report_rows(sn)[0]["notes"] == "legacy"
            records = store.db.execute("SELECT * FROM report_edit_history").fetchall()
            assert len(records) == 1
            assert records[0]["actor"] == "unknown:legacy"
            assert records[0]["origin"] == "legacy_import"


def test_export_collision_preserves_prior_csv_and_trace(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    import rp1_monitor.factory_result_summary as summary

    monkeypatch.setattr(summary, "uuid4", lambda: SimpleNamespace(hex="same-export-id"))
    with closing(FactoryStore(tmp_path / "factory")) as store:
        path = Path(store.export_result_summary("SYNTHETIC-COLLISION", tmp_path / "exports"))
        sidecar = Path(str(path) + ".trace.json")
        before = path.read_bytes(), sidecar.read_bytes()
        with pytest.raises(FileExistsError):
            store.export_result_summary("SYNTHETIC-COLLISION", tmp_path / "exports")
        assert (path.read_bytes(), sidecar.read_bytes()) == before


def test_invalid_batch_does_not_partially_write_history(tmp_path):
    import pytest

    with closing(FactoryStore(tmp_path / "factory")) as store:
        rows = store.report_rows("SYNTHETIC-BATCH")
        edits = [dict(row_key=row["row_key"], source_token=row["source_token"], fields={"notes": "new"}) for row in rows[:2]]
        edits[1]["source_token"] = "stale"
        with pytest.raises(ValueError, match="证据已变化"):
            store.save_report_edits("SYNTHETIC-BATCH", edits)
        assert store.db.execute("SELECT count(*) FROM report_edit_history").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM report_edits").fetchone()[0] == 0


def test_export_snapshot_excludes_interleaved_other_connection_edit(tmp_path, monkeypatch):
    """A second process cannot commit between CSV rows and audit-history reads."""
    import json
    import sqlite3

    import pytest

    import rp1_monitor.factory_result_summary as summary

    directory, sn = tmp_path / "factory", "SYNTHETIC-TWO-CONNECTIONS"
    with closing(FactoryStore(directory)) as exporter, closing(FactoryStore(directory)) as editor:
        exporter.save_estop_pairing(sn, "fail")
        row = next(row for row in exporter.report_rows(sn) if row["row_key"] == "SYS-012")
        edit = dict(row_key=row["row_key"], source_token=row["source_token"],
                    edit_token=row["edit_token"], fields={"verdict": "通过"}, reason="第二窗口核实后改判")
        editor.db.execute("PRAGMA busy_timeout=0")
        original_manifest = summary.trace.manifest
        interleaved = []

        def manifest_while_other_connection_edits(*args):
            assert exporter.db.in_transaction
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                editor.save_report_edits(sn, [edit])
            interleaved.append(True)
            return original_manifest(*args)

        monkeypatch.setattr(summary.trace, "manifest", manifest_while_other_connection_edits)
        path = Path(exporter.export_result_summary(sn, tmp_path / "exports"))
        assert interleaved == [True]
        assert not exporter.db.in_transaction
        # The lock is not retained across file delivery or later operator work.
        editor.save_report_edits(sn, [edit])
        with path.open(encoding="utf-8-sig", newline="") as stream:
            exported = next(row for row in csv.DictReader(stream) if row["测试项目编号"] == "SYS-012")
        audit = json.loads(Path(str(path) + ".trace.json").read_text())
        exported_row = next(row for row in audit["rows"] if row["row_key"] == "SYS-012")
        assert exported["是否通过"] == exported_row["verdict"] == "不通过"
        assert not audit["edit_history"]
        current = next(row for row in exporter.report_rows(sn) if row["row_key"] == "SYS-012")
        assert current["verdict"] == "通过"


def test_repeated_export_of_unchanged_snapshot_has_identical_trace(tmp_path):
    import json

    with closing(FactoryStore(tmp_path / "factory")) as store:
        store.save_estop_pairing("SYNTHETIC-REEXPORT", "fail")
        first = Path(store.export_result_summary("SYNTHETIC-REEXPORT", tmp_path / "exports"))
        second = Path(store.export_result_summary("SYNTHETIC-REEXPORT", tmp_path / "exports"))
        assert first != second
        assert first.read_bytes() == second.read_bytes()
        first_trace = Path(str(first) + ".trace.json").read_bytes()
        second_trace = Path(str(second) + ".trace.json").read_bytes()
        assert first_trace == second_trace
        audit = json.loads(first_trace)
        assert audit["created_at"] == max(row["observed_at"] for row in audit["source_history"])


def test_rule_digest_tracks_cached_rules_not_replaced_file(tmp_path, monkeypatch):
    import hashlib
    import json

    import rp1_monitor.factory_result_summary as summary

    summary._rules.cache_clear()
    try:
        frozen = summary._rules()
        path = Path(summary.__file__).parent / "data/factory-report-rules.json"
        original = path.read_bytes()
        replacement = original + b"\n"
        read_bytes = Path.read_bytes

        def replaced_rules_file(candidate):
            return replacement if candidate == path else read_bytes(candidate)

        monkeypatch.setattr(Path, "read_bytes", replaced_rules_file)
        with closing(FactoryStore(tmp_path / "factory")) as store:
            exported = Path(store.export_result_summary("SYNTHETIC-RULE-REPLACEMENT", tmp_path / "exports"))
            audit = json.loads(Path(str(exported) + ".trace.json").read_text())
        assert summary._rules() is frozen
        assert audit["rules_sha256"] == hashlib.sha256(original).hexdigest()
        assert audit["rules_sha256"] != hashlib.sha256(replacement).hexdigest()
    finally:
        summary._rules.cache_clear()
