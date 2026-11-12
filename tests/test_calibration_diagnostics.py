"""Offline verification of build receipts; never imports a hardware SDK."""

import json

from rp1_monitor import calibration_runtime as runtime


def test_receipt_reports_source_and_artifact_changes_together(tmp_path, monkeypatch):
    root = tmp_path / "deploy"
    root.mkdir()
    receipt = tmp_path / "build.json"
    original = {
        "schema_version": 1,
        "build_succeeded": True,
        "root": str(root),
        "python_abi": runtime.python_abi(),
        "command": runtime.COLCON_COMMAND,
        "source_sha256": {"existing.cpp": "old", "removed.cpp": "old"},
        "artifacts": {"motors_py": {"sha256": "same"}, "robot_py": {"sha256": "old"}},
    }
    receipt.write_text(json.dumps(original))
    monkeypatch.setattr(runtime, "source_fingerprint", lambda _: {"existing.cpp": "new", "added.cpp": "new"})
    monkeypatch.setattr(runtime, "artifact_inventory", lambda _: {
        "motors_py": {"sha256": "same"}, "robot_py": {"sha256": "new"},
    })

    report = runtime.verify_runtime_receipt(root, receipt)
    assert report["status"] == "needs_build"
    assert not report["hardware_ready"]
    assert "新增1项、修改1项、删除1项" in report["reasons"][0]
    assert "robot_py" in report["reasons"][1]
    assert json.loads(receipt.read_text()) == original


def test_matching_receipt_is_only_code_verification(tmp_path, monkeypatch):
    root = tmp_path / "deploy"
    root.mkdir()
    receipt = tmp_path / "build.json"
    sources = {"driver.cpp": "hash"}
    artifacts = {"motors_py": {"sha256": "hash"}}
    receipt.write_text(json.dumps({
        "schema_version": 1, "build_succeeded": True, "root": str(root),
        "python_abi": runtime.python_abi(), "command": runtime.COLCON_COMMAND,
        "source_sha256": sources, "artifacts": artifacts,
    }))
    monkeypatch.setattr(runtime, "source_fingerprint", lambda _: sources)
    monkeypatch.setattr(runtime, "artifact_inventory", lambda _: artifacts)
    assert runtime.verify_runtime_receipt(root, receipt) == {
        "status": "code_built", "hardware_ready": False, "reasons": [],
    }


def test_missing_artifact_never_passes_when_sources_changed(tmp_path, monkeypatch):
    root = tmp_path / "deploy"
    root.mkdir()
    receipt = tmp_path / "build.json"
    receipt.write_text(json.dumps({
        "schema_version": 1, "build_succeeded": True, "root": str(root),
        "python_abi": runtime.python_abi(), "command": runtime.COLCON_COMMAND,
        "source_sha256": {"driver.cpp": "old"}, "artifacts": {},
    }))
    monkeypatch.setattr(runtime, "source_fingerprint", lambda _: {"driver.cpp": "new"})

    def missing(_):
        raise FileNotFoundError("robot_py")

    monkeypatch.setattr(runtime, "artifact_inventory", missing)
    result = runtime.verify_runtime_receipt(root, receipt)
    assert result["status"] == "needs_build"
    assert len(result["reasons"]) == 2
    assert "robot_py" in result["reasons"][1]
