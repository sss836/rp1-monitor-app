"""Inspect PC packaging without building a deb, downloading wheels or installing it."""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("tomllib", reason="deb builder requires Python 3.12")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pc_deb_builder", ROOT / "scripts/build_pc_deb.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def file_at(root, name, content="fixture"):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


@pytest.fixture
def pc_source(tmp_path):
    root = tmp_path / "pc-source"
    file_at(root, "pyproject.toml", '[project]\nname = "rp1-monitor-pc"\nversion = "0.4.0"\n')
    file_at(root, "src/rp1_monitor/__init__.py", "")
    file_at(root, "src/rp1_monitor/pc_cli.py", "# PC CLI fixture\n")
    file_at(root, "src/rp1_monitor/desktop_result_summary.py", "# editable summary fixture\n")
    file_at(root, "src/rp1_monitor/data/factory-report-rules.json", "{}")
    file_at(root, "src/rp1_monitor/web/brand/robot.png", "icon fixture")
    file_at(root, "docs/templates/RP1_测试记录标准模板_v1.csv", "17-column-template-fixture")
    file_at(root, "docs/FACTORY_RESULT_SUMMARY.md", "summary docs")
    file_at(root, "README.md", "PC README")
    return root


@pytest.fixture
def wheelhouse(tmp_path):
    root = tmp_path / "wheelhouse"
    file_at(root, "rp1_monitor_pc-0.4.0-py3-none-any.whl", "new PC wheel fixture")
    file_at(root, "PySide6-6.12.0-cp39-abi3-manylinux_2_34_x86_64.whl", "dependency fixture")
    return root


def test_package_tree_has_only_pc_source_bundled_wheels_docs_and_launchers(pc_source, wheelhouse, tmp_path):
    for name in (
        "runtime/factory/real-result.csv", "state/credentials.json", ".venv/bin/python", ".git/config",
        "build/duplicated.py", "src/rp1_monitor_pc.egg-info/PKG-INFO", "observer.log", "capture.sqlite",
    ):
        file_at(pc_source, name, "do not package this fixture")
    stage = builder.prepare_package(pc_source, wheelhouse, tmp_path / "package", builder.DEFAULT_VERSION)
    app = stage / "opt/rp1-monitor-app"
    assert (app / "source/src/rp1_monitor/desktop_result_summary.py").is_file()
    assert (app / "source/src/rp1_monitor/data/factory-report-rules.json").is_file()
    assert not (app / "source/src/rp1_robot").exists()
    assert not (app / "venv").exists()
    assert len(list((app / "wheelhouse").glob("*.whl"))) == 2
    names = {path.relative_to(stage).as_posix() for path in stage.rglob("*")}
    assert not any(part in {"runtime", "state", ".venv", ".git", "build", "__pycache__"} for name in names for part in Path(name).parts)
    assert not any(name.endswith((".sqlite", ".log")) or ".egg-info" in name for name in names)
    assert (stage / "usr/share/doc/rp1-monitor-app/templates/RP1_测试记录标准模板_v1.csv").is_file()
    assert (stage / "usr/share/doc/rp1-monitor-app/PC_DEB.md").is_file()
    control = (stage / "DEBIAN/control").read_text()
    assert "Package: rp1-monitor-app\n" in control
    assert "Architecture: amd64\n" in control
    assert "Version: 0.4.1+platform.20261106.2\n" in control
    assert "python3 (>= 3.12), python3 (<< 3.13), python3-venv" in control
    assert "libxcb-cursor0" in control and "libnss3" in control
    for path in ("usr/bin/rp1-monitor-app", "DEBIAN/postinst", "DEBIAN/postrm"):
        assert (stage / path).stat().st_mode & 0o777 == 0o755
    assert "Exec=/usr/bin/rp1-monitor-app\n" in (stage / "usr/share/applications/rp1-monitor-app.desktop").read_text()
    assert "Exec=/usr/bin/rp1-monitor-app factory\n" in (stage / "usr/share/applications/rp1-monitor-app-factory.desktop").read_text()


@pytest.mark.parametrize("mode,command,flag", [
    (None, "desktop", "--quick-connect"), ("factory", "desktop", "--factory"),
    ("web", "station", "--open-browser"),
])
def test_launcher_modes_use_user_data_and_pc_cli_without_installing(tmp_path, mode, command, flag):
    interpreter = file_at(tmp_path, "fake-python", '#!/bin/sh\nprintf "%s\\n" "$@"\n')
    interpreter.chmod(0o755)
    script = file_at(tmp_path, "launch", builder.LAUNCHER.replace("/opt/rp1-monitor-app/venv/bin/python", str(interpreter)))
    environment = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "user-data")}
    environment.pop("RP1_MONITOR_HOME", None)
    result = subprocess.run(["sh", str(script), *([] if mode is None else [mode])], env=environment, capture_output=True, text=True, check=True)
    arguments = result.stdout.splitlines()
    assert arguments[:3] == ["-m", "rp1_monitor.pc_cli", command]
    assert flag in arguments
    data = tmp_path / "user-data/rp1-monitor-app"
    if mode == "web":
        assert arguments[arguments.index("--state-dir") + 1] == str(data / "station")
    else:
        assert arguments[arguments.index("--factory-dir") + 1] == str(data / "factory")
        assert arguments[arguments.index("--station-dir") + 1] == str(data / "station")
    assert data.is_dir()
    assert not any("pip" in arg or "/opt" in arg for arg in arguments)


def test_launcher_explicit_runtime_override_preserves_existing_files(tmp_path):
    interpreter = file_at(tmp_path, "fake-python", '#!/bin/sh\nprintf "%s\\n" "$@"\n')
    interpreter.chmod(0o755)
    script = file_at(tmp_path, "launch", builder.LAUNCHER.replace("/opt/rp1-monitor-app/venv/bin/python", str(interpreter)))
    old = tmp_path / "old runtime"
    keep = file_at(old, "factory/old-result.csv", "keep existing raw fixture")
    result = subprocess.run(["sh", str(script), "factory"], env={**os.environ, "RP1_MONITOR_HOME": str(old)}, capture_output=True, text=True, check=True)
    arguments = result.stdout.splitlines()
    assert arguments[arguments.index("--factory-dir") + 1] == str(old / "factory")
    assert arguments[arguments.index("--station-dir") + 1] == str(old / "station")
    assert keep.read_text() == "keep existing raw fixture"


def test_maintainer_scripts_are_offline_do_not_touch_user_data_or_start_services(tmp_path):
    for name, contents in (("postinst", builder.POSTINST), ("postrm", builder.POSTRM)):
        path = file_at(tmp_path, name, contents)
        subprocess.run(["sh", "-n", str(path)], check=True)
        assert not any(word in contents for word in ("systemctl", "service ", "pkill", "killall", "sudo", "curl", "wget", "apt ", "git ", "http://", "https://"))
        assert "$HOME" not in contents and "XDG" not in contents and "/runtime" not in contents
    assert 'python3 -m venv "$app_root/venv"' in builder.POSTINST
    assert "--no-index" in builder.POSTINST and "--find-links" in builder.POSTINST
    assert "PIP_CONFIG_FILE=/dev/null" in builder.POSTINST
    assert "--force-reinstall" in builder.POSTINST
    assert "rm -rf" not in builder.POSTINST
    assert "remove|purge)" in builder.POSTRM
    assert "rm -rf -- /opt/rp1-monitor-app/venv" in builder.POSTRM


def test_copy_existing_pc_repository_filters_private_files_and_preserves_input(pc_source, tmp_path):
    keep = file_at(pc_source, "runtime/factory/original.csv", "original fixture")
    private = ("devices.json", "profile.json", "observer-config.json", "observer.pid", "observer.lock", ".pip-cache/private.whl", ".tmp/private.csv")
    for name in private:
        file_at(pc_source, name, "private fixture")
    file_at(pc_source, ".git/config", "private repo state fixture")
    target = tmp_path / "clean-source"
    builder.copy_pc_source(pc_source, target)
    assert (target / "src/rp1_monitor/pc_cli.py").is_file()
    assert not (target / "runtime").exists()
    assert not any((target / name).exists() for name in private)
    assert not (target / ".git").exists()
    assert keep.read_text() == "original fixture"


def test_source_rejects_robot_role_and_nested_output(pc_source, tmp_path):
    with pytest.raises(ValueError, match="outside"):
        builder.copy_pc_source(pc_source, pc_source / "new-output")
    file_at(pc_source, "src/rp1_robot/agent.py", "robot fixture")
    with pytest.raises(ValueError, match="robot collector"):
        builder.copy_pc_source(pc_source, tmp_path / "bad-robot-source")
    file_at(pc_source, "pyproject.toml", '[project]\nname = "rp1-monitor-robot"\n')
    with pytest.raises(ValueError, match="exported rp1-monitor-pc"):
        builder.copy_pc_source(pc_source, tmp_path / "wrong-role")


def test_offline_wheel_resolution_prefers_new_application_and_does_not_mutate_source(pc_source, tmp_path, monkeypatch):
    commands = []
    cached = tmp_path / "cached-wheels"
    file_at(cached, "rp1_monitor_pc-0.4.0-py3-none-any.whl", "old application fixture")
    output = tmp_path / "output"
    output.mkdir()

    def fake_run(command):
        commands.append(command)
        if "wheel" in command:
            target = Path(command[command.index("--wheel-dir") + 1])
            file_at(target, "rp1_monitor_pc-0.4.0-py3-none-any.whl", "new application fixture")
        if "download" in command:
            target = Path(command[command.index("--dest") + 1])
            application = Path(command[-1].split("[", 1)[0])
            shutil.copyfile(application, target / application.name)

    monkeypatch.setattr(builder, "run", fake_run)
    source, resolved = builder.prepare_source_and_wheels(output, cached, pc_source)
    assert source != pc_source and (source / "src/rp1_monitor/pc_cli.py").is_file()
    assert len(commands) == 2
    assert all(flag in commands[0] for flag in ("--no-index", "--no-deps", "--no-build-isolation"))
    assert all(flag in commands[1] for flag in ("--only-binary=:all:", "--no-index", "--find-links"))
    assert commands[1][-1].endswith("rp1_monitor_pc-0.4.0-py3-none-any.whl[desktop,server,station]")
    assert str(output / "application-wheel") in commands[1][-1]
    assert (resolved / "rp1_monitor_pc-0.4.0-py3-none-any.whl").read_text() == "new application fixture"
    assert (cached / "rp1_monitor_pc-0.4.0-py3-none-any.whl").read_text() == "old application fixture"


def test_public_pc_repository_build_does_not_require_role_exporter(pc_source, tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(builder, "ROOT", pc_source)

    def fake_run(command):
        commands.append(command)
        if "wheel" in command:
            file_at(Path(command[command.index("--wheel-dir") + 1]), "rp1_monitor_pc-0.4.0-py3-none-any.whl")

    monkeypatch.setattr(builder, "run", fake_run)
    output = tmp_path / "public-build"
    output.mkdir()
    builder.prepare_source_and_wheels(output)
    assert len(commands) == 2
    assert not any("build_role_packages.py" in arg for command in commands for arg in command)
    assert "--no-index" not in commands[1]


def test_build_refuses_to_overwrite_any_existing_output_before_running_commands(tmp_path, monkeypatch):
    output = tmp_path / "existing"
    keep = file_at(output, "keep", "unchanged")
    monkeypatch.setattr(builder, "run", lambda _command: pytest.fail("must not execute build commands"))
    with pytest.raises(ValueError, match="already exists"):
        builder.build(output)
    assert keep.read_text() == "unchanged"
