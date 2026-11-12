"""Current local motion duration requirements, separate from recorded evidence."""

import math

STANDING_MINIMUM_SECONDS = 7200


def duration_requirement(test_type, duration):
    """Return a duration-only assessment; it never certifies motion or performance."""
    if test_type not in {"standing", "walking", "running", "stunt"}:
        raise ValueError("未知动作记录类型，不能判断时长要求")
    if test_type != "standing":
        return {"duration_required": False, "minimum_test_seconds": None,
                "duration_satisfied": None}
    known = type(duration) in (int, float) and math.isfinite(duration) and duration >= 0
    return {"duration_required": True, "minimum_test_seconds": STANDING_MINIMUM_SECONDS,
            "duration_satisfied": bool(known and duration >= STANDING_MINIMUM_SECONDS)}


def duration_status(summary):
    if not summary["duration_required"]:
        return "无两小时时长要求"
    if summary["duration_seconds"] is None:
        return "站立时长未知"
    return "站立记录达到2小时" if summary["duration_satisfied"] else "站立记录不足2小时"
