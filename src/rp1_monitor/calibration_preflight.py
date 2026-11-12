"""Robot-side interactive preflight, loaded by the verified SSH launcher."""

import hashlib
import math
import os
import socket
import subprocess
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
    return any(names[index:index + 2] in (["launch", "roboparty_inference"], ["colcon", "build"])
               for index in range(len(names) - 1))


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
            raise ValueError("推理、运控、电机工具或 colcon 构建仍在运行：PID " + entry.name)


def check_files(*, require_driver=True):
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
    if require_driver and not any(driver.glob("motors_py*.so")):
        raise ValueError("当前 Python 版本的已编译电机驱动不存在")
    if not all((Path("/sys/class/net") / name).exists()
               for name in ("can0", "can1", "can2", "can3")):
        raise ValueError("四路CAN接口不完整；不会自动配置或启用接口")


def check_runtime(*, build_if_needed=False):
    root = Path(ROOT).resolve(strict=True)
    for relative, expected in DRIVER_SOURCE_SHA256.items():
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("LRO驱动源码与已核对版本不符，请先重新审查：" + relative)
    runtime = {"__name__": "rp1_calibration_runtime_check"}
    exec(compile(RUNTIME_HELPER_BYTES, RUNTIME_HELPER_PATH, "exec"), runtime)
    verify = runtime["verify_runtime_receipt"]
    result = verify(root, RUNTIME_RECEIPT)
    if result["status"] == "needs_build" and build_if_needed:
        print("正在核对已有 colcon 构建。需要补编译时请保持终端打开。", flush=True)
        subprocess.run([
            "/usr/bin/sudo", "-H", "-u", "robo", "--", "/bin/bash", "--noprofile", "--norc", "-c",
            'source /opt/ros/jazzy/setup.bash && exec /usr/bin/python3 -B "$1" --root "$2" --prepare',
            "rp1-build", RUNTIME_HELPER_PATH, str(root),
        ], check=True)
        result = verify(root, RUNTIME_RECEIPT)
    if result["status"] != "code_built":
        raise ValueError("驱动构建记录核对失败：" + "；".join(result.get("reasons", [])))


def main():
    if not sys.stdin.isatty() or os.geteuid() != 0:
        raise ValueError("需要独立SSH交互终端和sudo授权，以检查全部进程")
    check_files(require_driver=False)
    check_processes()
    check_runtime(build_if_needed=True)
    check_files()
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
    except (OSError, ValueError, ImportError, subprocess.CalledProcessError) as error:
        print("拒绝启动标定：" + str(error), flush=True)
        if sys.stdin.isatty():
            input("按Enter结束预检终端。")
        raise SystemExit(1)
