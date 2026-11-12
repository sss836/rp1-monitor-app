"""Prepare an explicit, interactive launch of the reviewed robot calibration tool.

Generating arguments never contacts a robot. Angle inspection does not use this module.
"""

import hashlib
import json
import shlex
from pathlib import Path

from .ssh_transport import parse_ssh_target

CALIBRATION_ROOT = "/home/robo/roboparty_deploy"
CALIBRATION_SCRIPT = CALIBRATION_ROOT + "/scripts/set_zero.py"
CALIBRATION_CONFIG = CALIBRATION_ROOT + "/scripts/config/set_zero_rp1_24dof.yaml"
CALIBRATION_SCRIPT_SHA256 = "5103d897636801c471a0670deff9b7ac70cc3d5d781a8aa065a82f8a27faa921"
CALIBRATION_CONFIG_SHA256 = "81f9a92fa9cf0e91273fbb5817acea2566a298fe8ee06eb5ef6c0e1fd8c5a9a5"
CALIBRATION_CONFIRMATION = "CALIBRATE 24"
CALIBRATION_CONFIG_28 = CALIBRATION_ROOT + "/scripts/config/set_zero_rp1.yaml"
CALIBRATION_CONFIG_28_SHA256 = "a91ef44a2d83083765802f75cfa5af83708ada9f9c5dccc356e6c7122dff877f"
CALIBRATION_RUNTIME_RECEIPT = "/home/robo/.local/state/rp1-monitor/calibration/runtime-build.json"
CALIBRATION_DRIVER_SOURCE_SHA256 = {
    "src/motors/src/drivers/lro/lro_motor_driver.cpp":
        "0d593b397b1071dde3642aaab6c79a3e8883206a47e8cc1a02a195ef7a1f3885",
    "src/motors/src/drivers/lro/lro_motor_driver.hpp":
        "ba9c9192ba95737f48c546c7c799cdaae6f2851f6d438b43f20d320a61513d08",
}

# This short launcher carries identifiers and expected digests only. The
# application code runs from the robot's previously installed collector release.
REMOTE_BOOTSTRAP = r"""
import hashlib
import json
import re
import sys
from pathlib import Path


def run():
    try:
        pointer = json.loads((Path(MANAGED) / "installation.json").read_text(encoding="utf-8"))
        if not isinstance(pointer, dict) or pointer.get("robot_id") != ROBOT_ID:
            raise ValueError("机器人采集端编号与本次标定目标不一致")
        release = pointer.get("release")
        if not isinstance(release, str) or not re.fullmatch(r"[0-9a-f]{32}", release):
            raise ValueError("机器人采集端版本记录无效")
        scripts = Path(MANAGED) / "releases" / release / "scripts"
        sources = {}
        for filename, expected in TOOL_SHA256.items():
            path = scripts / filename
            if path.resolve(strict=True) != path or not path.is_file():
                raise ValueError("机器人标定工具路径无效")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("机器人标定工具与上位机版本不同，请更新一次机器人采集端")
            sources[filename] = data
    except FileNotFoundError as error:
        raise ValueError("机器人尚未安装配套标定工具，请更新一次机器人采集端") from error
    namespace = dict(globals(), __name__="__main__",
                     RUNTIME_HELPER_PATH=str(scripts / "calibration_runtime.py"),
                     RUNTIME_HELPER_BYTES=sources["calibration_runtime.py"])
    exec(compile(sources["calibration_preflight.py"], str(scripts / "calibration_preflight.py"), "exec"), namespace)


if __name__ == "__main__":
    try:
        run()
    except (OSError, ValueError) as error:
        print("拒绝启动标定：" + str(error), flush=True)
        if sys.stdin.isatty():
            input("按 Enter 结束预检终端。")
        raise SystemExit(1)
"""



def preflight_parameters(robot_id, dof=24):
    if type(dof) is not int or dof not in (24, 28):
        raise ValueError("仅支持24或28关节标定配置")
    if (not isinstance(robot_id, str) or not robot_id.strip() or len(robot_id) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in robot_id)):
        raise ValueError("机器人编号无效")
    return {
        "ROOT": CALIBRATION_ROOT,
        "SCRIPT": CALIBRATION_SCRIPT,
        "CONFIG": CALIBRATION_CONFIG if dof == 24 else CALIBRATION_CONFIG_28,
        "SCRIPT_SHA256": CALIBRATION_SCRIPT_SHA256,
        "CONFIG_SHA256": CALIBRATION_CONFIG_SHA256 if dof == 24 else CALIBRATION_CONFIG_28_SHA256,
        "CONFIRMATION": f"CALIBRATE {dof}",
        "DOF": dof,
        "EXPECTED_COUNTS": [7, 7, 5, 5] if dof == 24 else [7, 7, 7, 7],
        "ROBOT_ID": robot_id,
        "DRIVER_SOURCE_SHA256": CALIBRATION_DRIVER_SOURCE_SHA256,
        "RUNTIME_RECEIPT": CALIBRATION_RUNTIME_RECEIPT,
    }


def remote_calibration_script(robot_id, dof=24):
    constants = preflight_parameters(robot_id, dof)
    constants["MANAGED"] = "/home/robo/.local/share/rp1-collector-managed"
    constants["TOOL_SHA256"] = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ("calibration_preflight.py", "calibration_runtime.py")
    }
    return "\n".join(f"{name} = {json.dumps(value, ensure_ascii=True)}"
                     for name, value in constants.items()) + "\n" + REMOTE_BOOTSTRAP


def _existing_path(value, label):
    path = Path(value).expanduser()
    if any(ord(char) < 32 or ord(char) == 127 for char in str(path)):
        raise ValueError(label + "路径无效")
    if not path.is_file():
        raise ValueError(label + "文件不存在")
    return str(path.resolve())


def calibration_ssh_arguments(target, known_hosts, *, robot_id, dof=24, key="", ssh_port=22):
    """Return argv for a terminal; authentication stays in system SSH/sudo prompts."""
    username, host = parse_ssh_target(target)
    if username != "robo":
        raise ValueError("当前审查路径仅适用于robo用户")
    if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
        raise ValueError("SSH端口无效")
    known = _existing_path(known_hosts, "已知主机")
    script = remote_calibration_script(robot_id, dof)
    command = shlex.join([
        "/usr/bin/sudo", "/usr/bin/taskset", "-c", "1-3,7", "/usr/bin/nice", "-n", "10",
        "/usr/bin/python3", "-I", "-B", "-c", script,
    ])
    arguments = [
        "ssh", "-tt", "-F", "/dev/null", "-p", str(ssh_port),
        "-oStrictHostKeyChecking=yes", "-oUserKnownHostsFile=" + known,
        "-oGlobalKnownHostsFile=/dev/null", "-oClearAllForwardings=yes",
        "-oForwardAgent=no", "-oForwardX11=no", "-oPermitLocalCommand=no",
        "-oConnectTimeout=8",
    ]
    if key:
        arguments.extend(["-i", _existing_path(key, "密钥")])
    return arguments + ["--", username + "@" + host, command]
