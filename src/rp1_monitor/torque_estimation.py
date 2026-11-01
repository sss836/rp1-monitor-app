"""Provisional LRO output torque from motor Iq, without hardware control.

The supplied parameter script describes output-side torque/Iq fits. Its current
ranges are candidate encoding ranges, not continuous-current or safe-load limits.
No raw calibration samples or uncertainty were supplied, so these estimates must
not enable contact detection or be described as torque-sensor measurements.
"""

import math

# Statically inspected, never imported or executed: deploy_motorlro_params (1).py.
_REFERENCE = {
    "filename": "deploy_motorlro_params (1).py",
    "sha256": "34f2214d52cf5e879e9ec169ec91725b22cdc99228a47d2671a491c9c85b0f3c",
    "section": "MODEL_PARAMS and its preceding fit/convention comments",
}
# The enum order matches motors/src/drivers/lro/lro_motor_driver.hpp.
# name, script alias, candidate output Nm/Iq_A, candidate Iq encoding half-range A.
_PROFILES = (
    ("LRO_5550", "55", 1.74, 18.0),
    ("LRO_6562", "60", 1.62, 32.0),
    ("LRO_8462", "75", 2.31, 35.0),
    ("LRO_10062", "97", 2.38, 90.0),
)


def estimate_output_torque(
    iq_a,
    model_id,
    *,
    current_kind="motor_iq",
    binding_verified=False,
    current_scale_verified=False,
    source_kind="simulated",
):
    """Return a signed, explicitly unverified output-torque estimate.

    Only raw motor Iq in A is accepted. Bus current, phase RMS, ROS effort and
    closed-chain joint effort are different inputs. The returned sign is in the
    motor coordinate system; joint directions and ankle Jacobians are not applied.
    Unknown models, nonfinite input and values outside the candidate encoding
    range raise ValueError instead of producing a plausible clamped result.
    """
    if type(model_id) is not int or not 0 <= model_id < len(_PROFILES):
        raise ValueError("需要已核对的 LRO 型号枚举 0、1、2 或 3")
    if current_kind != "motor_iq":
        raise ValueError("仅接受原始电机 q 轴电流 Iq（A），不接受母线电流或关节 effort")
    if not isinstance(source_kind, str) or source_kind not in {"simulated", "ros2", "offline"}:
        raise ValueError("source_kind 必须为 simulated、ros2 或 offline")
    if type(binding_verified) is not bool or type(current_scale_verified) is not bool:
        raise ValueError("型号绑定和电流量程确认标记必须为布尔值")
    if isinstance(iq_a, bool) or not isinstance(iq_a, (int, float)):
        raise ValueError("Iq 必须是有限数值，单位 A")
    try:
        current = float(iq_a)
    except OverflowError as error:
        raise ValueError("Iq 必须是有限数值，单位 A") from error
    if not math.isfinite(current):
        raise ValueError("Iq 必须是有限数值，单位 A")
    name, alias, coefficient, encoding_max = _PROFILES[model_id]
    if abs(current) > encoding_max:
        raise ValueError("Iq 超出候选编码量程，不能截断后继续估算")
    return {
        "raw_iq_a": current,
        "model_id": model_id,
        "model_name": name,
        "model_alias": alias,
        "coefficient_nm_per_a": coefficient,
        "coefficient_basis": "candidate_output_torque_per_motor_iq",
        "candidate_iq_encoding_range_a": [-encoding_max, encoding_max],
        "provisional_estimated_nm": current * coefficient,
        "estimated_torque_nm": current * coefficient,
        "measured_torque_nm": None,
        "basis_frame": "motor_output",
        "input_basis": "motor_iq_A",
        "source_kind": source_kind,
        "measurement_kind": "estimated_from_motor_iq",
        "accuracy_status": "unverified_no_raw_calibration_data",
        "uncertainty_nm": None,
        "type1_theoretical_step_nm": 2 * encoding_max / 4095 * coefficient,
        "resolution_basis": "candidate_12_bit_encoding_not_measurement_accuracy",
        "usable_for_contact_detection": False,
        "validation_flags": {
            "binding_verified": binding_verified,
            "current_scale_verified": current_scale_verified,
            "coefficient_accuracy_verified": False,
            "physical_torque_sensor": False,
            "hardware_acceptance_completed": False,
        },
        "assumptions": [
            "型号枚举和别名来自所附脚本，实机型号绑定须单独核对。",
            "系数被解释为输出端 Nm/Iq_A，不再次乘减速比。",
            "脚本声称拟合基于 48 V PJ2/PJ3 中负载数据；原始样本与误差未提供。",
            "脚本假设幅值不变 Park 变换、Id=0 时 Iq=sqrt(2)*相电流 RMS；实机约定未核验。",
            "符号沿用原始电机 Iq；未换算 URDF 方向、闭链踝部关节力矩或外部接触力矩。",
            "CurMax 是候选编码量程，不是安全持续电流、接触阈值或中止阈值。",
            "12 位理论步长不是测量精度，且依赖固件与解码量程一致。",
        ],
        "reference": dict(_REFERENCE),
    }
