"""A 2.0.3 image can be reassessed without rewriting the board's receipt."""

from copy import deepcopy

from rp1_monitor.factory_board_profile import default_board_profile, factory_board_view


def test_old_tui_baseline_failure_is_reassessed_with_complete_203_identity():
    snapshot = {
        "BOARD": "robopi2", "VENDOR": "roboparty-os", "KERNEL_TARGET": "current",
        "VERSION": "26.11.0-trunk-2.0.3", "REVISION": "26.11.0-trunk-2.0.3",
        "IMAGE_UUID": "e8713364-c43f-4b1b-b816-3ebf2939d39c", "BUILD_REPOSITORY_COMMIT": "4b8460569-dirty",
    }
    receipt = {"result": "fail", "details": {"snapshot": snapshot, "mismatches": {
        "VERSION": {"actual": snapshot["VERSION"], "expected": "26.11.0-trunk-2.0.1"},
    }}}
    original = deepcopy(receipt)
    profile = default_board_profile()
    assert factory_board_view("RP002", receipt, profile).label == "通过"
    assert receipt == original
    snapshot["IMAGE_UUID"] = "another-image"
    assert factory_board_view("RP002", receipt, profile).label == "不通过"
    del snapshot["IMAGE_UUID"]
    assert factory_board_view("RP002", receipt, profile).label == "未完成"
