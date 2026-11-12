"""Verify, reuse or build deploy artifacts without opening hardware.

This file uses only the standard library and is installed with the collector.
The caller must source ROS first. A receipt attests code files, not hardware readiness.
"""

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

COLCON_COMMAND = ["colcon", "build", "--symlink-install", "--packages-up-to",
                  "roboparty_inference", "--executor", "sequential", "--parallel-workers", "1"]
BUILD_PACKAGES = {"roboparty_imu": "imu", "roboparty_motors": "motors", "roboparty_inference": "inference"}
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".cmake",
                   ".in", ".py", ".yaml", ".yml", ".json", ".xml", ".sh", ".txt", ".toml",
                   ".tgz", ".gz", ".zip", ".a", ".so"}
WRITER_NAMES = {"inference_node", "roslaunch", "set_zero.py", "read_joint_angles.py",
                "realtime_run.py", "motor_ota.py", "motors_py_example.py", "cansend", "cangen"}


def default_receipt_path():
    return Path.home() / ".local/state/rp1-monitor/calibration/runtime-build.json"


@contextmanager
def _build_lock(inherited_fd=None):
    directory = default_receipt_path().parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "build.lock"
    owned = inherited_fd is None
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600) if owned else inherited_fd
    try:
        info, expected = os.fstat(descriptor), path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)):
            raise ValueError("构建锁文件与当前用户或路径不匹配")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("机器人正在构建或更新 deploy，请等当前操作完成后再试") from error
        yield
    finally:
        if owned:
            os.close(descriptor)


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


def _artifact_paths(root):
    version = "python%d.%d" % sys.version_info[:2]
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not suffix:
        raise ValueError("无法确认当前Python扩展ABI")
    return {
        "motors_py": root / "install/roboparty_motors/lib" / version / "site-packages" / ("motors_py" + suffix),
        "robot_py": root / "install/roboparty_inference/lib" / version / "site-packages" / ("robot_py" + suffix),
        "inference_node": root / "install/roboparty_inference/lib/roboparty_inference/inference_node",
    }


def artifact_inventory(root):
    root = Path(root).resolve(strict=True)
    return {name: {"path": str(_inside(root, path)), "sha256": _sha256(_inside(root, path))}
            for name, path in _artifact_paths(root).items()}


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
            names[index:index + 2] in (["ros2", "launch"], ["colcon", "build"])
            for index in range(len(names) - 1)
        ):
            raise ValueError("运控、标零、总线写入工具或 colcon 构建仍活跃：PID " + entry.name)
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
                or receipt.get("root") != str(root) or receipt.get("python_abi") != python_abi()):
            raise ValueError("构建记录格式、路径或 Python ABI 不匹配")
        if receipt.get("origin") == "terminal_colcon":
            log = receipt.get("colcon_log")
            if not isinstance(log, str) or not re.fullmatch(r"log/build_[^/]+/events\.log", log):
                raise ValueError("终端构建记录缺少 colcon 日志来源")
        elif receipt.get("command", [])[-len(COLCON_COMMAND):] != COLCON_COMMAND:
            raise ValueError("构建记录命令不匹配")
        old_sources = receipt.get("source_sha256")
        if not isinstance(old_sources, dict):
            raise ValueError("构建记录缺少有效源码清单")
        current_sources = source_fingerprint(root)
        if old_sources != current_sources:
            added = sorted(current_sources.keys() - old_sources.keys())
            removed = sorted(old_sources.keys() - current_sources.keys())
            changed = sorted(key for key in current_sources.keys() & old_sources.keys()
                             if current_sources[key] != old_sources[key])
            report["reasons"].append(
                f"源码集合或内容已变化：新增{len(added)}项、修改{len(changed)}项、"
                f"删除{len(removed)}项，需要审查并重新构建"
            )
        try:
            previous_artifacts = receipt.get("artifacts")
            current_artifacts = artifact_inventory(root)
            if previous_artifacts != current_artifacts:
                if isinstance(previous_artifacts, dict):
                    names = sorted(name for name in current_artifacts
                                   if current_artifacts[name] != previous_artifacts.get(name))
                    detail = "、".join(names) if names else "产物清单"
                else:
                    detail = "产物清单"
                report["reasons"].append(f"已安装二进制路径或内容已变化：{detail}，需要重新构建")
        except (OSError, ValueError) as error:
            report["reasons"].append("已安装二进制检查未完成：" + str(error))
        if not report["reasons"]:
            report["status"] = "code_built"
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
        report["reasons"].append(str(error))
    return report


def _colcon_evidence(root, sources):
    latest = root / "log/latest_build"
    if not latest.is_symlink():
        raise ValueError("缺少 colcon 最新构建记录")
    start = latest.lstat().st_ctime_ns
    directory = latest.resolve(strict=True)
    if directory.parent != root / "log" or not directory.name.startswith("build_"):
        raise ValueError("colcon 最新构建目录不在当前 deploy 内")
    events = _inside(root, directory / "events.log")
    end = events.stat().st_mtime_ns
    jobs, shutdown = set(), False
    with events.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            shutdown = " (-) EventReactorShutdown: {}" in line
            job = re.fullmatch(r"\[[^\]]+\] \(([^)]+)\) JobEnded: \{'identifier': '([^']+)', 'rc': (-?\d+)\}\s*", line)
            if job:
                package, identifier, code = job.groups()
                if package != identifier or int(code) != 0 or package in jobs:
                    raise ValueError("最近一次 colcon 构建存在失败或重复任务")
                jobs.add(package)
    if not shutdown or end < start or not BUILD_PACKAGES.keys() <= jobs:
        raise ValueError("最近一次 colcon 未完成全部标定依赖包")

    source_time = max(_inside(root, root / name).stat().st_ctime_ns for name in sources)
    if source_time > start:
        raise ValueError("源码在这次 colcon 构建开始后发生过变化")
    manifests = {}
    for package, directory_name in BUILD_PACKAGES.items():
        build = root / "build" / package
        install = root / "install" / package
        rc = _inside(root, build / "colcon_build.rc")
        if rc.read_text().strip() != "0" or not start <= rc.stat().st_ctime_ns <= end:
            raise ValueError(package + " 缺少本次成功构建状态")
        cache = set(_inside(root, build / "CMakeCache.txt").read_text().splitlines())
        required = {f"CMAKE_HOME_DIRECTORY:INTERNAL={root / 'src' / directory_name}",
                    f"CMAKE_INSTALL_PREFIX:PATH={install}"}
        if not required <= cache:
            raise ValueError(package + " 的 CMake 源码或安装目录与当前 deploy 不一致")
        if package == "roboparty_inference":
            for dependency in ("roboparty_motors", "roboparty_imu"):
                prefix = dependency + "_DIR:PATH="
                value = next((line[len(prefix):] for line in cache if line.startswith(prefix)), "")
                path = Path(value)
                if not path.is_absolute() or not path.is_dir() or not path.resolve().is_relative_to(root / "install" / dependency):
                    raise ValueError("推理包未链接当前工作区的 " + dependency)
        manifests[package] = set()
        for filename in ("install_manifest.txt", "symlink_install_manifest.txt"):
            path = build / filename
            if path.is_file():
                manifests[package].update(_inside(root, path).read_text().splitlines())
    for path in _artifact_paths(root).values():
        package = path.relative_to(root).parts[1]
        if str(path) not in manifests[package]:
            raise ValueError("colcon 安装清单缺少 " + path.name)
        for artifact in (path, root / "build" / package / path.name):
            stamp = _inside(root, artifact).stat().st_ctime_ns
            if not source_time <= stamp <= end:
                raise ValueError("产物时间与当前源码或成功构建不一致：" + artifact.name)
    if latest.resolve(strict=True) != directory or latest.lstat().st_ctime_ns != start or events.stat().st_mtime_ns != end:
        raise ValueError("核对期间 colcon 构建记录发生变化")
    return events.relative_to(root).as_posix()


def prepare_runtime(root):
    if os.geteuid() == 0:
        raise ValueError("请使用 deploy 所有者准备标定构建")
    root = Path(root).resolve(strict=True)
    with _build_lock():
        check_processes()
        existing = verify_runtime_receipt(root)
        if existing["status"] == "code_built":
            return existing
        try:
            sources = source_fingerprint(root)
            artifacts = artifact_inventory(root)
            colcon_log = _colcon_evidence(root, sources)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(f"现有终端构建无法复用：{error}。正在补编译。", flush=True)
            return _build_runtime(root)
        check_processes()
        if source_fingerprint(root) != sources or artifact_inventory(root) != artifacts:
            raise ValueError("核对期间源码或编译产物发生变化，请重新检查")
        print("已核对机器人终端的 colcon 构建，直接复用编译产物。", flush=True)
        return _publish_receipt(root, sources, artifacts, origin="terminal_colcon", colcon_log=colcon_log)


def build_runtime(root, *, lock_fd=None):
    if os.geteuid() == 0:
        raise ValueError("拒绝root构建，避免污染源码和安装产物权限；请使用deploy所有者构建")
    root = Path(root).resolve(strict=True)
    with _build_lock(lock_fd):
        return _build_runtime(root)


def _build_runtime(root):
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
    return _publish_receipt(root, before, artifacts, command=command)


def _publish_receipt(root, sources, artifacts, **provenance):
    receipt = {"schema_version": 1, "root": str(root), "python_abi": python_abi(),
               "source_sha256": sources, "artifacts": artifacts, **provenance,
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--build", action="store_true", help="显式构建代码；不启动或操作机器人")
    mode.add_argument("--prepare", action="store_true", help="优先复用现有 colcon 构建，需要时补编译")
    parser.add_argument("--lock-fd", type=int, help="复用 deploy 更新命令持有的构建锁")
    args = parser.parse_args(argv)
    if args.lock_fd is not None and (not args.build or args.lock_fd < 3):
        parser.error("--lock-fd 仅与 --build 一起使用")
    try:
        if args.build:
            result = build_runtime(args.root, lock_fd=args.lock_fd)
        elif args.prepare:
            result = prepare_runtime(args.root)
        else:
            check_processes()
            result = verify_runtime_receipt(args.root)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result = {"status": "blocked", "hardware_ready": False, "reasons": [str(error)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "code_built" or result.get("build_succeeded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
