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
            fields=dict(actual_result="第一版人工结果", criterion="本批次人工确认对频正常",
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
