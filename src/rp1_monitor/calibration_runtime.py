"""Read-only build receipts; --build explicitly rebuilds code, never opens hardware.

This file uses only the standard library and can be copied and run with python -B.
The caller must source ROS first. A receipt attests code files, not hardware readiness.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import uuid
from datetime import datetime, timezone
from pathlib import Path

COLCON_COMMAND = ["colcon", "build", "--symlink-install", "--packages-up-to",
                  "roboparty_inference", "--executor", "sequential", "--parallel-workers", "1"]
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".cmake",
                   ".in", ".py", ".yaml", ".yml", ".json", ".xml", ".sh", ".txt", ".toml",
                   ".tgz", ".gz", ".zip", ".a", ".so"}
WRITER_NAMES = {"inference_node", "roslaunch", "set_zero.py", "read_joint_angles.py",
                "realtime_run.py", "motor_ota.py", "motors_py_example.py", "cansend", "cangen"}


def default_receipt_path():
    return Path.home() / ".local/state/rp1-monitor/calibration/runtime-build.json"


def python_abi():
    return {"version": list(sys.version_info[:2]), "cache_tag": sys.implementation.cache_tag,
            "soabi": sysconfig.get_config_var("SOABI"), "machine": platform.machine()}


def _inside(root, path):
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("文件缺失或路径越出deploy目录：" + str(path))
    return resolved


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(root):
    """Re-enumerate tracked and nonignored new build inputs, including submodules."""
    root = Path(root).resolve(strict=True)
    files = set()
    visited = set()

    def repository(repo):
        if repo in visited:
            raise ValueError("源码子模块路径循环或重复")
        visited.add(repo)
        # Scope ownership trust to this read-only invocation, also when sudo verifies.
        base = ["git", "-c", "safe.directory=" + str(repo), "-c", "core.fsmonitor=false",
                "-C", str(repo), "ls-files"]
        staged = subprocess.run(base + ["--stage", "-z"], check=True, capture_output=True).stdout
        for record in staged.split(b"\0"):
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            mode, _, stage = metadata.split()
            if stage != b"0":
                raise ValueError("源码存在未解决的Git合并冲突")
            path = repo / os.fsdecode(name)
            if mode == b"160000":
                # Only inference dependencies are needed; camera/AP are separate products.
                if path.relative_to(root).parts[:2] in (("src", "motors"), ("src", "imu"), ("src", "inference")):
                    nested = path.resolve(strict=True)
                    if not nested.is_relative_to(root) or not (nested / ".git").exists():
                        raise ValueError("必需子模块未初始化或路径越界：" + str(path))
                    repository(nested)
            else:
                files.add(path)
        new = subprocess.run(base + ["--others", "--exclude-standard", "-z"],
                             check=True, capture_output=True).stdout
        files.update(repo / os.fsdecode(name) for name in new.split(b"\0") if name)

    repository(root)
    result = {}
    for path in sorted(files):
        if path.relative_to(root).parts[0] in {"build", "install", "log"}:
            continue  # Colcon outputs are audited separately, never source inputs.
        if path.suffix.lower() in SOURCE_SUFFIXES or path.name in {"Makefile", ".gitmodules", ".gitignore"}:
            result[path.relative_to(root).as_posix()] = _sha256(_inside(root, path))
    if not all("src/" + package + "/CMakeLists.txt" in result for package in ("motors", "imu", "inference")):
        raise ValueError("缺少motors/imu/inference编译源码，不能建立fingerprint")
    return result


def artifact_inventory(root):
    root = Path(root).resolve(strict=True)
    version = "python%d.%d" % sys.version_info[:2]
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not suffix:
        raise ValueError("无法确认当前Python扩展ABI")
    paths = {
        "motors_py": root / "install/roboparty_motors/lib" / version / "site-packages" / ("motors_py" + suffix),
        "robot_py": root / "install/roboparty_inference/lib" / version / "site-packages" / ("robot_py" + suffix),
        "inference_node": root / "install/roboparty_inference/lib/roboparty_inference/inference_node",
    }
    return {name: {"path": str(_inside(root, path)), "sha256": _sha256(_inside(root, path))}
            for name, path in paths.items()}


def check_processes(proc=Path("/proc")):
    for entry in Path(proc).iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            if not raw:
                continue  # Kernel threads and zombies have no command line.
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue  # It exited during the snapshot.
        except OSError as error:
            raise ValueError("无法读取进程状态，拒绝构建：PID " + entry.name) from error
        args = [os.fsdecode(arg) for arg in raw.split(b"\0") if arg]
        names = [comm] + [Path(arg.removesuffix(" (deleted)")).name for arg in args]
        if any(name in WRITER_NAMES for name in names) or any(
            names[index:index + 2] == ["ros2", "launch"] for index in range(len(names) - 1)
        ):
            raise ValueError("运控、标零或总线写入工具仍活跃：PID " + entry.name)
        try:
            executable = Path(os.readlink(entry / "exe").removesuffix(" (deleted)")).name
        except FileNotFoundError:
            continue
        except PermissionError as error:
            if len(names) < 2 or any(name in {"main", "a.out", "unknown", ""} for name in names[:2]):
                raise ValueError("无法确认含糊进程的实际程序：PID " + entry.name) from error
            continue  # Unrelated cmdline/comm suffice for protected system processes.
        except OSError as error:
            raise ValueError("无法读取进程状态，拒绝构建：PID " + entry.name) from error
        if executable in WRITER_NAMES:
            raise ValueError("运控、标零或总线写入工具仍活跃：PID " + entry.name)


def _build_environment(root):
    env = os.environ.copy()
    if env.get("ROS_VERSION") != "2" or not env.get("ROS_DISTRO") or not env.get("AMENT_PREFIX_PATH"):
        raise ValueError("ROS 2环境未准备好；请先source现有ROS环境，不会自动安装或启动服务")
    prefixes = [Path(path) for path in env["AMENT_PREFIX_PATH"].split(os.pathsep) if path]
    if not all(any((prefix / "share" / package / "package.xml").is_file() for prefix in prefixes)
               for package in ("ament_cmake", "rclcpp")):
        raise ValueError("已source的ROS路径缺少ament_cmake/rclcpp，构建环境未准备好")
    if any(shutil.which(tool) is None for tool in ("git", "colcon", "taskset", "nice", "cmake", "ccache")):
        raise ValueError("缺少现有构建工具，不会自动安装依赖")
    arch = {"aarch64": "aarch64", "x86_64": "x64"}.get(platform.machine())
    thirdparty = root / "src/inference/thirdparty"
    if arch is None or not all((thirdparty / name).is_file() for name in (
        "onnxruntime-linux-" + str(arch) + "-1.21.0.tgz", "yaml-cpp-0.9.0.tar.gz", "cnpy/CMakeLists.txt"
    )):
        raise ValueError("本地第三方源码/归档未准备好，不允许CMake回退下载依赖")
    env.update(MAKEFLAGS="-j1", CMAKE_BUILD_PARALLEL_LEVEL="1")
    return env


def verify_runtime_receipt(root, receipt_path=None):
    """Read-only remote gate: no build, hardware import, service or CAN access."""
    report = {"status": "needs_build", "hardware_ready": False, "reasons": []}
    try:
        root = Path(root).resolve(strict=True)
        receipt = json.loads(Path(receipt_path or default_receipt_path()).read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
                or receipt["schema_version"] != 1 or receipt.get("build_succeeded") is not True
                or receipt.get("root") != str(root) or receipt.get("python_abi") != python_abi()
                or receipt.get("command", [])[-len(COLCON_COMMAND):] != COLCON_COMMAND):
            raise ValueError("receipt的schema、root、Python ABI或构建命令不匹配")
        if receipt.get("source_sha256") != source_fingerprint(root):
            raise ValueError("源码集合或内容已变化，需要重新构建")
        if receipt.get("artifacts") != artifact_inventory(root):
            raise ValueError("已安装二进制路径或内容已变化，需要重新构建")
        report["status"] = "code_built"
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
        report["reasons"].append(str(error))
    return report


def build_runtime(root):
    root = Path(root).resolve(strict=True)
    if os.geteuid() == 0:
        raise ValueError("拒绝root构建，避免污染源码和安装产物权限；请使用deploy所有者构建")
    env = _build_environment(root)
    cpus = sorted(os.sched_getaffinity(0) - {0})
    if not cpus:
        raise ValueError("没有可用的非CPU0核心，拒绝构建")
    check_processes()
    before = source_fingerprint(root)
    command = ["taskset", "-c", ",".join(map(str, cpus)), "nice", "-n", "10"] + COLCON_COMMAND
    subprocess.run(command, cwd=root, env=env, check=True)
    check_processes()
    artifacts = artifact_inventory(root)
    if source_fingerprint(root) != before:
        raise ValueError("构建期间源码发生变化，拒绝生成receipt")
    receipt = {"schema_version": 1, "root": str(root), "python_abi": python_abi(),
               "source_sha256": before, "artifacts": artifacts, "command": command,
               "timestamp_utc": datetime.now(timezone.utc).isoformat(), "build_succeeded": True,
               "hardware_ready": False}
    active = default_receipt_path()
    active.parent.mkdir(parents=True, exist_ok=True)
    history = active.with_name("runtime-build-" + uuid.uuid4().hex + ".json")
    data = json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with history.open("x", encoding="utf-8") as stream:
        stream.write(data)
    temporary = active.with_name(".runtime-build-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(temporary, active)
    finally:
        temporary.unlink(missing_ok=True)
    return {**receipt, "status": "code_built", "receipt_path": str(history), "active_receipt_path": str(active)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--build", action="store_true", help="显式构建代码；不启动或操作机器人")
    args = parser.parse_args(argv)
    try:
        if args.build:
            result = build_runtime(args.root)
        else:
            check_processes()
            result = verify_runtime_receipt(args.root)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result = {"status": "blocked", "hardware_ready": False, "reasons": [str(error)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "code_built" or result.get("build_succeeded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
