"""Video-plan calibration checks in named URDF coordinates, with no hardware I/O.

The packaged plans remain simulated evidence. This module cannot verify the real
mechanical reference, motor mapping or a collision-free physical retreat. A first
group ends at ``waiting_review`` after stopping; a caller must review/replan the
changed combined posture before running another group.
"""

import copy
import hashlib
import json
import math
import statistics
from importlib import resources
from pathlib import Path

try:
    from .gentle_motion import SmoothMove
    from .preparation_gains import preparation_gain_profile
except ImportError:  # Standalone, in-memory SSH helper modules.
    from gentle_motion import SmoothMove
    from preparation_gains import preparation_gain_profile

MAX_PLAN_BYTES = 256 * 1024
PREPARATION_TIMEOUT_S = 90.0
PREPARATION_DEFAULT_SPEED_DEG_S = 5.0
PREPARATION_MAX_SPEED_DEG_S = 20.0
PREPARATION_DEFAULT_TOLERANCE_DEG = 5.0
PREPARATION_MAX_TOLERANCE_DEG = 15.0
PREPARATION_CYCLE_MARGIN_S = 0.15
DEPLOY_RESET_PROFILE = "deploy_reset_4s"
DEPLOY_RESET_DURATION_S = 4.0
GROUP_PHASES = ("并行接近所选单侧", "各轴末端同步降速", "记录所选模型端点", "各轴缓退1°")
DEFAULT_OPTIONS = {
    "speed_deg_s": 3.0,
    "search_speed_deg_s": 0.3,
    "kp": 50.0,
    "kd": 0.5,
    "contact_torque_nm": 0.5,
    "torque_rise_nm": 0.25,
    "abort_torque_nm": 10.0,
    "tolerance_deg": 3.0,
    "hold_s": 0.5,
    "position_span_deg": 0.15,
    # This bounded search margin is displayed for confirmation for every run.
    # It is not a certified mechanical allowance or a claim of collision safety.
    "search_overtravel_deg": 1.0,
    "retreat_deg": 1.0,
    "stop_velocity_deg_s": 0.15,
    "stop_timeout_s": 2.0,
    "start_tolerance_deg": 3.0,
    "require_pose_settling": True,
    "search_timeout_s": 30.0,
}
DESKTOP_CHECK_DEFAULTS = DEFAULT_OPTIONS | {
    "require_pose_settling": False,
    "search_speed_deg_s": 1.0,
    "hold_s": 1.0,
    "position_span_deg": 0.2,
    "torque_rise_nm": 3.0,
    "abort_torque_nm": None,
}


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}须为有限数值")
    return float(value)


def _abort_torque(value, *, allow_disabled):
    if value is None:
        if allow_disabled:
            return None
        raise ValueError("旧严格接口须提供中止力矩")
    torque = _number(value, "中止力矩")
    if not 0 < torque <= 100:
        raise ValueError("中止力矩超出软件范围")
    return torque


def _vector(value, size, name):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"{name}须包含全部{size}个关节")
    return [_number(v, name) for v in value]


def _indices(value, size, name):
    if not isinstance(value, list) or any(type(v) is not int or not 0 <= v < size for v in value):
        raise ValueError(f"{name}索引无效")
    if len(value) != len(set(value)):
        raise ValueError(f"{name}包含重复索引")
    return value


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"计划包含重复字段: {key}")
        result[key] = value
    return result


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("计划须为有限JSON数据") from error


def validate_check_options(options):
    """Normalize software bounds; defaults are not certified robot parameters."""
    if not isinstance(options, dict) or set(options) - DEFAULT_OPTIONS.keys():
        raise ValueError("检查参数包含未知字段")
    defaults = DESKTOP_CHECK_DEFAULTS if options.get("require_pose_settling") is False else DEFAULT_OPTIONS
    result = defaults | options
    require_settling = result.pop("require_pose_settling")
    if type(require_settling) is not bool:
        raise ValueError("require_pose_settling须为布尔值")
    abort_torque = _abort_torque(result.pop("abort_torque_nm"), allow_disabled=not require_settling)
    result = {name: _number(value, name) for name, value in result.items()}
    result["require_pose_settling"] = require_settling
    result["abort_torque_nm"] = abort_torque
    intervals = {
        "speed_deg_s": (0, 20), "search_speed_deg_s": (0, 1),
        "kp": (0, 50), "kd": (-1e-12, 5),
        "contact_torque_nm": (0, 100), "torque_rise_nm": (0, 100),
        "hold_s": (0.299999999, 5),
        "tolerance_deg": (-1e-12, 3), "search_overtravel_deg": (-1e-12, 3),
        "start_tolerance_deg": (-1e-12, PREPARATION_MAX_TOLERANCE_DEG),
        "search_timeout_s": (0.999999999, 120),
    }
    for name, (low, high) in intervals.items():
        if not low < result[name] <= high:
            raise ValueError(f"{name}超出软件范围")
    for name in ("position_span_deg", "retreat_deg", "stop_velocity_deg_s", "stop_timeout_s"):
        if name == "position_span_deg" and not require_settling and result[name] in (0.15, 0.2):
            continue  # Accept the former normalized API value, too.
        if result[name] != DEFAULT_OPTIONS[name]:
            raise ValueError(f"{name}本版本固定为{DEFAULT_OPTIONS[name]}")
    if abort_torque is not None and abort_torque <= result["contact_torque_nm"]:
        raise ValueError("中止力矩须大于接触力矩")
    return result


def validate_probe_pairing(options):
    """Check candidate MIT parameters, without certifying a physical torque.

    The ideal Kp * position-error value is only a parameter-pairing check. It
    does not establish firmware gain units, actual load, motor capacity or a
    safe mechanical travel allowance. Hardware/UI callers opt in explicitly;
    synthetic state-machine tests may inject torque feedback independently.
    """
    result = validate_check_options(options)
    if not result["require_pose_settling"]:
        return result
    ideal_increment = result["kp"] * math.radians(result["search_overtravel_deg"])
    if ideal_increment + 1e-12 < result["torque_rise_nm"]:
        raise ValueError("候选参数不配合：理想Kp×搜索余量小于力矩升幅，不能据此期待检出候选；请核对参数")
    return result


def validate_preparation_options(options):
    """Validate position preparation without authorizing contact/search tests."""
    if not isinstance(options, dict) or set(options) - (DEFAULT_OPTIONS.keys() | {
            "preparation_timeout_s", "gain_profile", "preparation_tolerance_deg", "reset_profile"}):
        raise ValueError("复位参数包含未知字段")
    supplied = {name: _number(value, name) for name, value in options.items()
                if name not in {"gain_profile", "require_pose_settling", "reset_profile", "abort_torque_nm"}}
    require_settling = options.get("require_pose_settling", True)
    if type(require_settling) is not bool:
        raise ValueError("require_pose_settling须为布尔值")
    profile_id = options.get("gain_profile")
    if "gain_profile" in options and profile_id not in {
            preparation_gain_profile(dof)["profile_id"] for dof in (24, 28)}:
        raise ValueError("复位增益配置与绑定版本不符")
    used = {"speed_deg_s", "kp", "kd", "stop_velocity_deg_s", "stop_timeout_s",
            "preparation_timeout_s", "preparation_tolerance_deg"}
    # Search/contact options are unused by preparation and stay at inert defaults.
    defaults = DEFAULT_OPTIONS if require_settling else DESKTOP_CHECK_DEFAULTS
    result = (defaults | {"speed_deg_s": PREPARATION_DEFAULT_SPEED_DEG_S,
                         "preparation_timeout_s": PREPARATION_TIMEOUT_S,
                         "preparation_tolerance_deg": PREPARATION_DEFAULT_TOLERANCE_DEG}
              | {name: value for name, value in supplied.items() if name in used})
    result["abort_torque_nm"] = _abort_torque(
        options.get("abort_torque_nm", defaults["abort_torque_nm"]),
        allow_disabled=not require_settling)
    for name, low, high in (("speed_deg_s", 0, PREPARATION_MAX_SPEED_DEG_S), ("kp", 0, 50),
                            ("kd", -1e-12, 5),
                            ("preparation_timeout_s", 0, PREPARATION_TIMEOUT_S)):
        if not low < result[name] <= high:
            raise ValueError(f"复位{name}超出软件范围")
    if not 0.5 <= result["preparation_tolerance_deg"] <= PREPARATION_MAX_TOLERANCE_DEG:
        raise ValueError("复位到位允许偏差须为0.5至15°")
    for name in ("stop_velocity_deg_s", "stop_timeout_s"):
        if result[name] != DEFAULT_OPTIONS[name]:
            raise ValueError(f"复位{name}本版本固定为{DEFAULT_OPTIONS[name]}")
    if profile_id is not None:
        result["gain_profile"] = profile_id
    result["require_pose_settling"] = require_settling
    if "reset_profile" in options:
        if options["reset_profile"] != DEPLOY_RESET_PROFILE or require_settling or profile_id is None:
            raise ValueError("deploy复位须使用绑定增益、固定4秒配置且不要求到位停稳")
        result["reset_profile"] = DEPLOY_RESET_PROFILE
    return result


def validate_video_plan(data, dof):
    """Validate the original five/six-group preview without asserting real safety."""
    if type(dof) is not int or dof not in (24, 28) or not isinstance(data, dict):
        raise ValueError("仅支持24/28关节视频计划")
    plan = copy.deepcopy(data)
    supplied_digest = plan.pop("plan_sha256", None)
    plan.pop("source_file_sha256", None)
    plan.pop("source_path", None)
    _canonical(plan)
    if plan.get("dof") != dof or type(plan.get("dof")) is not int:
        raise ValueError("计划型号与所选型号不一致")
    if plan.get("source_kind") != "simulated" or plan.get("mode") != "kinematic_one_sided_endpoint_preview":
        raise ValueError("须使用明确标注simulated的单侧视频方案")
    for key in ("robot_connected", "torque_stop_verified", "mechanical_reference_verified",
                "source_ranges_modified"):
        if plan.get(key) is not False:
            raise ValueError(f"原视频来源字段{key}不应声明实机验收")
    if plan.get("calibration_passed") is not None:
        raise ValueError("视频计划不能声明标定通过")
    if plan.get("selected_one_endpoint_per_joint") is not True:
        raise ValueError("须每关节选择一侧端点")
    for key in ("model_sha256", "urdf_sha256"):
        value = plan.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"{key}无效")
    names = plan.get("joint_names")
    if (not isinstance(names, list) or len(names) != dof
            or any(not isinstance(n, str) or not n or len(n) > 128 for n in names)
            or len(set(names)) != dof):
        raise ValueError("关节名称必须唯一并覆盖全部关节")
    initial = _vector(plan.get("initial_pose_rad"), dof, "初始姿态")
    ranges = plan.get("original_ranges_rad")
    if not isinstance(ranges, list) or len(ranges) != dof:
        raise ValueError("关节范围缺失")
    for i, pair in enumerate(ranges):
        low, high = _vector(pair, 2, "关节范围")
        if not -2 * math.pi <= low < high <= 2 * math.pi or not low <= initial[i] <= high:
            raise ValueError("关节范围或初始姿态无效")
    groups = plan.get("group_order")
    count = 5 if dof == 24 else 6
    if not isinstance(groups, list) or len(groups) != count or not all(isinstance(g, str) and g for g in groups) or len(set(groups)) != count:
        raise ValueError("视频检查组顺序无效")
    outcomes = plan.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != dof:
        raise ValueError("每个关节须恰有一个端点")
    if any(not isinstance(o, dict) or not isinstance(o.get("joint"), str) for o in outcomes) or {o.get("joint") for o in outcomes} != set(names):
        raise ValueError("端点关节名称未完整覆盖")
    for outcome in outcomes:
        i = names.index(outcome["joint"])
        side = outcome.get("direction")
        if side not in ("lower", "upper") or outcome.get("group") not in groups:
            raise ValueError("端点方向/分组无效")
        expected = ranges[i][0 if side == "lower" else 1]
        if abs(_number(outcome.get("requested_rad"), "参考端点") - expected) > 1e-9:
            raise ValueError("端点参考与原URDF范围不一致")
        if abs(_number(outcome.get("planned_rad"), "规划端点") - expected) > 1e-9:
            raise ValueError("规划受限端点不能冒充参考端点")
        if (outcome.get("source_kind") != "simulated" or outcome.get("actual_measurement") is not False
                or outcome.get("torque_stop_verified") is not False):
            raise ValueError("端点来源必须保留模拟/未实测标记")
    segments = plan.get("segments")
    if not isinstance(segments, list) or len(segments) != 4 * count + 1:
        raise ValueError("每组须保留接近、末端、记录、缓退四段")
    previous, elapsed, selected_all = initial, 0.0, []
    for si, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError("轨迹段须为对象")
        start = _vector(segment.get("start"), dof, "段起点")
        end = _vector(segment.get("end"), dof, "段终点")
        duration = _number(segment.get("duration_s"), "段时长")
        if not 0 < _number(segment.get("speed_limit_deg_s"), "视频段速度") <= 5:
            raise ValueError("视频速度超过软件范围")
        if not 0 < duration <= 3600 or abs(_number(segment.get("start_s"), "段开始时间") - elapsed) > 1e-6:
            raise ValueError("轨迹段时间不连续")
        if any(abs(a - b) > 1e-8 for a, b in zip(start, previous)):
            raise ValueError("轨迹段位置不连续")
        for pose in (start, end):
            if any(not ranges[i][0] - 1e-9 <= p <= ranges[i][1] + 1e-9 for i, p in enumerate(pose)):
                raise ValueError("视频轨迹超出模型范围")
        selected = _indices(segment.get("selected"), dof, "主检查关节")
        _indices(segment.get("outcome_indices"), dof, "端点结果")
        if si < count * 4:
            gi, part = divmod(si, 4)
            if segment.get("group") != groups[gi] or segment.get("phase") != GROUP_PHASES[part]:
                raise ValueError("不得改写视频分组或段顺序")
            expected_indices = [names.index(o["joint"]) for o in outcomes if o["group"] == groups[gi]]
            if set(selected) != set(expected_indices):
                raise ValueError("段主轴与端点分组不一致")
            if part == 0:
                selected_all.extend(selected)
            if part in (1, 2):
                for i in selected:
                    expected = next(o["requested_rad"] for o in outcomes if o["joint"] == names[i])
                    if abs(end[i] - expected) > 1e-8:
                        raise ValueError("末端段须到达所选参考")
            if part == 1 and any(abs(end[i] - start[i]) > math.radians(2.01) for i in selected):
                raise ValueError("末端低速段超过2°")
            expected_outcomes = [i for i, o in enumerate(outcomes) if o["group"] == groups[gi]] if part == 2 else []
            if segment["outcome_indices"] != expected_outcomes:
                raise ValueError("端点记录索引不一致")
        elif selected or segment.get("phase") != "返回初始工装姿态" or any(abs(a - b) > 1e-8 for a, b in zip(end, initial)):
            raise ValueError("最终返回段无效")
        previous, elapsed = end, elapsed + duration
    if sorted(selected_all) != list(range(dof)):
        raise ValueError("各组须每关节恰检查一次")
    if abs(_number(plan.get("duration_s"), "计划总时长") - elapsed) > 1e-6:
        raise ValueError("总时长不一致")
    digest = hashlib.sha256(_canonical(plan)).hexdigest()
    if supplied_digest is not None and supplied_digest != digest:
        raise ValueError("计划摘要不一致")
    plan["plan_sha256"] = digest
    return plan


def load_video_plan(path, dof):
    path = Path(path)
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError("视频计划过大")
    raw = path.read_bytes()
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("视频计划不是有效JSON") from error
    plan = validate_video_plan(data, dof)
    plan.update(source_path=str(path.resolve()), source_file_sha256=hashlib.sha256(raw).hexdigest())
    return plan


def builtin_video_plan(dof):
    if type(dof) is not int or dof not in (24, 28):
        raise ValueError("仅支持24/28关节")
    item = resources.files("rp1_monitor").joinpath("data", f"calibration-check-{dof}.json")
    raw = item.read_bytes()
    plan = validate_video_plan(json.loads(raw, object_pairs_hook=_no_duplicates), dof)
    plan.update(source_path=f"package:data/calibration-check-{dof}.json",
                source_file_sha256=hashlib.sha256(raw).hexdigest())
    return plan


def start_pose_report(plan, positions, tolerance_deg=3.0):
    """Compare preparation posture, not calibration accuracy; no motion or I/O."""
    dof = plan.get("dof") if isinstance(plan, dict) else None
    names = plan.get("joint_names") if isinstance(plan, dict) else None
    if (type(dof) is not int or dof not in (24, 28) or not isinstance(names, list)
            or len(names) != dof or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != dof):
        raise ValueError("检查起始姿态的具名关节清单无效")
    actual = _vector(positions, dof, "当前逻辑角度")
    targets = _vector(plan.get("initial_pose_rad"), dof, "检查起始目标")
    tolerance = _number(tolerance_deg, "起始姿态允许偏差")
    if not 0 <= tolerance <= PREPARATION_MAX_TOLERANCE_DEG:
        raise ValueError("起始姿态允许偏差须为0至15°")
    return [dict(joint=name, actual_rad=value, target_rad=target,
                 deviation_deg=math.degrees(value - target),
                 within_tolerance=abs(value - target) <= math.radians(tolerance) + 1e-12)
            for name, value, target in zip(names, actual, targets)]


class PreparationMove:
    """Slow candidate route from complete live angles to the video start pose.

    All named logical joints share one seventh-order path parameter. This keeps
    their interpolation continuous, but does not establish collision clearance
    or physical reachability, especially for closed-chain ankle mechanisms.
    This class performs no hardware I/O; the caller must stop on any exception.
    """

    def __init__(self, plan, options, initial_positions, now_s=0.0, *, deadline_s=None):
        self.plan = validate_video_plan(plan, plan.get("dof") if isinstance(plan, dict) else None)
        self.options = validate_preparation_options(options)
        self.dof = self.plan["dof"]
        profile = preparation_gain_profile(self.dof)
        self.deploy_reset = self.options.get("reset_profile") == DEPLOY_RESET_PROFILE
        self.gain_ramp_s = profile["ramp_s"] if self.options.get("gain_profile") and not self.deploy_reset else 0.0
        if self.options.get("gain_profile") not in (None, profile["profile_id"]):
            raise ValueError("复位增益配置与机型不符")
        # Allow the bounded load trim to converge; the complete operation still
        # has the separately confirmed, maximum 90 second deadline.
        self.settling_timeout_s = 8.0 if self.gain_ramp_s else self.options["stop_timeout_s"]
        if not self.options["require_pose_settling"]:
            self.settling_timeout_s = 0.0
        self.started = self.now = _number(now_s, "准备单调时间")
        timeout = self.options["preparation_timeout_s"]
        self.deadline = (self.started + timeout if deadline_s is None
                         else _number(deadline_s, "准备完成截止时间"))
        if not 0 < self.deadline - self.started <= timeout:
            raise ValueError(f"复位完成期限须在{timeout:g}秒内")
        self.initial_positions = self._bounded_positions(initial_positions, "准备初始反馈")
        self.end_positions = self._bounded_positions(self.plan["initial_pose_rad"], "准备目标")
        self.effective_speed_deg_s = None if self.deploy_reset else self.options["speed_deg_s"]
        speed = math.radians(self.options["speed_deg_s"])
        individual = [] if self.deploy_reset else [
            SmoothMove.bounded(
                start, end, speed_rad_s=speed,
                acceleration_rad_s2=math.radians(1), jerk_rad_s3=math.radians(2),
                maximum_stroke_rad=(high - low if self.options["require_pose_settling"] else
                                    max(high - low, abs(end - start)) + 1e-12),
            )
            for start, end, (low, high) in zip(
                self.initial_positions, self.end_positions, self.plan["original_ranges_rad"])
        ]
        # Leave the complete settling window and one maximum control-cycle gap;
        # never meet a time target by raising speed, acceleration, jerk or torque.
        motion_duration = DEPLOY_RESET_DURATION_S if self.deploy_reset else max(move.duration_s for move in individual)
        self.duration_s = self.gain_ramp_s + motion_duration
        required = self.duration_s + self.settling_timeout_s + PREPARATION_CYCLE_MARGIN_S
        if required > self.deadline - self.started:
            raise ValueError(f"当前姿态与限速的复位预算需{required:.1f}秒，无法在{timeout:g}秒内完成"
                             f"（本次剩余预算{self.deadline - self.started:.1f}秒）；"
                             "请先缩小起始姿态差或核对独立复位速度，不自动提高速度或力矩")
        self.moves = [SmoothMove(move.start_rad, move.end_rad, motion_duration)
                      for move in individual]
        self.phase = "gain_ramp" if self.gain_ramp_s else "preparing"
        self.phase_started = self.started
        self.targets = self.initial_positions[:]
        self.previous_times = None
        self.previous_positions = self.initial_positions[:]
        self.stopped_since = [None] * self.dof
        self.stop_last_times = [None] * self.dof
        self.stop_windows = [[] for _ in range(self.dof)]

    def sample_targets(self, elapsed_s):
        """Hold the fresh initial target while gains ramp, then move smoothly."""
        if self.deploy_reset:
            phase = max(0.0, min(1.0, elapsed_s / DEPLOY_RESET_DURATION_S))
            blend = phase * phase * (3.0 - 2.0 * phase)
            return [a + blend * (b - a) for a, b in zip(self.initial_positions, self.end_positions)]
        return [move.sample(max(0.0, elapsed_s - self.gain_ramp_s))[0] for move in self.moves]

    def _bounded_positions(self, values, name):
        positions = _vector(values, self.dof, name)
        if not self.options["require_pose_settling"]:
            return positions
        for index, (position, (low, high)) in enumerate(zip(
                positions, self.plan["original_ranges_rad"])):
            if not low <= position <= high:
                raise ValueError(f"{name}超出模型范围：{self.plan['joint_names'][index]}；不能自动截断角度")
        return positions

    def _settling_detail(self, positions):
        details = []
        tolerance = self.options["preparation_tolerance_deg"]
        for index, (actual, target) in enumerate(zip(positions, self.end_positions)):
            start, end = self.stopped_since[index], self.stop_last_times[index]
            error = math.degrees(actual - target)
            if (abs(error) <= tolerance and start is not None and end is not None
                    and end - start >= 0.5 - 1e-9):
                continue
            name = self.plan["joint_names"][index]
            window = self.stop_windows[index]
            complete_window = len(window) >= 3 and window[-1][0] - window[0][0] >= 0.5 - 1e-9
            spread = max(value for _, value in window) - min(value for _, value in window) if window else None
            stationary = (complete_window and spread <=
                          math.radians(self.options["stop_velocity_deg_s"]) * 0.5 + 1e-12)
            if abs(error) > tolerance:
                reason = ("已静止但未到位，" if stationary else "未到位，") + f"目标偏差{error:+.3f}°（允许±{tolerance:g}°）"
            elif len(window) < 3 or window[-1][0] - window[0][0] < 0.5 - 1e-9:
                span = window[-1][0] - window[0][0] if window else 0.0
                reason = f"连续新鲜反馈窗口{span:.2f}/0.50秒"
            else:
                change = math.degrees(max(value for _, value in window) - min(value for _, value in window))
                reason = f"停稳窗口角度变化{change:.4f}°（允许≤0.075°）"
            details.append(name + " " + reason)
        return f"{len(details)}个关节未就绪；" + "；".join(details[:3])

    def step(self, now_s, positions_rad, torques_nm, feedback_times=None):
        """Return the bounded target; completion requires distinct fresh samples."""
        now = _number(now_s, "准备单调时间")
        if now < self.now or now - self.now > 0.25:
            raise ValueError("准备控制节拍倒退或中断，须中止并失能")
        if now - self.started > 3600:
            raise ValueError("准备运行超过一小时软件上限，须中止并失能")
        if self.phase != "prepared" and now > self.deadline:
            raise ValueError(f"复位超过{self.options['preparation_timeout_s']:g}秒完成期限，"
                             "未确认到位停稳，须中止并失能")
        positions = self._bounded_positions(positions_rad, "准备关节位置")
        self._bounded_positions(self.targets, "准备当前目标")
        torques = _vector(torques_nm, self.dof, "准备关节力矩")
        times = ([now] * self.dof if feedback_times is None
                 else _vector(feedback_times, self.dof, "准备反馈时间"))
        if any(time > now + 1e-6 or now - time > 0.25 for time in times):
            raise ValueError("准备反馈过期或时间基准无效，须中止并失能")
        if self.previous_times is not None:
            if any(time < old for time, old in zip(times, self.previous_times)):
                raise ValueError("准备反馈时间倒退，须中止并失能")
        if self.options["abort_torque_nm"] is not None and any(
                abs(torque) >= self.options["abort_torque_nm"] for torque in torques):
            raise ValueError("准备超过中止力矩，须中止并失能")
        # A preparation lag is not an instantaneous failure at an arbitrary
        # five-degree boundary. Continue the bounded route; actual arrival must
        # still earn the configured settling gate within the confirmed deadline.
        tracking_limit = max(5.0, self.options["preparation_tolerance_deg"])
        if self.options["require_pose_settling"] and self.phase == "prepared" and any(
                abs(actual - target) > math.radians(tracking_limit) + 1e-12 for actual, target in zip(positions, self.targets)):
            raise ValueError(f"检查起点保持偏差超过{tracking_limit:g}°，须中止并失能")
        fresh = [self.previous_times is None or time > self.previous_times[index]
                 for index, time in enumerate(times)]
        events = []
        if self.phase in ("gain_ramp", "preparing"):
            elapsed = now - self.started
            if elapsed >= self.gain_ramp_s:
                self.phase = "preparing"
            self.targets = self._bounded_positions(
                self.sample_targets(elapsed), "准备采样目标")
            if elapsed >= self.duration_s:
                self.phase, self.phase_started = ("confirm_stop" if self.options["require_pose_settling"] else "prepared"), now
                events.append({"type": "preparation_target_reached", "collision_verified": False})
                if self.phase == "prepared":
                    events.append({"type": "preparation_targets_sent", "actual_arrival_verified": False,
                                   "collision_verified": False})
        elif self.phase == "confirm_stop":
            if now - self.phase_started > self.settling_timeout_s:
                raise ValueError("准备结束后未在时限内取得停稳反馈，须中止并失能；" + self._settling_detail(positions))
            for index, is_fresh in enumerate(fresh):
                window = self.stop_windows[index]
                if not is_fresh:
                    # A paired ankle can change when one motor updates while
                    # the older pair timestamp remains unchanged. Do not call
                    # that a new velocity sample or let it accumulate settling.
                    if abs(positions[index] - self.previous_positions[index]) > 1e-12:
                        window.clear()
                        self.stopped_since[index] = None
                        self.stop_last_times[index] = None
                    continue
                if window and times[index] - window[-1][0] > 0.25:
                    window.clear()
                window.append((times[index], positions[index]))
                # Keep one sample at/before the left boundary. Dropping it
                # would make a jittered receive window perpetually <0.5s.
                cutoff = times[index] - 0.5
                while len(window) >= 3 and window[1][0] <= cutoff + 1e-9:
                    window.pop(0)
                spread = max(value for _, value in window) - min(value for _, value in window)
                # One native encoder step is ~0.022 degrees. Its per-frame
                # derivative is not evidence of physical motion. Require a
                # bounded spread over the full half-second evidence window.
                # Stationarity and reaching the requested pose are independent.
                # Preserve off-target samples for diagnostics, while requiring
                # the entire evidence window to meet the original pose gate.
                settled = (spread <= math.radians(self.options["stop_velocity_deg_s"]) * 0.5 + 1e-12
                           and all(abs(value - self.end_positions[index]) <= math.radians(self.options["preparation_tolerance_deg"]) + 1e-12
                                   for _, value in window))
                self.stopped_since[index] = window[0][0] if settled else None
                self.stop_last_times[index] = times[index] if settled else None
            if all(start is not None and end is not None and end - start >= 0.5 - 1e-9
                   and len(window) >= 3 for start, end, window in zip(
                       self.stopped_since, self.stop_last_times, self.stop_windows)):
                self.phase = "prepared"
                events.append({"type": "preparation_complete", "collision_verified": False})
        self.previous_positions = [positions[index] if is_fresh else self.previous_positions[index]
                                   for index, is_fresh in enumerate(fresh)]
        self.previous_times = times
        self.now = now
        progress = min(0.95, max(0.0, 0.95 * (now - self.started) / self.duration_s))
        if self.phase == "confirm_stop":
            spans = [0.0 if start is None or end is None else end - start
                     for start, end in zip(self.stopped_since, self.stop_last_times)]
            progress = 0.95 + 0.05 * min(1.0, min(spans) / 0.5)
        return {"targets_rad": self.targets[:], "phase": self.phase,
                "progress": 1.0 if self.phase == "prepared" else progress,
                "done": self.phase == "prepared", "events": events}


class CalibrationCheck:
    """One video group with feedback-based stop detection, then review/replanning.

    ``positions_rad`` and ``torques_nm`` must share the plan's named logical
    coordinates. Raw motor torques/angles, especially ankle pairs, do not satisfy
    that contract. Explicit feedback timestamps use the caller's monotonic clock.
    """

    def __init__(self, plan, options, initial_positions, now_s=0.0):
        self.plan = validate_video_plan(plan, plan.get("dof"))
        self.options = validate_check_options(options)
        self.contact_span_deg = self.options["position_span_deg"]
        self.dof = self.plan["dof"]
        self.positions = _vector(initial_positions, self.dof, "初始反馈")
        if self.options["require_pose_settling"] and not all(row["within_tolerance"] for row in start_pose_report(
                self.plan, self.positions, self.options["start_tolerance_deg"])):
            raise ValueError("请先按已核对的预定位路径到视频初始工装姿态；不自动生成转场")
        self.now = _number(now_s, "单调时间")
        self.started = self.phase_started = self.now
        self.group_index = 0
        self.group = self.plan["group_order"][0]
        self.phase = "approach"
        self.targets = self.plan["initial_pose_rad"][:]
        self.previous_times = None
        self.previous_positions = self.positions[:]
        self.history = [[] for _ in range(self.dof)]
        self.baselines = [[] for _ in range(self.dof)]
        self.group_initial = self.positions[:]
        self.outcomes = []
        self.candidates = set()
        self.missing = False
        self.early = False
        self.stop_since = None
        self.frozen = None
        self.retreat_start = None
        self.search_trace = []
        self.trace_problem = None
        self.retreat_verified = False
        self.retreat_actual_deg = None
        self.retreat_joint_distances = []
        self.retreats = []
        self.abort_reason = None
        self.events = []
        self._set_approach()
        self.approach = self.approach | {"start": self.positions[:]}
        self._approach_timing()

    def _set_approach(self):
        self.approach, self.search, _, self.video_retreat = self.plan["segments"][self.group_index * 4:self.group_index * 4 + 4]
        self.selected = self.approach["selected"]
        self.directions = {i: (1 if self.search["end"][i] > self.search["start"][i] else -1) for i in self.selected}
        self._approach_timing()

    def _approach_timing(self):
        maximum = max(abs(a - b) for a, b in zip(self.approach["start"], self.approach["end"]))
        requested = 2.1875 * maximum / math.radians(self.options["speed_deg_s"])
        if self.options["require_pose_settling"]:
            self.approach_duration = max(self.approach["duration_s"], requested)
        else:
            self.approach_duration = self._bounded_duration(self.approach["start"], self.approach["end"],
                                                          self.options["speed_deg_s"])

    def _interpolate(self, start, end, elapsed, duration):
        return [SmoothMove(a, b, duration).sample(elapsed)[0] for a, b in zip(start, end)]

    def _freeze(self, now, positions, candidates, missing=False):
        self.candidates = set(candidates)
        self.missing = missing
        self.frozen = positions[:]
        self.targets = positions[:]
        self.phase, self.phase_started, self.stop_since = "confirm_stop", now, None
        self.history = [[] for _ in range(self.dof)]
        for i in candidates:
            expected = self.search["end"][i]
            deviation = math.degrees(positions[i] - expected)
            early = self.directions[i] * (expected - positions[i]) > math.radians(self.options["tolerance_deg"])
            self.early = self.early or early
            self.events.append({"type": "unexpected_obstruction" if early else "candidate_obstruction",
                                "joint": self.plan["joint_names"][i], "position_rad": positions[i],
                                "deviation_deg": deviation})
        if missing:
            self.events.append({"type": "endpoint_not_detected", "group": self.group})
        self.events.append({"type": "group_frozen", "group": self.group,
                            "reason": "全组暂停，避免产生未预检的混合姿态路径"})

    def _record(self, positions):
        for i in self.selected:
            measured = self.frozen[i] if i in self.candidates else None
            expected = self.search["end"][i]
            deviation = None if measured is None else math.degrees(measured - expected)
            self.outcomes.append({
                "joint": self.plan["joint_names"][i], "group": self.group,
                "expected_rad": expected, "measured_rad": measured, "deviation_deg": deviation,
                "within_tolerance": None if measured is None else abs(deviation) <= self.options["tolerance_deg"],
                "status": ("unexpected_obstruction" if measured is not None and self.directions[i] * (expected - measured) > math.radians(self.options["tolerance_deg"]) else
                           "candidate_obstruction" if measured is not None else
                           "endpoint_not_detected" if self.missing else "not_covered"),
                "candidate_only": True, "calibration_passed": None,
                "physical_stop_confirmed": False,
            })
        self.events.append({"type": "results", "outcomes": copy.deepcopy(self.outcomes)})

    def _prepare_retreat(self, now, positions):
        """Replay whole measured poses in reverse; never compose axes anew."""
        points, distances = [positions[:]], [0.0]
        limit = math.radians(self.options["retreat_deg"])
        axes = self.candidates or set(self.selected)
        achieved = 0.0
        for _, pose in reversed(self.search_trace):
            distance = self._distance(points[-1], pose)
            # Two native encoder steps are noise, not useful retreat progress.
            # Kept waypoints remain complete measured robot postures.
            if distance <= math.radians(0.045):
                continue
            progress = {i: self.directions[i] * (positions[i] - pose[i]) for i in axes}
            if all(progress[i] >= limit for i in axes):
                # Stop when every candidate axis retreats a net 1°, rather
                # than when quantization or auxiliary movement sums to 1°.
                fractions = []
                for i in axes:
                    previous = self.directions[i] * (positions[i] - points[-1][i])
                    fractions.append(0.0 if previous >= limit else
                                     (limit - previous) / (progress[i] - previous))
                ratio = max(fractions)
                pose = [a + (b - a) * ratio for a, b in zip(points[-1], pose)]
                distance = self._distance(points[-1], pose)
            points.append(pose[:])
            distances.append(distances[-1] + distance)
            achieved = min(self.directions[i] * (positions[i] - pose[i]) for i in axes)
            if achieved >= limit - 1e-10:
                break
        early_abort = self.early and self.options["require_pose_settling"]
        if early_abort or self.trace_problem or achieved < limit - math.radians(0.05):
            self.phase = "aborted"
            self.abort_reason = ("候选阻挡早于参考容差，已中止检查" if early_abort else
                                 self.trace_problem or "连续实测退路不能使全部候选轴净退1°")
            self.retreat_joint_distances = [
                {"joint": self.plan["joint_names"][i], "requested_deg": self.options["retreat_deg"],
                 "actual_deg": None, "verified": False} for i in sorted(axes)
            ]
            self.retreats.append({"group": self.group, "requested_deg": self.options["retreat_deg"],
                                 "actual_deg": None, "verified": False, "reason": self.abort_reason,
                                 "joint_distances": copy.deepcopy(self.retreat_joint_distances)})
            self.events.append({"type": "review_required", "group": self.group,
                                "reason": self.abort_reason + "；未自动缓退，须现场核对路线"})
            return
        self.retreat_start = positions[:]
        self.retreat_points, self.retreat_distances = points, distances
        self.retreat_end = points[-1][:]
        self.retreat_duration = self._bounded_duration(self.retreat_start, self.retreat_end,
                                                       self.options["search_speed_deg_s"])
        self.retreat_duration = max(self.retreat_duration,
                                    2.1875 * distances[-1] / math.radians(self.options["search_speed_deg_s"]))
        self.phase, self.phase_started = "retreat", now
        self.events.append({"type": "retreat_started", "group": self.group,
                            "measured_pose_count": len(points),
                            "requested_deg": self.options["retreat_deg"],
                            "planned_net_deg": math.degrees(achieved),
                            "reason": "沿连续实测整姿态历史逆向缓退，全部候选轴净退1°；不以抖动累计弧长代替退步"})

    @staticmethod
    def _distance(start, end):
        return max(abs(a - b) for a, b in zip(start, end))

    def _trace(self, positions, times, fresh):
        if not all(fresh):
            return
        timestamp = min(times)
        if self.search_trace:
            previous_time, previous = self.search_trace[-1]
            if timestamp <= previous_time:
                return
            if timestamp - previous_time > 0.25:
                self.trace_problem = "实测退路反馈时间不连续，须现场核对路线"
            if self._distance(previous, positions) > math.radians(0.2):
                self.trace_problem = "实测退路角度不连续，须现场核对路线"
            # Allow two native position-code steps of quantization noise, not
            # a meaningful reversed motion while approaching the endpoint.
            if self.options["require_pose_settling"] and any(self.directions[i] * (positions[i] - previous[i]) < -math.radians(0.045)
                   for i in self.selected):
                self.trace_problem = "搜索期间反馈出现反向运动，须现场核对路线"
        self.search_trace.append((timestamp, positions[:]))
        while len(self.search_trace) > 2:
            # Retain two degrees of genuine signed primary-axis progress. A
            # static ±one-code reading must not evict the earlier real motion.
            prior = self.search_trace[1][1]
            if min(self.directions[i] * (positions[i] - prior[i]) for i in self.selected) < math.radians(2):
                break
            self.search_trace.pop(0)
        trace_limit = 2000 if self.options["require_pose_settling"] else math.ceil(self.options["search_timeout_s"] * 100) + 500
        if len(self.search_trace) > trace_limit:
            self.trace_problem = "实测退路记录超限，须现场核对路线"
            self.search_trace = self.search_trace[-trace_limit:]

    def _sample_retreat(self, elapsed):
        distance = SmoothMove(0, self.retreat_distances[-1], self.retreat_duration).sample(elapsed)[0]
        for index in range(1, len(self.retreat_distances)):
            if distance <= self.retreat_distances[index]:
                low, high = self.retreat_distances[index - 1:index + 1]
                ratio = (distance - low) / (high - low)
                return [a + (b - a) * ratio for a, b in
                        zip(self.retreat_points[index - 1], self.retreat_points[index])]
        return self.retreat_end[:]

    def _bounded_duration(self, start, end, speed):
        distance = max(abs(a - b) for a, b in zip(start, end))
        return max(0.25, 2.1875 * distance / math.radians(speed),
                   math.sqrt(8 * distance / math.radians(2)),
                   (60 * distance / math.radians(3)) ** (1 / 3))

    def continue_group(self, now_s, positions_rad, *, confirmed=False):
        """Use a freshly stopped, operator-confirmed route into the next group.

        Confirmation is a field route review, never a collision-verification
        assertion. A slow bounded transition recovers the video's next start.
        """
        now = _number(now_s, "单调时间")
        positions = _vector(positions_rad, self.dof, "继续前反馈")
        if (self.phase != "waiting_review" or confirmed is not True
                or self.early and self.options["require_pose_settling"]
                or self.missing or not self.retreat_verified):
            raise ValueError("仅停稳后并经现场确认的候选组可继续；异常/未检出须结束复核")
        if self.previous_times is None or any(now - t > 0.25 or t > now for t in self.previous_times):
            raise ValueError("继续前须有新鲜停稳反馈")
        if self.options["require_pose_settling"] and not self._stopped():
            raise ValueError("继续前须有持续停稳的新鲜反馈")
        if self.options["require_pose_settling"] and any(abs(a - b) > math.radians(self.options["position_span_deg"])
               for a, b in zip(positions, self.positions)):
            raise ValueError("继续确认期间姿态已改变，须重新取得停稳反馈")
        next_group = self.group_index + 1
        if next_group == len(self.plan["group_order"]):
            target = self.plan["segments"][-1]["start"]
        else:
            target = self.plan["segments"][next_group * 4]["start"]
        if self.options["require_pose_settling"] and any(abs(a - b) > math.radians(self.options["start_tolerance_deg"])
               for a, b in zip(positions, target)):
            raise ValueError(f"距下一段起点超过{self.options['start_tolerance_deg']:g}°，须重新规划或人工预定位")
        self.transition_start = positions[:] if self.options["require_pose_settling"] else self.targets[:]
        self.transition_end = target[:]
        self.transition_duration = self._bounded_duration(self.transition_start, target,
                                                          self.options["search_speed_deg_s"] if self.options["require_pose_settling"] else self.options["speed_deg_s"])
        self.phase, self.phase_started = "transition", now
        self.now = now
        return {"type": "route_confirmed", "group": self.group,
                "next_group": None if next_group == len(self.plan["group_order"]) else self.plan["group_order"][next_group],
                "collision_verified": False}

    def _stopped(self):
        for window in self.history:
            if len(window) < 3 or window[-1][0] - window[0][0] < self.options["hold_s"] - 1e-8:
                return False
            for a, b in zip(window, window[1:]):
                if abs(b[1] - a[1]) / (b[0] - a[0]) > math.radians(self.options["stop_velocity_deg_s"]):
                    return False
        return True

    def _post_contact_dwell_ready(self):
        if self.options["require_pose_settling"]:
            return self._stopped()
        return all(len(window) >= 3 and window[-1][0] - window[0][0] >= self.options["hold_s"] - 1e-8
                   for window in self.history)

    def result(self):
        """Return logical comparison evidence without claiming physical acceptance."""
        return {
            "source_kind": "simulated", "mode": "feedback_calibration_check",
            "dof": self.dof, "phase": self.phase, "group": self.group,
            "group_index": self.group_index,
            "group_count": len(self.plan["group_order"]),
            "plan_sha256": self.plan["plan_sha256"],
            "urdf_sha256": self.plan["urdf_sha256"],
            "outcomes": copy.deepcopy(self.outcomes), "candidate_only": True,
            "calibration_passed": None, "physical_stop_confirmed": False,
            "covered_joint_count": sum(r["measured_rad"] is not None for r in self.outcomes),
            "required_joint_count": self.dof,
            "retreat_requested_deg": self.options["retreat_deg"],
            "retreat_actual_deg": self.retreat_actual_deg,
            "retreat_verified": self.retreat_verified,
            "retreat_joint_distances": copy.deepcopy(self.retreat_joint_distances),
            "retreats": copy.deepcopy(self.retreats), "abort_reason": self.abort_reason,
            "contact_detection": {"window_s": self.options["hold_s"],
                                  "max_actual_span_deg": self.contact_span_deg,
                                  "baseline_method": ("first_25_approach_samples_median" if self.options["require_pose_settling"]
                                                      else "latest_25_approach_samples_median"),
                                  "contact_torque_nm": self.options["contact_torque_nm"],
                                  "torque_rise_nm": self.options["torque_rise_nm"]},
        }

    def step(self, now_s, positions_rad, torques_nm, feedback_times=None):
        now = _number(now_s, "单调时间")
        if now < self.now or now - self.now > 0.25:
            raise ValueError("控制节拍倒退或中断，须中止并失能")
        positions = _vector(positions_rad, self.dof, "关节位置")
        torques = _vector(torques_nm, self.dof, "关节力矩")
        times = [now] * self.dof if feedback_times is None else _vector(feedback_times, self.dof, "反馈时间")
        if any(t > now + 1e-6 or now - t > 0.25 for t in times):
            raise ValueError("反馈过期或时间基准无效，须中止并失能")
        if self.previous_times is not None and any(t < old for t, old in zip(times, self.previous_times)):
            raise ValueError("反馈时间倒退，须中止并失能")
        if self.options["abort_torque_nm"] is not None and any(
                abs(t) >= self.options["abort_torque_nm"] for t in torques):
            raise ValueError("超过中止力矩，须中止并失能")
        if now - self.started > 7200:
            raise ValueError("检查总时长超限，须中止并失能")
        tracking_limit = max(5.0, self.options["start_tolerance_deg"])
        if self.options["require_pose_settling"] and self.phase not in ("aborted", "complete") and any(
                abs(a - b) > math.radians(tracking_limit) + 1e-12 for a, b in zip(positions, self.targets)):
            raise ValueError(f"角度跟踪误差超过{tracking_limit:g}°，须中止并失能")
        fresh = [self.previous_times is None or times[i] > self.previous_times[i] for i in range(self.dof)]
        speeds = [None] * self.dof
        for i in range(self.dof):
            if fresh[i]:
                if self.previous_times is not None:
                    dt = times[i] - self.previous_times[i]
                    speeds[i] = abs(positions[i] - self.previous_positions[i]) / dt
                    if abs(positions[i] - self.previous_positions[i]) > math.radians(5):
                        raise ValueError("角度反馈跳变，须中止并失能")
                self.history[i].append((times[i], positions[i], abs(torques[i]), self.targets[i]))
                cutoff = times[i] - self.options["hold_s"]
                while len(self.history[i]) > 2 and self.history[i][1][0] <= cutoff:
                    self.history[i].pop(0)
                if self.phase == "approach":
                    if not self.options["require_pose_settling"] or len(self.baselines[i]) < 25:
                        self.baselines[i].append(abs(torques[i]))
                    if not self.options["require_pose_settling"] and len(self.baselines[i]) > 25:
                        self.baselines[i].pop(0)
        self.events = []
        if self.phase in ("search", "confirm_stop"):
            self._trace(positions, times, fresh)
        if self.phase == "approach":
            elapsed = now - self.phase_started
            self.targets = self._interpolate(self.approach["start"], self.approach["end"], elapsed, self.approach_duration)
            if elapsed >= self.approach_duration:
                self.phase, self.phase_started = "search", now
                self.history = [[] for _ in range(self.dof)]
                self.search_trace = [(min(times), positions[:])]
                self.trace_problem = None
                self.events.append({"type": "search_started", "group": self.group})
        elif self.phase == "search":
            # Shared path progress preserves the video pairing and avoidance axes.
            maximum = max(abs(a - b) for a, b in zip(self.search["start"], self.search["end"]))
            travel = math.radians(self.options["search_speed_deg_s"]) * (now - self.phase_started)
            ratio = (min((maximum + math.radians(self.options["search_overtravel_deg"])) / maximum, travel / maximum)
                     if self.options["require_pose_settling"] else travel / maximum)
            self.targets = [a + (b - a) * ratio for a, b in zip(self.search["start"], self.search["end"])]
            candidates = []
            for i in self.selected:
                window = self.history[i]
                if not fresh[i] or len(window) < 3 or window[-1][0] - window[0][0] < self.options["hold_s"]:
                    continue
                direction = self.directions[i]
                moved = direction * (positions[i] - self.group_initial[i]) >= math.radians(0.2)
                stagnant = max(p[1] for p in window) - min(p[1] for p in window) <= math.radians(self.contact_span_deg)
                target_progress = direction * (window[-1][3] - window[0][3])
                advancing = (target_progress >= math.radians(0.02) if self.options["require_pose_settling"]
                             else target_progress > 1e-12)
                baseline = statistics.median(self.baselines[i]) if self.baselines[i] else abs(torques[i])
                loaded = min(p[2] for p in window) >= self.options["contact_torque_nm"] and min(p[2] for p in window) - baseline >= self.options["torque_rise_nm"]
                # A motor may already be resting against a stop when search
                # starts. In desktop mode, fresh stationary feedback plus a
                # sustained load rise is evidence without first moving 0.2°.
                if (moved or not self.options["require_pose_settling"]) and stagnant and advancing and loaded:
                    candidates.append(i)
            if candidates:
                self._freeze(now, positions, candidates)
            elif ((self.options["require_pose_settling"] and travel >= maximum + math.radians(self.options["search_overtravel_deg"]) + math.radians(self.options["search_speed_deg_s"]) * self.options["hold_s"])
                  or (not self.options["require_pose_settling"] and now - self.phase_started >= self.options["search_timeout_s"])):
                self._freeze(now, positions, [], missing=True)
        elif self.phase == "confirm_stop":
            if not self.options["require_pose_settling"]:
                # Keep unloading the advancing spring during the post-contact
                # receive dwell. A drifting actual pose must not become a
                # discontinuous first retreat target.
                self.targets = positions[:]
            if now - self.phase_started > self.options["stop_timeout_s"]:
                raise ValueError("未在时限内取得停稳反馈，须中止并失能")
            if all(fresh) and self._post_contact_dwell_ready():
                self._record(positions)
                self._prepare_retreat(now, positions)
        elif self.phase == "retreat":
            elapsed = now - self.phase_started
            self.targets = self._sample_retreat(elapsed)
            if elapsed >= self.retreat_duration:
                self.phase, self.phase_started = "confirm_retreat_stop", now
                self.history = [[] for _ in range(self.dof)]
        elif self.phase == "confirm_retreat_stop":
            if now - self.phase_started > self.options["stop_timeout_s"]:
                raise ValueError("缓退后未取得停稳反馈，须中止并失能")
            if all(fresh) and self._post_contact_dwell_ready():
                axes = self.candidates or set(self.selected)
                self.retreat_joint_distances = [
                    {"joint": self.plan["joint_names"][i], "requested_deg": self.options["retreat_deg"],
                     "actual_deg": math.degrees(self.directions[i] * (self.retreat_start[i] - positions[i]))}
                    for i in sorted(axes)
                ]
                for row in self.retreat_joint_distances:
                    row["verified"] = row["actual_deg"] >= row["requested_deg"] - 0.05
                self.retreat_actual_deg = min(row["actual_deg"] for row in self.retreat_joint_distances)
                self.retreat_verified = all(row["verified"] for row in self.retreat_joint_distances)
                if not self.retreat_verified:
                    self.abort_reason = "缓退反馈有候选轴净退不足1°（量化容差0.05°），须现场复核"
                self.retreats.append({"group": self.group, "requested_deg": self.options["retreat_deg"],
                                     "actual_deg": self.retreat_actual_deg,
                                     "verified": self.retreat_verified, "reason": self.abort_reason,
                                     "joint_distances": copy.deepcopy(self.retreat_joint_distances)})
                self.phase = "aborted" if self.missing or (self.early and self.options["require_pose_settling"]) or not self.retreat_verified else "waiting_review"
                self.events.append({"type": "review_required", "group": self.group,
                                    "reason": self.abort_reason or "缓退停稳后请核对实际姿态，确认下一组恢复路线"})
        elif self.phase == "transition":
            elapsed = now - self.phase_started
            self.targets = self._interpolate(self.transition_start, self.transition_end,
                                             elapsed, self.transition_duration)
            if elapsed >= self.transition_duration:
                if self.group_index + 1 == len(self.plan["group_order"]):
                    self.phase, self.phase_started = "return", now
                    self.return_start = self.targets[:]
                    self.return_end = self.plan["initial_pose_rad"][:]
                    self.return_duration = self._bounded_duration(self.return_start, self.return_end,
                                                                  self.options["speed_deg_s"])
                    if self.options["require_pose_settling"]:
                        self.return_duration = max(self.plan["segments"][-1]["duration_s"], self.return_duration)
                else:
                    self.group_index += 1
                    self.group = self.plan["group_order"][self.group_index]
                    self.phase, self.phase_started = "approach", now
                    self.group_initial = positions[:]
                    self.history = [[] for _ in range(self.dof)]
                    self.baselines = [[] for _ in range(self.dof)]
                    self.candidates = set()
                    self.retreat_verified, self.retreat_actual_deg = False, None
                    self.retreat_joint_distances = []
                    self._set_approach()
        elif self.phase == "return":
            elapsed = now - self.phase_started
            self.targets = self._interpolate(self.return_start, self.return_end, elapsed,
                                             self.return_duration)
            if elapsed >= self.return_duration:
                self.phase, self.phase_started = "confirm_return_stop", now
                self.history = [[] for _ in range(self.dof)]
        elif self.phase == "confirm_return_stop":
            if now - self.phase_started > self.options["stop_timeout_s"]:
                raise ValueError("返回后未取得停稳反馈，须中止并失能")
            if all(fresh) and self._post_contact_dwell_ready():
                self.phase = "complete"
                self.events.append({"type": "check_complete", "outcomes": copy.deepcopy(self.outcomes),
                                    "calibration_passed": None, "physical_stop_confirmed": False})
        self.previous_times, self.previous_positions = times, positions[:]
        self.positions, self.now = positions[:], now
        progress = (self.group_index + min(1.0, (now - self.started) / max(self.approach_duration + 15, 1))) / len(self.plan["group_order"])
        return {"targets_rad": self.targets[:], "phase": self.phase, "group": self.group,
                "progress": 1.0 if self.phase == "complete" else progress,
                "done": self.phase in ("complete", "aborted"),
                "events": copy.deepcopy(self.events)}
