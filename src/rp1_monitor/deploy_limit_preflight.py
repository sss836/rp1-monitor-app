"""Inspect deploy configuration as data without importing or constructing a hardware SDK."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

from .torque_estimation import estimate_output_torque

_BLOCKERS = {
    "motor_iq_A_units": "尚未验证已安装二进制的电流解码及逐电机固件CUR量程，motor_iq_A不能视为已验证。",
    "raw_feedback_freshness": "尚未验证每电机真实接收时间、递增接收序号及首帧有效性。",
    "device_watchdog": "尚未读取并验证驱动器通信超时与断联后的实际停止行为。",
    "control_mutual_exclusion": "尚未验证推理、标零及其他总线写入者已退出且不会自动重启。",
    "ankle_raw_current": "踝部必须关联两台物理电机的原始电流，不能把变换后的effort当作A。",
    "real_motion_parameters": "尚未确认逐轴速度、增益、接触/中止电流和反馈超时参数。",
    "joint_name_binding": "逻辑索引尚未与所选URDF版本的关节名称关联并核对。",
    "physical_setup": "尚未确认现场支撑、急停、摆位和机器人上的实际配置版本。",
}

_LRO_CPP = "src/motors/src/drivers/lro/lro_motor_driver.cpp"
_LRO_HPP = "src/motors/src/drivers/lro/lro_motor_driver.hpp"
_LRO_MODELS = ["LRO_5550", "LRO_6562", "LRO_8462", "LRO_10062"]


def _cpp_code(source):
    # Preserve offsets for evidence while excluding comments and quoted examples.
    return re.sub(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: "".join("\n" if char == "\n" else " " for char in match[0]),
        source,
        flags=re.DOTALL,
    )


def _cpp_block(code, pattern):
    matches = list(re.finditer(pattern, code))
    if len(matches) != 1:
        return None
    match = matches[0]
    depth = 1
    for index in range(match.end(), len(code)):
        depth += (code[index] == "{") - (code[index] == "}")
        if depth == 0:
            return match.start(), match.end(), index
    return None


def analyze_lro_source_compatibility(cpp: str | None, hpp: str | None) -> dict:
    """Recognize local LRO source signatures; never attest a loaded driver or firmware.

    This deliberately accepts only the known enum, limit-table and receive-decoder
    forms. Other C++ forms remain unrecognized rather than being inferred from a
    CurMax identifier or a comment. No source, SDK or configuration is executed.
    """
    if any(source is not None and not isinstance(source, str) for source in (cpp, hpp)):
        raise ValueError("LRO源码必须为字符串或None")
    report = {
        "scope": "local_source_only",
        "status": "missing_source" if cpp is None or hpp is None else "unrecognized",
        "source_decode_limit_field": None,
        "candidate_iq_encoding_ranges_a_by_model": [],
        "motor_iq_A_verified": False,
        "installed_binary_verified": False,
        "firmware_cur_range_verified": False,
        "missing_files": [path for path, source in ((_LRO_CPP, cpp), (_LRO_HPP, hpp)) if source is None],
        "evidence": [],
        "notes": [
            "源码签名只描述所读文件，不能证明机器人已编译、安装或加载这些源码。",
            "实际逐电机固件CUR量程未读取验证，源码量程不能作为已验证motor_iq_A或安全阈值。",
        ],
    }
    if cpp is None or hpp is None:
        return report
    cpp_code, hpp_code = _cpp_code(cpp), _cpp_code(hpp)
    if re.search(r"(?m)^\s*#\s*(?:if|ifdef|ifndef|elif|else|endif)\b", cpp_code + "\n" + hpp_code):
        return report

    def evidence(path, source, start, end, feature):
        report["evidence"].append(
            {"path": path, "line": source.count("\n", 0, start) + 1,
             "feature": feature, "excerpt": source[start:end].strip()}
        )

    callback = _cpp_block(
        cpp_code, r"void\s+LroMotorDriver::canfd_rx_cbk\s*\(\s*const\s+canfd_frame\s*&\s*rx_frame\s*\)\s*\{"
    )
    limits = _cpp_block(hpp_code, r"typedef\s+struct\s*\{(?=[^{}]*}\s*LRO_Limit_Param\s*;)")
    models = _cpp_block(hpp_code, r"enum\s+LRO_Motor_Model\s*\{")
    table = _cpp_block(
        cpp_code, r"LRO_Limit_Param\s+lro_limit_param\s*\[\s*LRO_Num_Of_Motor\s*\]\s*=\s*\{"
    )
    if any(block is None for block in (callback, limits, models, table)):
        return report
    # Preprocessor-dependent definitions require a build context, absent here.
    if any("#" in code[block[0]:block[2]] for code, block in (
        (cpp_code, callback), (hpp_code, limits), (hpp_code, models), (cpp_code, table)
    )):
        return report
    callback_text = cpp_code[callback[1]:callback[2]]
    assignments = list(re.finditer(r"motor_current_\s*=\s*([^;]+);", callback_text))
    if len(assignments) != 1:
        return report
    assignment = assignments[0]
    expression = re.sub(r"\s+", "", assignment[1])
    field = next((field for field in ("TauMax", "CurMax") if expression == (
        f"range_map(t_int,uint16_t(0),bitmax<uint16_t>(12),-limit_param_.{field},limit_param_.{field})"
    )), None)
    if field is None:
        return report
    report["source_decode_limit_field"] = field
    evidence(_LRO_CPP, cpp, callback[1] + assignment.start(), callback[1] + assignment.end(), "type1_current_decode")
    fields = re.sub(r"\s+", "", hpp_code[limits[1]:limits[2]])
    expected_fields = ["PosMax", "SpdMax", "TauMax"] + (["CurMax"] if field == "CurMax" else []) + ["OKpMax", "OKdMax"]
    enum = re.sub(r"\s+", "", hpp_code[models[1]:models[2]]).strip(",")
    if fields != "".join(f"float{name};" for name in expected_fields) or enum.split(",") != _LRO_MODELS + ["LRO_Num_Of_Motor"]:
        return report
    evidence(_LRO_HPP, hpp, limits[0], limits[2] + 1, "limit_field_order")
    evidence(_LRO_HPP, hpp, models[0], models[2] + 1, "motor_model_order")
    table_text = cpp_code[table[1]:table[2]]
    rows = re.findall(r"\{([^{}]*)\}", table_text)
    if len(rows) != 4 or re.sub(r"\{[^{}]*\}|[\s,]", "", table_text):
        return report
    values = []
    for row in rows:
        parts = [part.strip() for part in row.split(",")]
        if len(parts) != len(expected_fields) or any(
            not re.fullmatch(r"[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[fF]?", part)
            for part in parts
        ):
            return report
        numbers = [float(part.rstrip("fF")) for part in parts]
        if any(not math.isfinite(value) or value <= 0 for value in numbers):
            return report
        values.append(numbers)
    evidence(_LRO_CPP, cpp, table[0], table[2] + 1, "model_limit_table")
    report["status"] = "curmax_source_recognized" if field == "CurMax" else "legacy_taumax"
    if field == "CurMax":
        report["candidate_iq_encoding_ranges_a_by_model"] = [
            {"model_id": index, "model_name": name, "range_a": [-row[3], row[3]]}
            for index, (name, row) in enumerate(zip(_LRO_MODELS, values))
        ]
    else:
        report["notes"].append("旧版将motor_current_按TauMax解码，不能将其直接视为已验证的Iq（A）。")
    return report


def _integer(value, name, minimum=0, maximum=65535):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name}必须为{minimum}到{maximum}的整数")
    return value


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name}必须为有限数值")
    return float(value)


def _array(config, name, size):
    value = config.get(name)
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f"{name}必须为包含{size}项的列表")
    return value


def _dof(value):
    if type(value) is not int or value not in (24, 28):
        raise ValueError("dof仅支持24或28")
    return value


def analyze_deploy_config(
    motor_config: dict, robot_config: dict, dof: int, *, policy_config: dict | None = None
) -> dict:
    """Validate already parsed sections; valid structure never authorizes robot motion.

    ``urdf2motor[j]`` selects the physical motor index for logical joint ``j``.
    The files do not provide reliable joint names; callers must bind a specific URDF.
    Legacy robot gains use physical-motor order. New policy gains use logical
    joint order and must be mapped through ``urdf2motor`` before reporting
    physical entries. Neither source is approved for hardware probing.
    """
    _dof(dof)
    if not isinstance(motor_config, dict) or not isinstance(robot_config, dict):
        raise ValueError("motors和robot必须为配置对象")
    if robot_config.get("type") != "rp1":
        raise ValueError("仅识别robot.type为rp1的配置")
    counts = motor_config.get("motor_num")
    if not isinstance(counts, list) or not counts:
        raise ValueError("motor_num必须为非空列表")
    counts = [_integer(n, "motor_num", 1, dof) for n in counts]
    if sum(counts) != dof:
        raise ValueError("motor_num总数与所选自由度不符")
    bus_count = len(counts)
    interfaces = _array(motor_config, "motor_interface", bus_count)
    if any(not isinstance(bus, str) or not bus.strip() for bus in interfaces):
        raise ValueError("motor_interface必须为非空接口名称")
    if len(set(interfaces)) != bus_count:
        raise ValueError("motor_interface不能重复")
    interface_types = _array(motor_config, "motor_interface_type", bus_count)
    motor_types = _array(motor_config, "motor_type", bus_count)
    if any(item != "canfd" for item in interface_types):
        raise ValueError("当前仅识别直接canfd接口")
    if any(item != "LRO" for item in motor_types):
        raise ValueError("当前仅识别LRO电机")
    ids = [_integer(item, "motor_id", 1, 8) for item in _array(motor_config, "motor_id", dof)]
    models = [
        _integer(item, "motor_model", 0, 3) for item in _array(motor_config, "motor_model", dof)
    ]
    offsets = [
        _number(item, "motor_zero_offset")
        for item in _array(motor_config, "motor_zero_offset", dof)
    ]
    master_offset = _integer(motor_config.get("master_id_offset", 0), "master_id_offset")
    mapping = [
        _integer(item, "urdf2motor", 0, dof - 1) for item in _array(robot_config, "urdf2motor", dof)
    ]
    if sorted(mapping) != list(range(dof)):
        raise ValueError("urdf2motor必须为全部电机索引的无重复排列")
    signs = _array(robot_config, "motor_sign", dof)
    if any(type(sign) not in (int, float) or sign not in (-1, 1) for sign in signs):
        raise ValueError("motor_sign每项必须为+1或-1")
    has_kp, has_kd = "kp" in robot_config, "kd" in robot_config
    if has_kp != has_kd:
        raise ValueError("robot.yaml的kp和kd必须同时提供")
    if has_kp:
        kp = [_number(item, "kp") for item in _array(robot_config, "kp", dof)]
        kd = [_number(item, "kd") for item in _array(robot_config, "kd", dof)]
        if any(gain < 0 for gain in kp + kd):
            raise ValueError("kp和kd不能为负")
        gain_source = "robot_yaml_physical_motor_order"
    else:
        if not isinstance(policy_config, dict):
            raise ValueError("robot.yaml无kp/kd；必须明确选择包含增益的策略配置")
        logical_kp = [_number(item, "kp") for item in _array(policy_config, "kp", dof)]
        logical_kd = [_number(item, "kd") for item in _array(policy_config, "kd", dof)]
        if any(gain <= 0 for gain in logical_kp) or any(gain < 0 for gain in logical_kd):
            raise ValueError("策略kp必须大于0，kd不能为负")
        kp, kd = [0.0] * dof, [0.0] * dof
        for logical_index, motor_index in enumerate(mapping):
            kp[motor_index] = logical_kp[logical_index]
            kd[motor_index] = logical_kd[logical_index]
        gain_source = "policy_yaml_logical_joint_order"
    closed = [
        _integer(item, "close_chain_motor_idx", 0, dof - 1)
        for item in _array(robot_config, "close_chain_motor_idx", 4)
    ]
    if len(set(closed)) != 4:
        raise ValueError("close_chain_motor_idx必须为四个不同电机索引")
    if any(kp[index] <= 0 for index in closed):
        raise ValueError("闭链电机kp必须大于0，现有apply_action闭链计算使用kp作分母")

    physical = []
    index = 0
    for bus_index, count in enumerate(counts):
        bus_ids = ids[index : index + count]
        if len(set(bus_ids)) != count:
            raise ValueError("同一总线的motor_id不能重复")
        for _ in range(count):
            # Extract profile metadata only; no measured current or torque is fabricated.
            profile = estimate_output_torque(0.0, models[index], source_kind="offline")
            physical.append(
                {
                    "motor_index": index,
                    "bus_index": bus_index,
                    "interface": interfaces[bus_index],
                    "interface_type": "canfd",
                    "motor_id": ids[index],
                    "motor_type": "LRO",
                    "motor_model": models[index],
                    "motor_sign": int(signs[index]),
                    "software_zero_offset_rad": offsets[index],
                    "master_id_offset": master_offset,
                    "configured_kp": kp[index],
                    "configured_kd": kd[index],
                    "torque_estimation_profile": {
                        name: profile[name]
                        for name in (
                            "model_name",
                            "model_alias",
                            "coefficient_nm_per_a",
                            "basis_frame",
                            "reference",
                            "candidate_iq_encoding_range_a",
                            "accuracy_status",
                            "usable_for_contact_detection",
                        )
                    },
                }
            )
            index += 1
    motor_to_logical = {motor: logical for logical, motor in enumerate(mapping)}
    pairs = []
    for pair_index in range(2):
        members = closed[pair_index * 2 : pair_index * 2 + 2]
        pairs.append(
            {
                "pair_index": pair_index,
                "motor_indices": members,
                "logical_indices": [motor_to_logical[motor] for motor in members],
                "side": None,
            }
        )
    pair_by_motor = {motor: pair["pair_index"] for pair in pairs for motor in pair["motor_indices"]}
    logical_joints = [
        {
            **physical[motor_index],
            "logical_index": logical_index,
            "joint_name": None,
            "closed_chain_pair": pair_by_motor.get(motor_index),
        }
        for logical_index, motor_index in enumerate(mapping)
    ]
    return {
        "schema_version": 1,
        "dof": dof,
        "operation": "static_deploy_preflight",
        "configuration_valid": True,
        "hardware_ready": False,
        "hardware_opened": False,
        "execution_performed": False,
        "calibration_passed": None,
        "name_binding": "unverified_logical_indices_only",
        "source_compatibility": analyze_lro_source_compatibility(None, None),
        "gain_source": gain_source,
        "logical_joints": logical_joints,
        "closed_chain_pairs": pairs,
        "blockers": [{"code": code, "message": message} for code, message in _BLOCKERS.items()],
        "notes": [
            "此结果只验证配置结构，不证明机器人文件、接线、反馈或停止行为正确。",
            "motor_zero_offset是软件位置偏移，不是已验证的硬件零位。",
            "配置增益和驱动协议范围不能作为已认可的接触检查参数。",
        ],
    }


def inspect_deploy(root: str | Path, dof: int, *, policy: str | None = None) -> dict:
    """Read local source files only; optional PyYAML is needed for this file convenience API."""
    _dof(dof)
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError(
            "读取YAML需要可选的PyYAML；也可向analyze_deploy_config传入已解析配置"
        ) from error
    root = Path(root).resolve(strict=True)
    relative = f"src/inference/robots/{'rp1_24dof' if dof == 24 else 'rp1'}/robot.yaml"
    config_path = (root / relative).resolve(strict=True)
    if not config_path.is_relative_to(root):
        raise ValueError("配置路径位于所选deploy根目录之外")
    raw = config_path.read_bytes()
    if len(raw) > 512 * 1024:
        raise ValueError("配置文件超过512KiB")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise ValueError("robot.yaml不是可安全解析的YAML配置") from error
    if not isinstance(data, dict):
        raise ValueError("robot.yaml必须为配置对象")
    robot = data.get("robot")
    policy_path, policy_raw, policy_config = None, None, None
    if isinstance(robot, dict) and "kp" not in robot and "kd" not in robot:
        if not isinstance(policy, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", policy):
            raise ValueError("robot.yaml无kp/kd；请使用--policy明确选择策略")
        name = policy if policy.endswith(".yaml") else policy + ".yaml"
        policy_dir = (config_path.parent / "configs").resolve(strict=True)
        if not policy_dir.is_relative_to(config_path.parent):
            raise ValueError("策略配置目录位于所选机器人配置目录之外")
        policy_path = (policy_dir / name).resolve(strict=True)
        if not policy_path.is_relative_to(policy_dir) or not policy_path.is_file():
            raise ValueError("策略配置文件位于策略目录之外或不存在")
        policy_raw = policy_path.read_bytes()
        if len(policy_raw) > 512 * 1024:
            raise ValueError("策略配置文件超过512KiB")
        try:
            policy_data = yaml.safe_load(policy_raw)
        except yaml.YAMLError as error:
            raise ValueError("策略配置不是可安全解析的YAML") from error
        if not isinstance(policy_data, dict) or not isinstance(policy_data.get("inference_node"), dict):
            raise ValueError("策略配置缺少inference_node对象")
        policy_config = policy_data["inference_node"].get("ros__parameters")
    report = analyze_deploy_config(data.get("motors"), robot, dof, policy_config=policy_config)
    report["deploy_root"] = str(root)
    report["source_files"] = []
    source_text = {}
    for path in [
        relative,
        "src/motors/src/pybind_module.cpp",
        "src/inference/src/pybind_module.cpp",
        "src/inference/src/robot_interface.cpp",
        _LRO_CPP,
        _LRO_HPP,
    ]:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("源码路径位于所选deploy根目录之外")
        content = (raw if path == relative else target.read_bytes()) if target.is_file() else None
        report["source_files"].append(
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
            }
        )
        if path in (_LRO_CPP, _LRO_HPP):
            # A non-UTF-8 file is present but has no recognized source signature.
            try:
                source_text[path] = content.decode("utf-8") if content is not None else None
            except UnicodeDecodeError:
                source_text[path] = ""
    if policy_path is not None:
        report["selected_policy"] = {
            "name": policy,
            "path": str(policy_path.relative_to(root)),
            "sha256": hashlib.sha256(policy_raw).hexdigest(),
            "gain_order": "logical_joint",
        }
        report["source_files"].append({"path": str(policy_path.relative_to(root)),
                                       "sha256": hashlib.sha256(policy_raw).hexdigest()})
    report["source_compatibility"] = analyze_lro_source_compatibility(
        source_text[_LRO_CPP], source_text[_LRO_HPP]
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="本地deploy根目录，仅静态读取")
    parser.add_argument("--dof", type=int, choices=(24, 28), required=True)
    parser.add_argument("--policy", help="新版增益所在的策略名，例如default；旧版机器人配置可省略")
    args = parser.parse_args(argv)
    try:
        result = inspect_deploy(args.root, args.dof, policy=args.policy)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"静态预检失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
