"""Ordinary-user lifecycle manager, transported over SSH and executed in memory.

Only the marked collector directory belongs to this manager. No motor, deploy,
TUI installation, system service or privileged command is performed here.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
import uuid
import venv
import zipfile
from pathlib import Path, PurePosixPath

PRODUCT = "rp1-monitor-robot"
MANAGED = ".local/share/rp1-collector-managed"
UPLOAD = ".cache/rp1-collector-upload"
MARKER = ".rp1-collector-owner.json"
MAX_BUNDLE = 256 * 1024**2
CONFIG_KEYS = {"robot_id", "ros_setup", "dds_profile", "rmw_implementation",
               "fastdds_xml", "ros_domain_id", "tui_executor"}
SCRIPTS = {"scripts/start-robot.sh", "scripts/start-robot-background.sh",
           "scripts/observer-fastdds-profile.xml"}


def _json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise ValueError("采集端元信息缺失、不安全或过大")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("采集端元信息必须是对象")
    return value


def _write(path, value):
    if path.is_symlink():
        raise ValueError("采集端元信息不能是符号链接")
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _plain_parents(path, home):
    """Never follow a user-facing path through a symlink into another tree."""
    path.relative_to(home)
    for item in (path, *path.parents):
        if item == home:
            break
        if item.is_symlink():
            raise ValueError("采集端管理路径包含符号链接，拒绝操作")
    return path


def _owned(root):
    value = _json(root / MARKER)
    if value.get("product") != PRODUCT or value.get("schema") != 1:
        raise ValueError("目录没有本采集端的归属标记，拒绝操作")
    if root.stat().st_uid != os.getuid():
        raise ValueError("采集端目录不属于当前 SSH 用户")
    return value


def _current(root):
    info = _json(root / "installation.json")
    name = info.get("release")
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}", name):
        raise ValueError("当前采集端版本目录无效")
    release = _plain_parents(root / "releases" / name, root)
    if not release.is_dir():
        raise ValueError("当前采集端版本目录不存在")
    return info, release


def _process(root):
    token_path = root / "process.json"
    if not token_path.exists():
        return None
    token = _json(token_path)
    pid = token.get("pid")
    if type(pid) is not int or pid <= 1:
        raise ValueError("采集端 PID 记录无效")
    try:
        process = Path(f"/proc/{pid}")
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z" or fields[19] != token.get("start_time"):
            return None
        if process.stat().st_uid != os.getuid():
            raise ValueError("采集端 PID 属于其他用户")
        _, release = _current(root)
        args = (process / "cmdline").read_bytes().split(b"\0")
        if not any(args):
            return None  # Linux clears cmdline while a process is exiting.
        allowed = {str(release / "start-observer.sh").encode(),
                   str(release / ".venv/bin/python").encode()}
        if not allowed.intersection(args) or Path(os.readlink(process / "cwd")) != release:
            raise ValueError("采集端 PID 与本安装不匹配，拒绝停止其他进程")
        return token
    except (FileNotFoundError, ProcessLookupError):
        return None


def _port_open():
    try:
        with socket.create_connection(("127.0.0.1", 8891), timeout=.4):
            return True
    except ConnectionRefusedError:
        return False


def _owns_listener(pid):
    listening = set()
    try:
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            for line in Path(table).read_text().splitlines()[1:]:
                fields = line.split()
                if (len(fields) >= 10 and fields[1].rsplit(":", 1)[-1] == "22BB"
                        and fields[3] == "0A"):
                    listening.add("socket:[" + fields[9] + "]")
        for descriptor in Path(f"/proc/{pid}/fd").iterdir():
            try:
                if os.readlink(descriptor) in listening:
                    return True
            except OSError:
                pass
    except OSError:
        pass
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _api(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open("http://127.0.0.1:8891" + path, timeout=1) as response:
        raw = response.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2 or response.status != 200:
            raise ValueError("采集端接口返回无效")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("采集端接口格式无效")
        return value


def _identity(info):
    frame = _api("/api/v1/state")
    if frame.get("robot_id") != info["robot_id"] or frame.get("source_kind") != "ros2":
        raise ValueError("8891接口身份与本采集端不符")
    return frame


def _inactive(info):
    _identity(info)
    value = _api("/api/v1/factory/segments")
    if "active_segment_id" not in value:
        raise ValueError("无法确认采集端记录状态")
    if value["active_segment_id"]:
        raise ValueError("机器人仍有活动测试记录，请在上位机结束记录后再停止或卸载")


def _stop(root, *, check_active=True):
    token = _process(root)
    if token is None:
        return
    info, _ = _current(root)
    if check_active:
        _inactive(info)
    pid = token["pid"]
    # Recheck kernel starttime and argv immediately before every signal.
    if _process(root) == token:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if _process(root) is None:
            (root / "process.json").unlink(missing_ok=True)
            return
        time.sleep(.1)
    if _process(root) == token:
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _process(root) is not None:
        time.sleep(.1)
    if _process(root) is not None:
        raise ValueError("本采集端进程尚未退出，未删除安装或数据")
    (root / "process.json").unlink(missing_ok=True)


def _upload_directory(home, robot_id):
    path = _plain_parents(home / UPLOAD, home)
    if not path.is_dir():
        return None
    try:
        owner = _owned(path)
        if owner.get("robot_id") != robot_id:
            return None
        return path
    except (OSError, ValueError):
        return None


def _legacy(home, robot_id=None):
    """Recognise only independently-installed old observers with matching identity."""
    base = _plain_parents(home / ".local/share/rp1-monitor", home)
    if not base.is_dir():
        return []
    result = []
    for path in sorted(base.iterdir()):
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
            continue
        try:
            installation = _json(path / "installation.json")
            config = _json(path / "observer-config.json")
            identity = installation.get("robot_id")
            verified = installation.get("verified", {})
            if (installation.get("standalone_dependencies") is not True
                    or not identity or identity != config.get("robot_id")
                    or robot_id is not None and identity != robot_id
                    or verified.get("package") != "0.4.0"):
                continue
            module = Path(verified.get("module", ""))
            dependencies = Path(installation.get("dependency_directory", ""))
            if (not module.is_absolute() or not module.is_relative_to(path / ".venv")
                    or module.name != "__init__.py" or module.parent.name != "rp1_robot"
                    or module.is_symlink() or not module.is_file()
                    or not dependencies.is_relative_to(path)
                    or not dependencies.is_dir() or not (path / "profile.json").is_file()):
                continue
            _plain_parents(module.parent, path)
            _plain_parents(dependencies, path)
            result.append({"path": str(path), "robot_id": identity,
                           "version": verified["package"], "running": _legacy_running(path)})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return result


def _legacy_running(path):
    prefix = str(path).encode() + b"/"
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            args = (process / "cmdline").read_bytes().split(b"\0")
            if any(arg.startswith(prefix) for arg in args):
                return True
        except (OSError, ValueError):
            pass
    return False


def _status(root):
    if not root.exists():
        return {"installed": False, "running": False, "root": str(root), "delete_paths": []}
    _owned(root)
    marker = _owned(root)
    if not (root / "installation.json").exists():
        return {"installed": False, "incomplete": True, "running": False,
                "root": str(root), "robot_id": marker.get("robot_id"),
                "delete_paths": [str(root)], "log_path": str(root / "installation.log")}
    info, release = _current(root)
    process = _process(root)
    return {"installed": True, "running": process is not None,
            "version": info["version"], "robot_id": info["robot_id"],
            "bundle_sha256": info.get("bundle_sha256"),
            "root": str(root), "release_path": str(release),
            "log_path": str(root / "observer.log"),
            "delete_paths": [str(root)], "pid": process["pid"] if process else None,
            "cpu_affinity": [1, 2, 3, 7], "port": 8891}


def _bundle(home, request):
    name = request.get("bundle_id", "")
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}", name):
        raise ValueError("上传包编号必须是 UUID 十六进制字符串")
    path = _plain_parents(home / UPLOAD / (name + ".zip"), home)
    expected = request.get("bundle_sha256", "")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("缺少采集包 SHA256")
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BUNDLE:
        raise ValueError("未找到已上传的独立采集包或大小超限")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("上传包 SHA256 不匹配，未安装")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 2000 or sum(item.file_size for item in members) > 512 * 1024**2:
            raise ValueError("采集包解压大小超限")
        names = set()
        for item in members:
            relative = PurePosixPath(item.filename)
            safe = (item.filename == "manifest.json" or item.filename in SCRIPTS
                    or len(relative.parts) == 2 and relative.parts[0] == "wheelhouse"
                    and re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", relative.name))
            if (not safe or item.filename in names or item.is_dir()
                    or relative.is_absolute() or ".." in relative.parts
                    or "\\" in item.filename or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError("采集包包含重复、越界或不允许的文件")
            names.add(item.filename)
        if "manifest.json" not in names:
            raise ValueError("采集包缺少 manifest.json")
        manifest = json.loads(archive.read("manifest.json"))
        if (manifest.get("schema") != 1 or manifest.get("package") != PRODUCT
                or manifest.get("python") != list(sys.version_info[:2])
                or manifest.get("machine") != platform.machine()):
            raise ValueError("采集包产品、Python 或处理器架构与机器人不匹配")
        version = manifest.get("version", "")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,79}", version):
            raise ValueError("采集包版本无效")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != names - {"manifest.json"}:
            raise ValueError("采集包文件清单不完整")
        for name, expected in files.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError("采集包内容校验失败")
        if not {"scripts/start-robot.sh", "scripts/observer-fastdds-profile.xml"} <= names:
            raise ValueError("采集包缺少启动脚本")
        if not any(n.startswith("wheelhouse/rp1_monitor_robot-") for n in names):
            raise ValueError("采集包缺少机器人 wheel")
        if not any(n.startswith("wheelhouse/pip-") for n in names):
            raise ValueError("采集包缺少离线 pip 引导 wheel")
    return path, manifest


def _install(root, home, request, *, update):
    if update:
        _owned(root)
        if _process(root):
            raise ValueError("请先停止本采集端，再执行更新")
        info, previous = _current(root)
        robot_id = info["robot_id"]
    else:
        if root.exists():
            raise ValueError("采集端目录已存在，请先核对或选择更新")
        previous = None
        robot_id = request.get("robot_id", "")
        if not isinstance(robot_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", robot_id):
            raise ValueError("请填写机器人编号")
    legacy = _legacy(home, robot_id) if not update else []
    if any(item["running"] for item in legacy):
        raise ValueError("检测到该机器人旧采集端仍在运行，请先手动停止旧采集端")
    if not previous and len(legacy) > 1:
        raise ValueError("发现多份同身份旧安装，请先核对后保留一份再安装")
    if legacy:
        previous = Path(legacy[0]["path"])
    bundle, manifest = _bundle(home, request)
    ros_setup = request.get("ros_setup", "/opt/ros/jazzy/setup.bash")
    if previous:
        config = _json(previous / "observer-config.json")
        if (set(config) - CONFIG_KEYS or config.get("robot_id") != robot_id
                or not all(isinstance(value, str) for value in config.values())):
            raise ValueError("旧采集端配置含未知字段或身份无效，未安装")
        for key in CONFIG_KEYS:
            config.setdefault(key, "")
        ros_setup = config["ros_setup"]
    elif ros_setup not in {"/opt/ros/jazzy/setup.bash", "/opt/ros/humble/setup.bash"}:
        raise ValueError("请选择机器人已安装的 Jazzy 或 Humble")
    if not Path(ros_setup).is_file():
        raise ValueError("机器人未安装所选 ROS 环境，未修改系统软件")
    if not update:
        root.mkdir(mode=0o700)
        _write(root / MARKER, {"schema": 1, "product": PRODUCT, "robot_id": robot_id})
    release = root / "releases" / uuid.uuid4().hex
    release.mkdir(parents=True, mode=0o700)
    try:
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(release)
        venv.EnvBuilder(system_site_packages=True, with_pip=False).create(release / ".venv")
        python = release / ".venv/bin/python"
        wheels = release / "wheelhouse"
        pip_wheels = list(wheels.glob("pip-*.whl"))
        if len(pip_wheels) != 1:
            raise ValueError("采集包必须包含唯一的离线 pip 引导 wheel")
        bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('pip',run_name='__main__')"
        command = [str(python), "-I", "-B", "-c", bootstrap, str(pip_wheels[0]),
                   "install", "--no-index", "--find-links", str(wheels), "--only-binary=:all:",
                   "--ignore-installed", "--disable-pip-version-check", "--no-cache-dir",
                   "--no-compile", PRODUCT + "==" + manifest["version"]]
        with (root / "installation.log").open("ab") as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT,
                           timeout=180, env={**{key: value for key, value in os.environ.items()
                           if not key.startswith("PIP_")}, "PIP_CONFIG_FILE": "/dev/null"})
        verify = """import importlib,sys,pathlib
root=pathlib.Path(sys.argv[1]).resolve()
for name in ('rp1_robot','pydantic','httpx','websockets','fastapi','uvicorn'):
 m=importlib.import_module(name)
 assert pathlib.Path(m.__file__).resolve().is_relative_to(root), name+'依赖来自安装外部'
from rp1_robot.profiles import RobotProfile,profile_json
if not (root.parent/'profile.json').exists():
 (root.parent/'profile.json').write_text(profile_json(RobotProfile(robot_id=sys.argv[2])))
"""
        # Do not import ROS, construct an agent or probe any hardware during installation.
        subprocess.run([str(python), "-I", "-B", "-c", verify, str(release / ".venv"), robot_id],
                       check=True, capture_output=True, timeout=20)
        if previous:
            for name in ("profile.json", "observer-config.json"):
                source = previous / name
                if source.is_symlink() or not source.is_file():
                    raise ValueError("旧版配置不安全，未切换版本")
                shutil.copyfile(source, release / name)
            _write(release / "observer-config-validated.json", config)
            (release / "observer-config-validated.json").replace(release / "observer-config.json")
            check_profile = "from rp1_robot.profiles import RobotProfile;import pathlib,sys;assert RobotProfile.model_validate_json(pathlib.Path(sys.argv[1]).read_text()).robot_id==sys.argv[2], '设备档案身份不符'"
            subprocess.run([str(python), "-I", "-B", "-c", check_profile,
                            str(release / "profile.json"), robot_id], check=True,
                           capture_output=True, timeout=20)
        else:
            config = {"robot_id": robot_id, "ros_setup": ros_setup, "dds_profile": "",
                      "rmw_implementation": "rmw_fastrtps_cpp", "fastdds_xml": "1",
                      "ros_domain_id": "", "tui_executor": ""}
            _write(release / "observer-config.json", config)
        _plain_parents(root / "state", root)
        (root / "state").mkdir(mode=0o700, exist_ok=True)
        (release / "state").symlink_to(root / "state", target_is_directory=True)
        shutil.copyfile(release / "scripts/start-robot.sh", release / "start-observer.sh")
        shutil.copyfile(release / "scripts/observer-fastdds-profile.xml", release / "observer-fastdds-profile.xml")
        for name in ("start-observer.sh", "observer-config.json", "profile.json"):
            (release / name).chmod(0o700 if name.endswith(".sh") else 0o600)
        _write(root / "installation.json", {"release": release.name, "version": manifest["version"],
               "robot_id": robot_id, "bundle_sha256": request["bundle_sha256"]})
        # The pointer is committed. Cleanup cannot turn a successful install into
        # rollback of its now-current release if the upload directory is read-only.
        with contextlib.suppress(OSError):
            bundle.unlink()
    except BaseException:
        # Remove only the newly-created release; existing data and current pointer remain intact.
        shutil.rmtree(release)
        raise
    return _status(root)


def _start(root):
    _owned(root)
    info, release = _current(root)
    existing = _process(root)
    if existing:
        _identity(info)
        if not _owns_listener(existing["pid"]):
            raise ValueError("8891 监听并非本采集进程持有，未认定已启动")
        return _status(root)
    if _port_open():
        raise ValueError("8891 已有其他进程监听，请先核对旧采集端；未停止或覆盖原进程")
    cpus = set(range(os.cpu_count() or 1))
    if not {1, 2, 3, 7} <= cpus:
        raise ValueError("机器人缺少约定的 CPU 1,2,3,7，未启动采集")
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with (root / "observer.log").open("ab") as log:
        child = subprocess.Popen(["bash", str(release / "start-observer.sh")], cwd=release,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True, env=env)
    fields = Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()
    _write(root / "process.json", {"pid": child.pid, "start_time": fields[19]})
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ValueError("采集端启动后退出，请查看 observer.log")
            try:
                _identity(info)
                token = _process(root)
                if token and _owns_listener(token["pid"]):
                    actual = os.sched_getaffinity(token["pid"])
                    if actual != {1, 2, 3, 7}:
                        raise RuntimeError("采集进程 CPU 亲和不符合约定，已回收新进程")
                    return _status(root)
            except (OSError, ValueError):
                pass
            time.sleep(.25)
        raise ValueError("25 秒内未确认采集接口就绪，已回收本次新启动进程，请查看 observer.log")
    except BaseException:
        _stop(root, check_active=False)
        raise


def _operate(request, *, _home=None):
    """Return a result dictionary; failures raise and never run arbitrary shell input."""
    if not isinstance(request, dict):
        raise ValueError("采集端管理请求必须是对象")
    if os.geteuid() == 0:
        raise ValueError("请使用机器人普通 SSH 用户，不能使用 root 或 sudo")
    home = Path(_home) if _home is not None else Path.home()
    home = home.resolve(strict=True)
    root = _plain_parents(home / MANAGED, home)
    action = request.get("action")
    if action not in {"status", "install", "update", "start", "stop", "uninstall"}:
        raise ValueError("不支持的采集端操作")
    selected = request.get("robot_id")
    if selected is not None and (not isinstance(selected, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", selected)):
        raise ValueError("机器人编号无效")
    if action != "status" and not selected:
        raise ValueError("采集端操作必须绑定机器人编号")
    if root.exists():
        owner = _owned(root)
        identity = owner.get("robot_id")
        if (root / "installation.json").exists():
            identity = _current(root)[0]["robot_id"]
        if selected and identity != selected:
            raise ValueError("所选机器人编号与已安装采集端不一致，拒绝操作")
        for relative in ("state", "releases"):
            _plain_parents(root / relative, root)
    if action == "status":
        report = _status(root)
        identity = report.get("robot_id") or request.get("robot_id")
        report["legacy_installations"] = _legacy(home, identity)
        report["delete_paths"] += [item["path"] for item in report["legacy_installations"]]
        upload = _upload_directory(home, identity)
        if upload:
            report["delete_paths"].append(str(upload))
        return report
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.parent / ".rp1-collector-managed.lock"
    if lock.is_symlink():
        raise ValueError("采集端管理锁路径不安全")
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("另一个采集端管理操作正在进行，请稍后刷新状态") from exc
        if action in {"install", "update"}:
            return _install(root, home, request, update=action == "update")
        if action in {"start", "stop"}:
            _owned(root)
            if action == "start":
                return _start(root)
            _stop(root, check_active=False)
            return _status(root)
        if request.get("confirm_delete") is not True:
            raise ValueError("卸载将删除本采集端程序、配置、缓存、日志和测试数据，请先确认删除")
        info = _owned(root) if root.exists() else {}
        identity = info.get("robot_id") or request.get("robot_id")
        if not identity:
            raise ValueError("卸载必须绑定机器人编号，未删除任何目录")
        if root.exists():
            if (root / "installation.json").exists():
                current, _ = _current(root)
                identity = current["robot_id"]
                _stop(root)
        legacy = _legacy(home, identity)
        if any(item["running"] for item in legacy):
            raise ValueError("旧采集端仍在运行，请先手动停止；未删除安装或测试数据")
        upload = _upload_directory(home, identity)
        targets = ([root] if root.exists() else []) + [Path(item["path"]) for item in legacy]
        if upload:
            targets.append(upload)
        # Hold each old launcher's own lock to prevent a simultaneous manual start.
        with contextlib.ExitStack() as locks:
            for item in legacy:
                old = Path(item["path"])
                launch_lock = old / "observer.lock"
                if launch_lock.is_symlink():
                    raise ValueError("旧采集端启动锁不安全，未删除")
                handle = locks.enter_context(launch_lock.open("a"))
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError("旧采集端正在使用，未删除") from exc
            if any(_legacy_running(Path(item["path"])) for item in legacy):
                raise ValueError("旧采集端刚刚启动，未删除")
            for target in targets:
                _plain_parents(target, home)
                if target == root:
                    _owned(root)
                elif target == upload:
                    if _upload_directory(home, identity) != upload:
                        raise ValueError("上传缓存归属发生变化，未删除")
                elif not any(item["path"] == str(target) for item in _legacy(home, identity)):
                    raise ValueError("旧安装归属发生变化，未删除")
                shutil.rmtree(target)
        return {"installed": False, "running": False, "root": str(root),
                "deleted_paths": [str(path) for path in targets], "delete_paths": []}


def main(request, *, _home=None):
    report = _operate(request, _home=_home)
    home = (Path(_home) if _home is not None else Path.home()).resolve(strict=True)
    identity = report.get("robot_id") or request.get("robot_id")
    legacy = _legacy(home, identity)
    identities = {item["robot_id"] for item in legacy}
    if not identity and len(identities) == 1:
        identity = identities.pop()
        report["robot_id"] = identity
    report["legacy_installations"] = legacy
    if "deleted_paths" not in report:
        paths = list(report.get("delete_paths", []))
        paths += [item["path"] for item in legacy]
        upload = _upload_directory(home, identity)
        if upload:
            paths.append(str(upload))
        report["delete_paths"] = list(dict.fromkeys(paths))
    report["managed_root"] = report["root"]
    report["deletion_paths"] = report.get("delete_paths", [])
    # A single inert coordination lock remains outside the removable installation.
    report["coordination_lock"] = str(Path(report["root"]).parent / ".rp1-collector-managed.lock")
    return report
