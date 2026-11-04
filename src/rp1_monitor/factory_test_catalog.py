"""Display identifiers for whole-robot factory tests, independent of TUI IDs."""

SUITE_CODE = "SYS"
SUITE_NAME = "整机测试"
TEST_FAMILIES = {
    "SYS": "整机测试",
    "UNIT-AGING": "单元老化测试",
    "CTRL": "独立主控测试",
    "CHAIN": "串联测试",
    "HW-POWER": "硬件通电测试",
}
UNIT_AGING_CASES = {
    "left_leg": ("UNIT-AGING-001", "左腿"),
    "right_leg": ("UNIT-AGING-002", "右腿"),
    "waist_hip": ("UNIT-AGING-003", "腰髋"),
    "left_short_arm": ("UNIT-AGING-004", "左短臂"),
    "right_short_arm": ("UNIT-AGING-005", "右短臂"),
    "left_arm": ("UNIT-AGING-006", "左臂"),
    "right_arm": ("UNIT-AGING-007", "右臂"),
}
CHAIN_CASE_CODE = "CHAIN-001"
CONTROLLER_CASE_CODE = "CTRL-001"
HARDWARE_POWER_CASE_CODE = "HW-POWER-001"
CATEGORY_NAMES = {"CTRL": "主控测试", "CAL": "标定测试", "FUNC": "功能测试", "MOTION": "运控测试"}
BASIC_CASE_CODE = "SYS-001"
# Source IDs identify the seventeen child checks; the whole-robot project is one.
BOARD_CASE_CODES = dict.fromkeys((
    "RP001", "RP002", "RP003", "RP005", "RP007", "RP008", "RP011", "RP012", "RP013",
    "RP020", "RP021", "RP022", "RP029", "RP034", "IMU_CONTINUOUS", "IMU_FIRMWARE", "BMS_RECOVERY",
), BASIC_CASE_CODE)
FUNCTION_CASE_CODES = {"remote_estop": "SYS-007", "battery_hot_swap": "SYS-008"}
MOTION_CASE_CODES = {"standing": "SYS-009", "walking": "SYS-010", "running": "SYS-010", "stunt": "SYS-011"}
CALIBRATION_CASE_CODES = {2: "SYS-005", 3: "SYS-006"}
LIGHT_CASE_CODE = "SYS-002"
MOTOR_FIRMWARE_CASE_CODE = "SYS-003"
MOTOR_PARAMETERS_CASE_CODE = "SYS-004"
ESTOP_PAIRING_CASE_CODE = "SYS-012"


def standalone_catalog():
    """Number references only; never executes or archives standalone tests."""
    return [dict(family="UNIT-AGING", code=code, name=name)
            for code, name in UNIT_AGING_CASES.values()] + [
        dict(family="CHAIN", code=CHAIN_CASE_CODE, name="串联测试"),
        dict(family="CTRL", code=CONTROLLER_CASE_CODE, name="独立主控测试"),
        dict(family="HW-POWER", code=HARDWARE_POWER_CASE_CODE, name="硬件通电测试"),
    ]


def test_catalog(dof=24):
    from .factory_basic_checks import BASIC_CHECKS

    rows = [dict(code=BASIC_CASE_CODE, category="CTRL", name="基础自检（17项）", source_id="BASIC_CHECKS",
                 subchecks=[dict(source_id=key, name=BASIC_CHECKS[key]) for key in BOARD_CASE_CODES],
                 item=1, applicable=True)]
    rows.extend(dict(code=code, category="CTRL", name=name, source_id=source, item=1, applicable=True)
                for code, name, source in ((LIGHT_CASE_CODE, "灯板点亮确认", "light_observation"),
                    (MOTOR_FIRMWARE_CASE_CODE, "电机固件版本核查", "motor_firmware"),
                    (MOTOR_PARAMETERS_CASE_CODE, "电机参数核查", "motor_parameters"),
                    (ESTOP_PAIRING_CASE_CODE, "远程急停开关对频测试", "remote_estop_pairing")))
    rows.extend(dict(code=code, category="CAL", name=name, source_id=source, item=item, applicable=True)
                for item, code, name, source in ((2, CALIBRATION_CASE_CODES[2], "整机标定", "calibration"),
                    (3, CALIBRATION_CASE_CODES[3], "标定检查", "calibration_check")))
    rows.extend(dict(code=FUNCTION_CASE_CODES[key], category="FUNC", name=name, source_id=key,
                     item=4, applicable=True) for key, name in (("remote_estop", "远程急停"), ("battery_hot_swap", "热换电")))
    rows.extend(dict(code=MOTION_CASE_CODES[key], category="MOTION", name=name, source_id=source,
                     item=5 if key == "stunt" else 4, applicable=not (key == "stunt" and dof == 28))
                for key, name, source in (("standing", "站立", "standing"),
                    ("walking", "行走／跑步", "walking / running"), ("stunt", "UFO特技", "stunt")))
    return rows
