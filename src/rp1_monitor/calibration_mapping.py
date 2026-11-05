"""Version-bound candidate mapping and RP1 ankle mathematics, without hardware I/O.

Inputs/outputs use the video plan's named joint order. SDK positions already
include their configured software offsets: this module never applies them twice.
This module does not authorize motion, confirm wiring, or identify physical stops.
"""

import hashlib
import json
import math
from pathlib import Path

# Frozen from the individually hashed local deploy files. No YAML parser or SDK.
_PROFILES = json.loads(r'''{"24":{"robot_config_path":"src/inference/robots/rp1_24dof/robot.yaml","robot_config_sha256":"9f1ff9c6b7399b6b82e280304a008b50222cbbdfdba1bf806c07742b13a8428a","zero_config_path":"scripts/config/set_zero_rp1_24dof.yaml","zero_config_sha256":"81f9a92fa9cf0e91273fbb5817acea2566a298fe8ee06eb5ef6c0e1fd8c5a9a5","motors":{"motor_id":[2,3,4,5,6,7,1,2,3,4,5,6,7,1,1,2,3,4,5,1,2,3,4,5],"motor_interface_type":["canfd","canfd","canfd","canfd"],"motor_interface":["can1","can2","can0","can3"],"motor_num":[7,7,5,5],"motor_type":["LRO","LRO","LRO","LRO"],"motor_model":[3,3,2,3,1,1,3,3,3,2,3,1,1,3,2,1,1,1,1,2,1,1,1,1],"motor_zero_offset":[-2.8623,2.6157,-2.7541,0.0723,0.488,-0.763,2.9102,2.8623,-2.6157,2.7541,-0.0723,-0.7696,0.4932,0.0,2.8894,2.9931,2.8798,-1.5966,2.7881,-2.8894,-2.9931,-2.8798,1.5966,-2.7881],"master_id_offset":16},"robot":{"motor_sign":[1,1,1,1,1,1,-1,1,1,1,1,1,1,-1,1,1,1,1,1,1,1,1,1,1],"urdf2motor":[0,1,2,3,4,5,7,8,9,10,11,12,13,6,14,15,16,17,18,19,20,21,22,23],"close_chain_motor_idx":[4,5,11,12],"type":"rp1"}},"28":{"robot_config_path":"src/inference/robots/rp1/robot.yaml","robot_config_sha256":"0cdc96af415557a62ea93508af31d31b7fb3a359b053493f0b3c2efb446e9588","zero_config_path":"scripts/config/set_zero_rp1.yaml","zero_config_sha256":"a91ef44a2d83083765802f75cfa5af83708ada9f9c5dccc356e6c7122dff877f","motors":{"motor_id":[2,3,4,5,6,7,1,2,3,4,5,6,7,1,1,2,3,4,5,6,7,1,2,3,4,5,6,7],"motor_interface_type":["canfd","canfd","canfd","canfd"],"motor_interface":["can1","can2","can0","can3"],"motor_num":[7,7,7,7],"motor_type":["LRO","LRO","LRO","LRO"],"motor_model":[3,3,2,3,1,1,3,3,3,2,3,1,1,3,2,1,1,1,1,0,0,2,1,1,1,1,0,0],"motor_zero_offset":[-2.8623,2.6157,-2.7541,0.0723,0.488,-0.763,2.9102,2.8623,-2.6157,2.7541,-0.0723,-0.7696,0.4932,0.0,2.8894,2.9931,2.8798,-1.5966,2.7881,0.0,0.0,-2.8894,-2.9931,-2.8798,1.5966,-2.7881,0.0,0.0],"master_id_offset":16},"robot":{"motor_sign":[1,1,1,1,1,1,-1,1,1,1,1,1,1,-1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"urdf2motor":[0,1,2,3,4,5,7,8,9,10,11,12,13,6,14,15,16,17,18,19,20,21,22,23,24,25,26,27],"close_chain_motor_idx":[4,5,11,12],"type":"rp1"}}}''')

_SOURCE_SHA256 = {
    "src/inference/src/robot_interface.cpp":
        "e31b54bc707d7960cd4ee6818acb9f8f51349bc6552c0e6a595b62080bde7dc5",
    "src/inference/src/utils/decouple_rp1.cpp":
        "50e394dfd0f335df63f17be5c8eb08a675e967ae5c908aa9b7214587eb63d7a0",
    "src/inference/src/utils/decouple_rp1.hpp":
        "19c8fb5f00d89ea1abf1ffcf8bcb3f4269c328ed61ee3216c3d4cb3ab66122a6",
    "src/inference/src/utils/close_chain_mapping.cpp":
        "d21b68e790bba2fef5a1f33a12fa1a4e1a172c6a54f179ed9de36b8d0ff09eec",
    "src/inference/src/utils/close_chain_mapping.hpp":
        "307b15fead0ad19176b21d91a4ea0495d5f632ddcd478f75373661ee16bcfdab",
    "src/motors/src/drivers/lro/lro_motor_driver.cpp":
        "0d593b397b1071dde3642aaab6c79a3e8883206a47e8cc1a02a195ef7a1f3885",
    "src/motors/src/drivers/lro/lro_motor_driver.hpp":
        "ba9c9192ba95737f48c546c7c799cdaae6f2851f6d438b43f20d320a61513d08",
}
# The pinned 103d0f policy-gain configuration has been identified offline,
# but the changed RobotInterface lifecycle and gain path lack hardware/closed-
# chain validation. Recognising it here is deliberately not an authorization.
_UNVERIFIED_POLICY_CONFIG_SHA256 = {
    24: "a61ba9f01dbfbc088f451dd05e0a8ab3330ebf730351c76b9d0295af0acf523b",
    28: "3f5afbd5dd4f242616dffbd2b5c55098a7cadb1cc288f24e084da3e014f5dee3",
}
_LEG = ("hip_pitch", "hip_roll", "hip_yaw", "knee", "ankle_pitch", "ankle_roll")
_ARM = ("shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow", "wrist_roll",
        "wrist_yaw", "wrist_pitch")
_TWO_PI = 2 * math.pi
_SDK_POSITION_LIMIT = 12.5
_FK_RESIDUAL = 1e-9


def _profile(dof):
    if type(dof) is not int or dof not in (24, 28):
        raise ValueError("标定映射仅支持24或28电机")
    return _PROFILES[str(dof)]


def _number(value, description):
    if type(value) not in (int, float):
        raise ValueError(description + "必须为有限数值")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(description + "必须为有限数值") from error
    if not math.isfinite(result):
        raise ValueError(description + "必须为有限数值")
    return result


def _vector(values, size, description):
    if not isinstance(values, (list, tuple)) or len(values) != size:
        raise ValueError(description + "数量与机型不符")
    return [_number(value, description) for value in values]


def _names(dof, video=False):
    legs = ("right", "left") if video else ("left", "right")
    return ([side + "_" + axis + "_joint" for side in legs for axis in _LEG]
            + ["waist_roll_joint", "waist_yaw_joint"]
            + [side + "_" + axis + "_joint" for side in ("left", "right")
               for axis in _ARM[:5 if dof == 24 else 7]])


def _physical(profile):
    config, entries, index = profile["motors"], [], 0
    for bus, count in zip(config["motor_interface"], config["motor_num"]):
        for _ in range(count):
            entries.append(dict(index=index, interface=bus,
                                motor_id=config["motor_id"][index],
                                model=config["motor_model"][index],
                                software_offset_rad=config["motor_zero_offset"][index]))
            index += 1
    return entries


def _match_motors(motors, expected):
    if not isinstance(motors, list) or len(motors) != len(expected):
        raise ValueError("电机列表与版本绑定配置不符")
    for actual, fixed in zip(motors, expected):
        if not isinstance(actual, dict):
            raise ValueError("电机条目必须为配置对象")
        for key in ("index", "motor_id", "model"):
            if type(actual.get(key)) is not int or actual[key] != fixed[key]:
                raise ValueError("电机索引、编号或型号与版本绑定配置不符")
        if actual.get("interface") != fixed["interface"]:
            raise ValueError("电机CAN总线与版本绑定配置不符")
        offset = _number(actual.get("software_offset_rad"), "电机软件偏移")
        if offset != fixed["software_offset_rad"]:
            raise ValueError("电机软件偏移与版本绑定配置不符")


def version_bound_mapping(dof, motors=None, *, root=None):
    """Return a fresh candidate named binding; optionally verify deploy files.

    ``motors`` follows postzero_reader.load_configuration()'s physical entries.
    ``None`` is for computer-side presentation only. A real runtime must provide
    its observed entries and ``root``. Even matching hashes do not confirm wiring;
    callers must separately require mapping_confirmed and reference_confirmed.
    """
    profile = _profile(dof)
    physical = _physical(profile)
    if motors is not None:
        _match_motors(motors, physical)
    sources = dict(_SOURCE_SHA256)
    sources[profile["robot_config_path"]] = profile["robot_config_sha256"]
    sources[profile["zero_config_path"]] = profile["zero_config_sha256"]
    if root is not None:
        if motors is None:
            raise ValueError("机器人版本核对必须提供当前电机列表")
        try:
            base = Path(root).resolve(strict=True)
            if not base.is_dir():
                raise ValueError("deploy根路径不是目录")
            robot_source = (base / profile["robot_config_path"]).resolve(strict=True)
            if not robot_source.is_relative_to(base) or not robot_source.is_file():
                raise ValueError("标定映射机器人配置路径不合法")
            if hashlib.sha256(robot_source.read_bytes()).hexdigest() == _UNVERIFIED_POLICY_CONFIG_SHA256[dof]:
                raise ValueError("检测到新版策略增益 deploy；机器人控制接口与复位流程已变化，"
                                 "关节/电机映射和踝部闭链尚未完成该版本实机验证，"
                                 "已阻止标定及限位真机动作")
            for relative, expected in sources.items():
                source = (base / relative).resolve(strict=True)
                if (not source.is_relative_to(base) or not source.is_file()
                        or hashlib.sha256(source.read_bytes()).hexdigest() != expected):
                    raise ValueError("标定映射源码/配置SHA256不符：" + relative)
        except (OSError, RuntimeError, TypeError) as error:
            raise ValueError("标定映射源码/配置缺失或无法只读核对") from error
    # Copy nested constants so callers cannot mutate the frozen profile.
    robot = json.loads(json.dumps(profile["robot"]))
    joint_names, deploy_names = _names(dof, True), _names(dof)
    rows, pairs = [], []
    for side, offset in (("left", 0), ("right", 2)):
        pairs.append(dict(side=side,
                          pitch_index=joint_names.index(side + "_ankle_pitch_joint"),
                          roll_index=joint_names.index(side + "_ankle_roll_joint"),
                          motor_indices=robot["close_chain_motor_idx"][offset:offset + 2]))
    for joint_index, name in enumerate(joint_names):
        deploy_index = deploy_names.index(name)
        primary = robot["urdf2motor"][deploy_index]
        pair = next((pair for pair in pairs
                     if joint_index in (pair["pitch_index"], pair["roll_index"])), None)
        indices = list(pair["motor_indices"]) if pair else [primary]
        rows.append(dict(name=name, joint_index=joint_index, deploy_index=deploy_index,
                         motor_index=primary, motor_indices=indices,
                         busids=[physical[i]["interface"] + "/" + str(physical[i]["motor_id"])
                                 for i in indices],
                         sign=robot["motor_sign"][primary],
                         signs=[robot["motor_sign"][i] for i in indices],
                         software_offsets_rad=[physical[i]["software_offset_rad"] for i in indices],
                         coupled_side=pair["side"] if pair else None))
    return dict(schema_version=1, dof=dof, source_kind="version_bound_candidate",
                candidate=True, hardware_verified=False,
                mapping_confirmed=False, reference_confirmed=False,
                source_verified=root is not None,
                joint_names=joint_names, deploy_joint_names=deploy_names,
                motors=physical, rows=rows, closed_chain_pairs=pairs,
                joint_motor_indices=[list(row["motor_indices"]) for row in rows],
                motor_sign=robot["motor_sign"], urdf2motor=robot["urdf2motor"],
                close_chain_motor_idx=robot["close_chain_motor_idx"],
                master_id_offset=profile["motors"]["master_id_offset"],
                source_sha256=sources,
                binding_note="版本绑定候选；具名关节与实物接线、参考姿态需现场确认")


def _validated(mapping):
    if not isinstance(mapping, dict):
        raise ValueError("需要版本绑定标定映射")
    expected = version_bound_mapping(mapping.get("dof"))
    for key in ("schema_version", "source_kind", "candidate", "hardware_verified", "dof",
                "joint_names", "deploy_joint_names", "motors", "rows", "closed_chain_pairs",
                "joint_motor_indices", "motor_sign", "urdf2motor", "close_chain_motor_idx",
                "master_id_offset", "source_sha256"):
        if not _exact(mapping.get(key), expected[key]):
            raise ValueError("版本绑定标定映射已更改或不完整：" + key)
    _match_motors(mapping["motors"], expected["motors"])
    return mapping["dof"]


def _exact(value, expected):
    """Do not let JSON booleans impersonate integer indices inside a binding."""
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (value.keys() == expected.keys()
                and all(_exact(value[key], item) for key, item in expected.items()))
    if isinstance(expected, list):
        return (len(value) == len(expected)
                and all(_exact(item, fixed) for item, fixed in zip(value, expected)))
    return value == expected


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _ry(vector, sine, cosine):
    x, y, z = vector
    return cosine * x + sine * z, y, -sine * x + cosine * z


def _rx(vector, sine, cosine):
    x, y, z = vector
    return x, cosine * y - sine * z, sine * y + cosine * z


def _inverse2(matrix):
    a, b = matrix[0]
    c, d = matrix[1]
    determinant = a * d - b * c
    norm = math.sqrt(a * a + b * b + c * c + d * d)
    if (not math.isfinite(determinant) or not norm
            or abs(determinant) < 1e-10 * norm * norm):
        raise ValueError("踝部闭链Jacobian奇异")
    inverse = ((d / determinant, -b / determinant),
               (-c / determinant, a / determinant))
    condition = norm * math.sqrt(sum(x * x for row in inverse for x in row))
    if not math.isfinite(condition) or condition > 1e5:
        raise ValueError("踝部闭链Jacobian接近奇异")
    return inverse


def _mul2(matrix, vector):
    return tuple(_dot(row, vector) for row in matrix)


def _ankle(pitch, roll, side):
    """Return (theta_long/short, dtheta/djoint) from reviewed deploy geometry."""
    pitch, roll = _number(pitch, "踝俯仰角"), _number(roll, "踝侧摆角")
    if side not in ("left", "right"):
        raise ValueError("踝部side仅支持left/right")
    if max(abs(pitch), abs(roll)) >= math.pi / 2:
        raise ValueError("踝部角度越出近中性闭链解支路")
    sp, cp, sr, cr = math.sin(pitch), math.cos(pitch), math.sin(roll), math.cos(roll)
    theta, jacobian = [], []
    left = side == "left"
    for long in (True, False):
        rod, cz = (210.636, 209.663) if long else (139.941, 138.663)
        by = -15.0 if (long == left) else 15.0
        theta0 = -math.pi if (long == left) else 0.0
        rotated_d = _rx((-27.5, by, 18.4), sr, cr)
        b_rot = _ry((rotated_d[0], rotated_d[1], rotated_d[2] - 19.0), sp, cp)
        diff = (-37.390 - b_rot[0], -b_rot[1], cz - b_rot[2])
        a, b = 52.0 * diff[1], -52.0 * diff[2]
        radius = math.hypot(a, b)
        if not math.isfinite(radius) or radius <= 1e-9:
            raise ValueError("踝部闭链约束奇异")
        cosine = -(_dot(diff, diff) + 26.0 ** 2 - rod ** 2) / radius
        if not math.isfinite(cosine) or abs(cosine) > 1.0:
            raise ValueError("踝部闭链几何无解，不能截断后继续")
        phase, delta = math.atan2(b, a), math.acos(cosine)
        first, second = phase + delta - theta0, phase - delta - theta0
        angle = first if abs(first) < abs(second) else second
        actual = angle + theta0
        bar = (0.0, 26.0 * math.cos(actual), -26.0 * math.sin(actual))
        rrod = (b_rot[0] + 37.390, b_rot[1] - bar[1], b_rot[2] - cz - bar[2])
        # dRy/dpitch * (p_a + Rx * d).
        db_pitch = (-sp * rotated_d[0] + cp * (rotated_d[2] - 19.0),
                    0.0, -cp * rotated_d[0] - sp * (rotated_d[2] - 19.0))
        db_roll = _ry((0.0, -sr * by - cr * 18.4, cr * by - sr * 18.4), sp, cp)
        # S_bar=(-1,0,0), hence S_bar cross r_bar=(0,bar_z,-bar_y).
        divisor = _dot(rrod, (0.0, bar[2], -bar[1]))
        if not math.isfinite(divisor) or abs(divisor) < 1e-7:
            raise ValueError("踝部闭链传动奇异")
        theta.append(angle)
        jacobian.append((_dot(rrod, db_pitch) / divisor, _dot(rrod, db_roll) / divisor))
    _inverse2(jacobian)
    return theta, jacobian


def _nearest(angle, reference):
    return angle + _TWO_PI * round((reference - angle) / _TWO_PI)


def _fk(target, side, previous=None):
    """Solve the near-neutral branch and verify the inverse projection."""
    target = _vector(target, 2, "踝电机角度")
    seeds = [list(previous)] if previous is not None else []
    seeds += [[0.0, 0.0], [-0.2, 0.0], [0.2, 0.0], [-0.4, 0.0],
              [-0.2, 0.2], [-0.2, -0.2], [0.0, 0.3], [0.0, -0.3]]
    expected, _ = _ankle(*(previous or (0.0, 0.0)), side)
    target = [_nearest(value, expected[i]) for i, value in enumerate(target)]
    for seed in seeds:
        q = list(seed)
        for _ in range(100):
            try:
                current, jacobian = _ankle(*q, side)
            except ValueError:
                break
            error = [target[i] - current[i] for i in range(2)]
            residual = math.hypot(*error)
            if residual < _FK_RESIDUAL:
                if previous is not None and max(abs(q[i] - previous[i]) for i in range(2)) > 0.5:
                    raise ValueError("踝部FK反馈跳变或解支路不连续")
                return q, jacobian
            delta = _mul2(_inverse2(jacobian), error)
            scale = min(1.0, 0.08 / max(abs(value) for value in delta))
            improved = False
            for _ in range(12):
                candidate = [q[i] + scale * delta[i] for i in range(2)]
                try:
                    predicted, _ = _ankle(*candidate, side)
                    improved = math.hypot(*(target[i] - predicted[i] for i in range(2))) < residual
                except ValueError:
                    improved = False
                if improved:
                    q = candidate
                    break
                scale *= 0.5
            if not improved:
                break
    raise ValueError("踝部FK未收敛或反馈与闭链几何不一致")


def _sdk_positions(values, mapping, description):
    values = _vector(values, mapping["dof"], description)
    if any(abs(value - motor["software_offset_rad"]) > _SDK_POSITION_LIMIT + 1e-10
           for value, motor in zip(values, mapping["motors"])):
        raise ValueError(description + "越出SDK原始编码范围")
    return values


def logical_to_motor(q, mapping, previous=None):
    """Convert video-order logical angles to complete physical SDK targets.

    ``previous`` is the physical SDK vector used only to select continuous 2π
    branches. URDF/probe stroke and velocity limits remain the executor's job.
    """
    dof = _validated(mapping)
    q = _vector(q, dof, "逻辑关节角度")
    if max(abs(value) for value in q) > _TWO_PI:
        raise ValueError("逻辑关节角度越出单圈候选映射范围")
    previous = None if previous is None else _sdk_positions(previous, mapping, "上一电机角度")
    positions = [None] * dof
    for row in mapping["rows"]:
        if row["coupled_side"] is None:
            i = row["motor_index"]
            positions[i] = q[row["joint_index"]] * mapping["motor_sign"][i]
    for pair in mapping["closed_chain_pairs"]:
        theta, _ = _ankle(q[pair["pitch_index"]], q[pair["roll_index"]], pair["side"])
        for angle, i in zip(theta, pair["motor_indices"]):
            positions[i] = angle * mapping["motor_sign"][i]
    if previous is not None:
        positions = [_nearest(angle, old) for angle, old in zip(positions, previous)]
    return _sdk_positions(positions, mapping, "目标电机角度")


def motor_to_logical(positions, torques, mapping, previous=None):
    """Return video-order angles and estimated joint Nm from physical SDK data.

    ``torques`` must already be motor-output Nm estimates, never Iq or ROS effort.
    ``previous`` is a previous video-order logical vector, unlike the forward API.
    Both ankle motor readings are required; freshness is checked by the caller.
    """
    dof = _validated(mapping)
    positions = _sdk_positions(positions, mapping, "反馈电机角度")
    torques = _vector(torques, dof, "电机估算输出力矩")
    previous = None if previous is None else _vector(previous, dof, "上一逻辑关节角度")
    q, tau = [None] * dof, [None] * dof
    for row in mapping["rows"]:
        if row["coupled_side"] is None:
            j, i = row["joint_index"], row["motor_index"]
            q[j] = positions[i] * mapping["motor_sign"][i]
            if previous is not None:
                q[j] = _nearest(q[j], previous[j])
            tau[j] = torques[i] * mapping["motor_sign"][i]
    for pair in mapping["closed_chain_pairs"]:
        pi, ri = pair["pitch_index"], pair["roll_index"]
        indices = pair["motor_indices"]
        angles = [positions[i] * mapping["motor_sign"][i] for i in indices]
        last = None if previous is None else [previous[pi], previous[ri]]
        solved, jacobian = _fk(angles, pair["side"], last)
        q[pi], q[ri] = solved
        motor_tau = [torques[i] * mapping["motor_sign"][i] for i in indices]
        # Virtual work: tau_joint = (dtheta/djoint)^T * tau_motor.
        tau[pi] = sum(jacobian[i][0] * motor_tau[i] for i in range(2))
        tau[ri] = sum(jacobian[i][1] * motor_tau[i] for i in range(2))
    if (any(not math.isfinite(value) for value in q + tau)
            or max(abs(value) for value in q) > _TWO_PI):
        raise ValueError("逻辑角度/力矩越出候选映射范围或非有限")
    return q, tau
