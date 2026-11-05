"""Offline factory preview: read original bytes only; never devices or uploads."""

import csv
import hashlib
import io
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from rp1_monitor.factory_bridge import CSV_COLUMNS, BridgeError
from rp1_monitor.factory_preview import preview_result_csv
from rp1_monitor.factory_store import FactoryStore
from rp1_monitor.platform_client import NativePlatformError
from rp1_monitor.platform_workspace import NativePlatformService


def write_csv(path, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(rows)
    path.write_bytes(stream.getvalue().encode("utf-8-sig"))
    return path


def values(*, record="record-1", code="SYS-001", level="项目", original="", verdict="未测试"):
    return [record, "PC-01", "SN-01", "24轴", "SYS", "主控", level, code, original, "实际测试内容",
            "", "", "人员甲", "保留原实际结果", "保留原标准", verdict, "保留原备注"]


def test_preview_preserves_bytes_and_maps_original_project_child_and_verdicts(tmp_path):
    path = write_csv(tmp_path / "result.csv", [values(), values(record="child", level="子项", original="RP001", verdict="不通过"),
                                               values(record="other", code="SYS-011", verdict="不适用")])
    original, mtime = path.read_bytes(), path.stat().st_mtime_ns
    result = preview_result_csv(path)
    assert path.read_bytes() == original and path.stat().st_mtime_ns == mtime
    assert result["sha256"] == hashlib.sha256(original).hexdigest()
    assert result["source_id"] == "PC-01" and result["serial_number"] == "SN-01"
    assert result["model"] == "24轴" and result["operators"] == ["人员甲"]
    assert result["source_kind"] == "unknown" and not result["simulated_marker"]
    assert len(result["projects"]) == 12
    project = next(item for item in result["projects"] if item["case_code"] == "SYS-001")
    assert project["name"] == "基础自检（17项）"
    assert project["parent_indices"] == [0] and project["child_indices"] == [1]
    assert project["verdict_counts"] == {"未测试": 1, "不通过": 1}
    missing = next(item for item in result["projects"] if item["case_code"] == "SYS-012")
    assert not missing["present"] and missing["verdict_counts"] == {}
    assert result["rows"][2]["verdict"] == "不适用"
    assert result["rows"][1]["original_check_id"] == "RP001"
    assert result["rows"][0]["actual_result"] == "保留原实际结果"
    assert result["rows"][0]["criterion"] == "保留原标准"
    assert result["rows"][0]["evidence"] is None and not result["rows"][0]["evidence_available"]
    assert all("通过" not in item["verdict_counts"] for item in result["projects"])


def test_preview_real_factory_store_export_uses_same_31_rows_and_original_simulated_marker(tmp_path):
    with closing(FactoryStore(tmp_path / "source")) as store:
        store.save_board_snapshot("SYNTHETIC-SN", "robot-synthetic", "simulated", {})
        store.save_estop_pairing("SYNTHETIC-SN", "fail")
        row = next(item for item in store.report_rows("SYNTHETIC-SN") if item["row_key"] == "SYS-012")
        store.save_report_edits("SYNTHETIC-SN", [{"row_key": row["row_key"], "source_token": row["source_token"],
                                                 "fields": {"verdict": "待复核", "notes": "人工备注"}}], station_id="PC-01")
        path = Path(store.export_result_summary("SYNTHETIC-SN", tmp_path / "export"))
        before = path.read_bytes()
        preview = preview_result_csv(path)
        assert len(preview["rows"]) == 31
        assert preview["source_kind"] == "simulated" and preview["simulated_marker"]
        assert len(preview["projects"]) == 12
        assert path.read_bytes() == before
        assert next(item for item in preview["rows"] if item["case_code"] == "SYS-012")["verdict"] == "待复核"


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[0].__setitem__(1, "bad/source"),
    lambda rows: rows.append(values(record="other")[:2] + ["OTHER-SN"] + values(record="other")[3:]),
    lambda rows: rows.append(values(record="other")[:3] + ["28轴"] + values(record="other")[4:]),
    lambda rows: rows.append(values(record="other")[:1] + ["PC-02"] + values(record="other")[2:]),
    lambda rows: rows.append(values()),
    lambda rows: rows[0].__setitem__(7, "SYS-REL-001"),
    lambda rows: rows[0].__setitem__(6, "主项"),
    lambda rows: rows[0].__setitem__(15, "pass"),
    lambda rows: rows[0].__setitem__(14, "x" * 65537),
])
def test_preview_invalid_identity_schema_or_mapping_is_not_repaired(tmp_path, mutation):
    rows = [values()]
    mutation(rows)
    path = write_csv(tmp_path / "invalid.csv", rows)
    original = path.read_bytes()
    with pytest.raises(BridgeError):
        preview_result_csv(path)
    assert path.read_bytes() == original


def test_preview_refuses_symlink_size_header_and_more_than_1000_rows(tmp_path):
    path = write_csv(tmp_path / "result.csv", [values()])
    link = tmp_path / "link.csv"
    link.symlink_to(path)
    with pytest.raises(BridgeError):
        preview_result_csv(link)
    path.write_bytes(b"x" * (4 * 1024**2 + 1))
    with pytest.raises(BridgeError):
        preview_result_csv(path)
    path.write_text("wrong,header\n", encoding="utf-8")
    with pytest.raises(BridgeError):
        preview_result_csv(path)
    write_csv(path, [values(record=str(index)) for index in range(1001)])
    with pytest.raises(BridgeError):
        preview_result_csv(path)


def client(permission=True):
    def no_network(*args, **kwargs):
        raise AssertionError("preview or changed file must not use HTTP")

    return SimpleNamespace(base_url="https://platform.example", cancel_event=None,
                           session=lambda: {"authenticated": True, "user": {"id": "00000000-0000-4000-8000-000000000001", "must_change_password": False},
                                            "generation": 1, "can_submit_factory": permission},
                           call=no_network, clear=lambda: None, cancel_current_request=lambda: None)


def test_preview_action_requires_permission_and_never_enqueues_or_submits(tmp_path):
    service = NativePlatformService(tmp_path / "private")
    service.client = client()
    path = write_csv(tmp_path / "result.csv", [values()])
    preview = service.operation("factory_preview", {"path": str(path)})["preview"]
    assert preview["source_id"] == "PC-01"
    assert not (service.state_dir / "factory").exists()
    assert service.operation("tasks")["items"] == []
    service.client = client(False)
    with pytest.raises(NativePlatformError, match="forbidden"):
        service.operation("factory_preview", {"path": "/must/not/be/read.csv"})
    service.close()


def test_changed_after_preview_is_rejected_before_task_or_http(tmp_path):
    service = NativePlatformService(tmp_path / "private")
    service.client = client()
    path = write_csv(tmp_path / "result.csv", [values()])
    preview = service.operation("factory_preview", {"path": str(path)})["preview"]
    write_csv(path, [values(verdict="不通过")])
    with pytest.raises(NativePlatformError, match="factory_csv_changed_since_preview"):
        service.operation("factory_submit", {"path": str(path), "source_id": preview["source_id"], "robot_id": "robot-1",
                                              "source_kind": "real", "reason": "人工核实", "verified": True, "completed": True,
                                              "expected_sha256": preview["sha256"]})
    assert not (service.state_dir / "factory").exists()
    assert service.operation("tasks")["items"] == []
    service.close()
