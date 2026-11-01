"""Prepare an explicit, interactive launch of the reviewed robot calibration tool.

Generating arguments never contacts a robot. Angle inspection does not use this module.
"""

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

# Kept as ordinary Python so the exact remote preflight can be tested without SSH.
REMOTE_PREFLIGHT = r'''
import hashlib
import math
import os
import socket
import sys
from pathlib import Path


def validate_configuration(config):
    if not isinstance(config, dict):
        raise ValueError("标定配置不是映射")
    interfaces = config.get("motor_interface")
    counts = config.get("motor_num")
    if (not isinstance(interfaces, list) or len(interfaces) != 4
            or set(interfaces) != {"can0", "can1", "can2", "can3"}
            or counts != EXPECTED_COUNTS
            or config.get("motor_interface_type") != ["canfd"] * 4
            or config.get("motor_type") != ["LRO"] * 4):
        raise ValueError("配置必须是已审查的%d关节、四路CANFD LRO配置" % DOF)
    for field in ("motor_id", "motor_model", "motor_zero_offset"):
        values = config.get(field)
        if not isinstance(values, list) or len(values) != DOF:
            raise ValueError(field + "必须包含%d项" % DOF)
    ids = config["motor_id"]
    if any(type(value) is not int or not 1 <= value <= 8 for value in ids):
        raise ValueError("电机编号无效")
    if any(type(value) is not int or value < 0 for value in config["motor_model"]):
        raise ValueError("电机型号无效")
    if any(type(value) not in (int, float) or not math.isfinite(value)
           for value in config["motor_zero_offset"]):
        raise ValueError("电机偏移无效")
    if type(config.get("master_id_offset")) is not int:
        raise ValueError("主机编号偏移无效")
    start = 0
    for count in counts:
        if len(set(ids[start:start + count])) != count:
            raise ValueError("同一CAN总线存在重复电机编号")
        start += count


def process_conflict(executable, arguments):
    if Path(executable.removesuffix(" (deleted)")).name == "inference_node":
        return True
    names = [Path(arg).name for arg in arguments]
    if any(name in {"inference_node", "set_zero.py", "read_joint_angles.py"}
           for name in names):
        return True
    return any(arguments[index:index + 2] == ["launch", "roboparty_inference"]
               for index in range(len(arguments) - 1))


def check_processes(proc=Path("/proc")):
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            if not raw:
                continue  # Kernel threads and zombies have no command line.
            arguments = [arg.decode("utf-8", "replace") for arg in raw.split(b"\0") if arg]
            executable = os.readlink(entry / "exe")
        except FileNotFoundError:
            continue  # The process exited during this snapshot.
        except OSError as error:
            raise ValueError("无法确认进程状态，拒绝标定：PID " + entry.name) from error
        if process_conflict(executable, arguments):
            raise ValueError("推理、运控启动或其他电机工具仍在运行：PID " + entry.name)


def check_files():
    for filename, expected in ((SCRIPT, SCRIPT_SHA256), (CONFIG, CONFIG_SHA256)):
        path = Path(filename)
        if str(path.resolve()) != filename or not path.is_file():
            raise ValueError("标定文件路径与审查版本不符")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("标定脚本或配置已变化，请先重新审查：" + filename)
    import yaml  # Parse data only; never import motors_py during preflight.
    validate_configuration(yaml.safe_load(Path(CONFIG).read_text(encoding="utf-8")))
    driver = (Path(ROOT) / "install/roboparty_motors/lib"
              / ("python%d.%d" % sys.version_info[:2]) / "site-packages")
    if not any(driver.glob("motors_py*.so")):
        raise ValueError("当前Python版本的已编译电机驱动不存在；不会自动编译")
    if not all((Path("/sys/class/net") / name).exists()
               for name in ("can0", "can1", "can2", "can3")):
        raise ValueError("四路CAN接口不完整；不会自动配置或启用接口")


def check_runtime():
    root = Path(ROOT).resolve(strict=True)
    for relative, expected in DRIVER_SOURCE_SHA256.items():
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("LRO驱动源码与已核对版本不符，请先重新审查：" + relative)
    # A pure-stdlib verifier copied from this computer; importing the motor SDK
    # is deliberately not part of checking paths, source hashes or build receipts.
    runtime = {"__name__": "rp1_calibration_runtime_check"}
    exec(compile(RUNTIME_CHECK_SOURCE, "reviewed_runtime_verifier", "exec"), runtime)
    result = runtime["verify_runtime_receipt"](root, RUNTIME_RECEIPT)
    if result.get("status") != "code_built":
        raise ValueError("驱动尚无匹配当前源码的构建记录，请先完成独立构建：" +
                         "；".join(result.get("reasons", [])))


def main():
    if not sys.stdin.isatty() or os.geteuid() != 0:
        raise ValueError("需要独立SSH交互终端和sudo授权，以检查全部进程")
    check_files()
    check_processes()
    check_runtime()
    print("\n目标标签：" + ROBOT_ID + "；远端主机：" + socket.gethostname(), flush=True)
    print("上位机核对的%d关节LRO配置：" % DOF + CONFIG, flush=True)
    print("配置及脚本哈希与上位机核对版本一致，不代表已核验实际电机接线和摆位。", flush=True)
    print("驱动源码、安装产物和构建记录一致；尚未据此验证实际固件量程或硬件反馈。", flush=True)
    print("这会使能电机、发送MIT阻尼控制指令并按人工确认写入硬件零位。", flush=True)
    print("脚本的EVO/RP分支会发送8槽批量MIT指令；禁止更换配置直接复用。", flush=True)
    print("请现场确认正确机器人、机械支撑、接线与摆位；推理和其他电机工具须保持关闭。", flush=True)
    print("没有远端共用互斥锁，预检后也禁止从其他终端启动运控。", flush=True)
    print("SSH断开或一般异常不能保证自动失能恢复；标零后需人工重新上下电并复查。", flush=True)
    print("上位机不自动停止服务、不重新上电，也不把退出码当作标定通过。", flush=True)
    answer = input("输入 " + CONFIRMATION + " 打开现有交互标定工具；其他输入取消：")
    if answer != CONFIRMATION:
        print("已取消，未启动电机标定。", flush=True)
        return
    check_files()
    check_processes()
    check_runtime()
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, "-B", SCRIPT, "--config", CONFIG])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, ImportError) as error:
        print("拒绝启动标定：" + str(error), flush=True)
        if sys.stdin.isatty():
            input("按Enter结束预检终端。")
        raise SystemExit(1)
'''


def remote_calibration_script(robot_id, dof=24):
    if type(dof) is not int or dof not in (24, 28):
        raise ValueError("仅支持24或28关节标定配置")
    if (not isinstance(robot_id, str) or not robot_id.strip() or len(robot_id) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in robot_id)):
        raise ValueError("机器人编号无效")
    constants = {
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
        "RUNTIME_CHECK_SOURCE": Path(__file__).with_name("calibration_runtime.py").read_text(encoding="utf-8"),
    }
    return "\n".join(f"{name} = {json.dumps(value, ensure_ascii=True)}"
                     for name, value in constants.items()) + "\n" + REMOTE_PREFLIGHT


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
        "/usr/bin/python3", "-B", "-c", script,
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
