"""Bounded whole-body motor trajectories; importing this module performs no I/O.

Positions are physical motor output angles in the deploy SDK's coordinate system.
They are not URDF joint angles. This module does not enable or command hardware.
"""

import ast
import bisect
import hashlib
import io
import json
import math
import re
import struct
import zipfile
from pathlib import Path

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 6000
MAX_DURATION_S = 1800.0
POSITION_BASIS = "motor_output_sdk_rad"


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是有限数值，不能使用布尔值")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数值")
    return result


def _check_dof(dof):
    if type(dof) is not int or dof not in (24, 28):
        raise ValueError("整机轨迹仅支持 24 或 28 个物理电机")


def _json_bytes(value):
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("轨迹不能序列化为有限数值 JSON") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("轨迹 JSON 超过 8 MiB")
    return raw


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"JSON 不允许非有限数值：{value}")


def validate_trajectory(trajectory, dof):
    """Validate and copy data, recomputing derived fields and a content digest.

    File paths and original-file hashes are display/audit metadata only. Execution
    must use the normalized positions and timestamps, never trust these labels.
    """
    _check_dof(dof)
    if not isinstance(trajectory, dict):
        raise ValueError("轨迹必须是 JSON 对象")
    _json_bytes(trajectory)
    if type(trajectory.get("schema_version")) is not int or trajectory["schema_version"] != 1:
        raise ValueError("轨迹 schema_version 必须为 1")
    if type(trajectory.get("dof")) is not int or trajectory["dof"] != dof:
        raise ValueError("轨迹电机数量与所选 24/28 电机配置不一致")
    if trajectory.get("position_basis") != POSITION_BASIS:
        raise ValueError("轨迹必须明确使用 motor_output_sdk_rad 物理电机坐标")
    limb = trajectory.get("limb")
    if limb is not None and limb not in ("whole_body", "rp1", f"rp1_{dof}dof"):
        raise ValueError("仅接受与所选配置一致的整机轨迹，不能导入单肢轨迹")
    times = trajectory.get("timestamps_s")
    positions = trajectory.get("positions_rad")
    if not isinstance(times, (list, tuple)) or not 2 <= len(times) <= MAX_FRAMES:
        raise ValueError("轨迹必须包含 2 至 6000 帧")
    if not isinstance(positions, (list, tuple)) or len(positions) != len(times):
        raise ValueError("位置帧数必须与时间戳数量一致")
    normalized_times = [_number(t, "时间戳") for t in times]
    if normalized_times[0] != 0.0:
        raise ValueError("轨迹时间戳必须从 0 开始")
    if any(b <= a for a, b in zip(normalized_times, normalized_times[1:])):
        raise ValueError("轨迹时间戳必须严格递增")
    if normalized_times[-1] > MAX_DURATION_S:
        raise ValueError("原始轨迹时长不能超过 30 分钟")
    normalized_positions = []
    for row in positions:
        if not isinstance(row, (list, tuple)) or len(row) != dof:
            raise ValueError(f"每帧必须恰好包含 {dof} 个物理电机角度")
        normalized_positions.append([_number(p, "电机角度") for p in row])
    result = {
        "schema_version": 1,
        "dof": dof,
        "position_basis": POSITION_BASIS,
        "timestamps_s": normalized_times,
        "positions_rad": normalized_positions,
    }
    if "motors" in trajectory:
        motors = trajectory["motors"]
        if (
            not isinstance(motors, (list, tuple))
            or len(motors) != dof
            or any(not isinstance(m, str) or not re.fullmatch(r"can[0-9]+/[1-9][0-9]*", m) for m in motors)
            or len(set(motors)) != dof
        ):
            raise ValueError("motors 必须依次列出不重复的 CAN 电机身份，例如 can1/2")
        result["motors"] = list(motors)
    if limb is not None:
        result["limb"] = limb
    content_hash = hashlib.sha256(_json_bytes(result)).hexdigest()
    result.update(
        frame_count=len(normalized_times),
        duration_s=normalized_times[-1],
        trajectory_sha256=content_hash,
        path=trajectory.get("path") if isinstance(trajectory.get("path"), str) else "",
        file_sha256=content_hash,
    )
    for name in ("file_sha256", "source_file_sha256"):
        value = trajectory.get(name)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            result[name] = value
    _json_bytes(result)
    return result


def load_trajectory(path, dof):
    """Load explicit JSON or HMI ``motor_pos`` NPZ without pickle or large arrays.

    HMI NPZ motor_pos is explicitly interpreted as physical-motor SDK radians.
    NumPy is only needed while loading NPZ on the workstation, not on the robot.
    """
    _check_dof(dof)
    source = Path(path)
    with source.open("rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("轨迹文件超过 8 MiB")
    suffix = source.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("轨迹 JSON 格式无效") from exc
    elif suffix == ".npz":
        # Check ZIP sizes before NumPy sees an array. Also inspect NPY headers so a
        # tiny malformed member cannot request an enormous allocation via shape.
        try:
            import numpy as np
        except ImportError as exc:
            raise ValueError("导入 HMI NPZ 需要上位机安装 numpy") from exc
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                names = [m.filename for m in members]
                if len(names) != len(set(names)) or len(names) > 64:
                    raise ValueError("NPZ 包含重复或过多的数组")
                if sum(m.file_size for m in members) > MAX_FILE_BYTES:
                    raise ValueError("NPZ 解压总量超过 8 MiB")
                for member in members:
                    if not member.filename.endswith(".npy") or "/" in member.filename or "\\" in member.filename:
                        raise ValueError("NPZ 仅接受顶层 NPY 数组")
                    if member.file_size > MAX_FILE_BYTES:
                        raise ValueError("NPZ 单数组超过 8 MiB")
                    with archive.open(member) as handle:
                        payload = handle.read(MAX_FILE_BYTES + 1)
                    if len(payload) != member.file_size or len(payload) > MAX_FILE_BYTES:
                        raise ValueError("NPZ 数组大小不一致")
                    if len(payload) < 10 or payload[:6] != b"\x93NUMPY":
                        raise ValueError("NPZ 包含无效 NPY 数组")
                    version = payload[6:8]
                    if version == b"\x01\x00":
                        start, header_size = 10, struct.unpack("<H", payload[8:10])[0]
                    elif version in (b"\x02\x00", b"\x03\x00") and len(payload) >= 12:
                        start, header_size = 12, struct.unpack("<I", payload[8:12])[0]
                    else:
                        raise ValueError("NPY 版本不受支持")
                    if header_size > 10000 or start + header_size > len(payload):
                        raise ValueError("NPY 数组头过大或不完整")
                    header = ast.literal_eval(payload[start : start + header_size].decode("utf-8"))
                    if not isinstance(header, dict) or set(header) != {"descr", "fortran_order", "shape"}:
                        raise ValueError("NPY 数组头无效")
                    shape = header["shape"]
                    if not isinstance(shape, tuple) or len(shape) > 8 or any(type(n) is not int or n < 0 for n in shape):
                        raise ValueError("NPY 数组形状无效")
                    dtype = np.dtype(header["descr"])
                    if dtype.hasobject or dtype.fields is not None:
                        raise ValueError("NPZ 不允许 pickle/object 或结构化数组")
                    expected = math.prod(shape) * dtype.itemsize
                    if expected > MAX_FILE_BYTES or start + header_size + expected != len(payload):
                        raise ValueError("NPY 数组形状与实际大小不一致")
            with np.load(io.BytesIO(raw), allow_pickle=False, max_header_size=10000) as arrays:
                if "motor_pos" not in arrays:
                    raise ValueError("HMI NPZ 缺少 motor_pos")
                positions = arrays["motor_pos"]
                if positions.ndim != 2 or positions.shape[1] != dof or not 2 <= positions.shape[0] <= MAX_FRAMES:
                    raise ValueError(f"HMI motor_pos 必须为 2 至 6000 帧、{dof} 列的整机数组")
                data = {
                    "schema_version": 1,
                    "dof": dof,
                    "position_basis": POSITION_BASIS,
                    "positions_rad": positions.tolist(),
                }
                if "dof" in arrays:
                    declared_dof = arrays["dof"].item()
                    if type(declared_dof) is not int or declared_dof != dof:
                        raise ValueError("NPZ 声明的电机数量与所选配置不一致")
                if "time" in arrays:
                    times = arrays["time"].tolist()
                    if not isinstance(times, list) or len(times) != positions.shape[0]:
                        raise ValueError("HMI time 必须逐帧对应 motor_pos")
                    times = [_number(t, "HMI time") for t in times]
                    data["timestamps_s"] = [t - times[0] for t in times]
                else:
                    if "fps" not in arrays:
                        raise ValueError("HMI NPZ 需要 time 或 fps")
                    fps = _number(arrays["fps"].item(), "HMI fps")
                    if fps <= 0:
                        raise ValueError("HMI fps 必须大于 0")
                    data["timestamps_s"] = [i / fps for i in range(positions.shape[0])]
                for key in ("position_basis", "limb"):
                    if key in arrays:
                        value = arrays[key].item()
                        if isinstance(value, bytes):
                            value = value.decode("utf-8")
                        data[key] = value
                if "motors" in arrays:
                    data["motors"] = arrays["motors"].tolist()
        except (OSError, zipfile.BadZipFile, KeyError, TypeError, SyntaxError, UnicodeDecodeError, OverflowError, RecursionError, struct.error, EOFError, RuntimeError, NotImplementedError) as exc:
            raise ValueError("HMI NPZ 数据无效或超出读取限制") from exc
    else:
        raise ValueError("轨迹仅支持 .json 或 HMI .npz 文件")
    result = validate_trajectory(data, dof)
    result.update(
        path=str(source.resolve()),
        file_sha256=hashlib.sha256(raw).hexdigest(),
        source_file_sha256=hashlib.sha256(raw).hexdigest(),
    )
    return result


def prepare_run(trajectory, dof, positions_rad, options):
    """Validate a single, non-looping run and slow every segment to the speed cap."""
    normalized = validate_trajectory(trajectory, dof)
    if not isinstance(options, dict):
        raise ValueError("运行参数必须是对象")
    limits = {}
    for name in ("speed_deg_s", "position_min_deg", "position_max_deg", "kp", "kd", "max_torque_nm"):
        if name not in options:
            raise ValueError("缺少运行参数：" + name)
        limits[name] = _number(options[name], name)
    if not 0 < limits["speed_deg_s"] <= 10:
        raise ValueError("速度必须大于 0 且不超过 10 °/s")
    if not -180 <= limits["position_min_deg"] < limits["position_max_deg"] <= 180:
        raise ValueError("角度范围必须在 -180 至 180° 内，且下界小于上界")
    if not 0 < limits["kp"] <= 50 or not 0 <= limits["kd"] <= 5:
        raise ValueError("kp 必须在 (0, 50]，kd 必须在 [0, 5]")
    if not 0 < limits["max_torque_nm"] <= 100:
        raise ValueError("最大力矩必须在 (0, 100] N·m")
    if not isinstance(positions_rad, (list, tuple)) or len(positions_rad) != dof:
        raise ValueError("需要全部物理电机的新鲜当前位置")
    current = [_number(p, "当前位置") for p in positions_rad]
    lower, upper = limits["position_min_deg"], limits["position_max_deg"]
    for row in [current, *normalized["positions_rad"]]:
        if any(not lower - 1e-9 <= math.degrees(p) <= upper + 1e-9 for p in row):
            raise ValueError("当前位置或轨迹角度超出本次设置的角度范围")
    first_delta = max(abs(math.degrees(a - b)) for a, b in zip(current, normalized["positions_rad"][0]))
    if first_delta > 3.0 + 1e-9:
        raise ValueError("当前位置与轨迹首帧相差超过 3°，不能直接跳到首帧")
    times = normalized["timestamps_s"]
    maximum_speed = 0.0
    for i in range(1, len(times)):
        delta_time = times[i] - times[i - 1]
        for a, b in zip(normalized["positions_rad"][i - 1], normalized["positions_rad"][i]):
            velocity = abs(math.degrees(b - a)) / delta_time
            maximum_speed = max(maximum_speed, velocity)
    scale = max(1.0, maximum_speed / limits["speed_deg_s"])
    duration = normalized["duration_s"] * scale
    if not math.isfinite(duration) or duration > MAX_DURATION_S:
        raise ValueError("按限速缩放后的轨迹超过 30 分钟")
    return {
        "trajectory": normalized,
        "options": limits,
        "scaled_timestamps_s": [t * scale for t in times],
        "duration_s": duration,
        "first_frame_delta_deg": first_delta,
        "speed_scale": scale,
    }


def sample_trajectory(plan, time_s):
    """Interpolate a prepared plan, clamping before its first/after its last frame."""
    sample_time = _number(time_s, "采样时间")
    times = plan["scaled_timestamps_s"]
    positions = plan["trajectory"]["positions_rad"]
    if sample_time <= times[0]:
        return list(positions[0])
    if sample_time >= times[-1]:
        return list(positions[-1])
    right = bisect.bisect_right(times, sample_time)
    left = right - 1
    weight = (sample_time - times[left]) / (times[right] - times[left])
    return [a + (b - a) * weight for a, b in zip(positions[left], positions[right])]
