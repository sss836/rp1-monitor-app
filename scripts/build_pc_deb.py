#!/usr/bin/env python3
"""Build an Ubuntu 24.04 amd64 PC package without installing it or starting services."""

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "rp1-monitor-app"
DEFAULT_VERSION = "0.4.4"
APP_ROOT = "/opt/rp1-monitor-app"
EXTRAS = "desktop,server,station"
SOURCE_IGNORE = shutil.ignore_patterns(
    "build", "dist", "*.egg-info", "__pycache__", "*.pyc", ".venv", "venv", ".git",
    ".pytest_cache", ".ruff_cache", ".pip-cache", ".tmp", "runtime", "state", "credentials", ".secrets",
    ".env", ".env.*", "*.sqlite*", "*.db", "*.dump", "*.log", "*.pem", "*.key",
    "known_hosts", "devices.json", "profile.json", "observer-config.json", "*-token.*",
    "observer.pid", "observer.lock",
)
DEPENDENCIES = (
    "python3 (>= 3.12)", "python3 (<< 3.13)", "python3-venv", "ca-certificates",
    "libgl1", "libegl1", "libopengl0", "libglib2.0-0t64", "libdbus-1-3", "libfontconfig1",
    "libxkbcommon0", "libxkbcommon-x11-0", "libxcb1", "libxcb-cursor0", "libxcb-icccm4",
    "libxcb-keysyms1", "libxcb-image0", "libxcb-render-util0", "libxcb-shape0",
    "libxcb-xinerama0", "libxcb-xkb1", "libxcb-dri3-0", "libnss3", "libnspr4",
    "libgbm1", "libdrm2", "libasound2t64", "libx11-6", "libxcomposite1", "libxdamage1",
    "libxext6", "libxfixes3", "libxrandr2", "libxtst6", "libxkbfile1", "fonts-noto-cjk",
)

LAUNCHER = r'''#!/bin/sh
set -eu
mode=${1:-desktop}
if [ "$#" -gt 0 ]; then shift; fi
case "$mode" in
    desktop|factory|web) ;;
    *) echo '用法：rp1-monitor-app [desktop|factory|web]' >&2; exit 2 ;;
esac
if [ "$#" -ne 0 ]; then
    echo '用法：rp1-monitor-app [desktop|factory|web]' >&2
    exit 2
fi
umask 077
monitor_home=${RP1_MONITOR_HOME:-${XDG_DATA_HOME:-"$HOME/.local/share"}/rp1-monitor-app}
mkdir -p "$monitor_home"
cd "$monitor_home"
case "$mode" in
    desktop)
        exec env -u PYTHONPATH -u PYTHONHOME /opt/rp1-monitor-app/venv/bin/python \
            -m rp1_monitor.pc_cli desktop --quick-connect \
            --factory-dir "$monitor_home/factory" --station-dir "$monitor_home/station"
        ;;
    factory)
        exec env -u PYTHONPATH -u PYTHONHOME /opt/rp1-monitor-app/venv/bin/python \
            -m rp1_monitor.pc_cli desktop --factory \
            --factory-dir "$monitor_home/factory" --station-dir "$monitor_home/station"
        ;;
    web)
        exec env -u PYTHONPATH -u PYTHONHOME /opt/rp1-monitor-app/venv/bin/python \
            -m rp1_monitor.pc_cli station --state-dir "$monitor_home/station" \
            --port 18980 --background --open-browser
        ;;
esac
'''

POSTINST = r'''#!/bin/sh
set -eu
unset PYTHONPATH PYTHONHOME
if [ "${1:-}" = configure ]; then
    app_root=/opt/rp1-monitor-app
    export PIP_CONFIG_FILE=/dev/null PIP_FIND_LINKS= PIP_NO_INDEX=1
    python3 -m venv "$app_root/venv"
    set -- "$app_root"/wheelhouse/rp1_monitor_pc-*.whl
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
        echo '缺少唯一的电脑端应用wheel，未启动应用或服务。' >&2
        exit 1
    fi
    "$app_root/venv/bin/python" -m pip install --no-index \
        --find-links "$app_root/wheelhouse" --disable-pip-version-check \
        --no-compile --force-reinstall "$1[desktop,server,station]"
fi
'''

POSTRM = r'''#!/bin/sh
set -eu
case "${1:-}" in
    remove|purge)
        # Only package-generated dependencies; user data lives outside /opt.
        rm -rf -- /opt/rp1-monitor-app/venv
        rmdir -- /opt/rp1-monitor-app 2>/dev/null || true
        ;;
esac
'''


def write_file(path, contents, *, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)


def check_host():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("Build on Ubuntu 24.04 amd64; ARM64 robot environments are not supported")
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Build with Python 3.12 to match the target PC dependency wheels")
    release = Path("/etc/os-release").read_text()
    if not re.search(r"^ID=ubuntu$", release, re.M) or not re.search(r'^VERSION_ID="24\.04"$', release, re.M):
        raise ValueError("This package targets Ubuntu 24.04; use a matching build environment")
    if shutil.which("dpkg-deb") is None:
        raise ValueError("dpkg-deb is required for building the package")


def run(command):
    subprocess.run(command, check=True)


def copy_pc_source(source, destination):
    source = Path(source).resolve()
    project = tomllib.loads((source / "pyproject.toml").read_text())
    if project.get("project", {}).get("name") != "rp1-monitor-pc":
        raise ValueError("Source must be an exported rp1-monitor-pc project, not the unified development or robot package")
    if destination.resolve().is_relative_to(source):
        raise ValueError("Choose an output directory outside the supplied PC source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=SOURCE_IGNORE, symlinks=True)
    if any(path.is_symlink() for path in destination.rglob("*")):
        raise ValueError("PC source contains a symlink; use a clean regular-file export")
    if (destination / "src/rp1_robot").exists() or (destination / "src/rp1_monitor/agent.py").exists():
        raise ValueError("PC source includes a robot collector; use the PC role exporter")


def prepare_source_and_wheels(output, wheelhouse=None, source_project=None, collector_bundle=None):
    """Export only the PC role, then resolve all binary dependencies into a new directory."""
    source = output / "source/upper-computer"
    role_builder = ROOT / "scripts/build_role_packages.py"
    if source_project is not None:
        copy_pc_source(source_project, source)
    elif role_builder.is_file():
        run([
            sys.executable, str(role_builder), "--role", "pc",
            "--output", str(output / "source"), "--archive",
        ])
    else:
        # The public PC repository is already role-separated and has no role exporter.
        copy_pc_source(ROOT, source)
    if collector_bundle is not None:
        target_bundle = source / "src/rp1_monitor/data/collector-bundle.zip"
        target_bundle.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(collector_bundle, target_bundle)
    wheels = output / "application-wheel"
    wheels.mkdir()
    run([
        sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps",
        "--no-build-isolation", "--wheel-dir", str(wheels), str(source),
    ])
    application_wheels = list(wheels.glob("rp1_monitor_pc-*.whl"))
    if len(application_wheels) != 1:
        raise ValueError("Expected exactly one newly built rp1-monitor-pc wheel")
    resolved = output / "wheelhouse"
    resolved.mkdir()
    command = [
        sys.executable, "-m", "pip", "download", "--only-binary=:all:",
        "--dest", str(resolved), "--disable-pip-version-check",
    ]
    if wheelhouse is not None:
        wheelhouse = Path(wheelhouse).resolve()
        if not wheelhouse.is_dir():
            raise ValueError("--wheelhouse must name an existing directory of dependency wheels")
        command.extend(["--no-index", "--find-links", str(wheelhouse)])
    command.append(f"{application_wheels[0]}[{EXTRAS}]")
    run(command)
    return source, resolved


def prepare_package(source, wheelhouse, stage, version):
    """Create the package filesystem; this does not execute any maintainer script."""
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", version):
        raise ValueError("Invalid Debian version")
    stage.mkdir()
    app = stage / APP_ROOT.lstrip("/")
    app.mkdir(parents=True)
    # pip wheel may create build/ and *.egg-info in the exported project.
    shutil.copytree(source, app / "source", ignore=SOURCE_IGNORE)
    destination = app / "wheelhouse"
    destination.mkdir()
    wheel_files = sorted(Path(wheelhouse).iterdir())
    if not wheel_files or any(path.suffix != ".whl" or not path.is_file() or path.is_symlink() for path in wheel_files):
        raise ValueError("Resolved wheelhouse must contain only regular .whl files")
    if len([path for path in wheel_files if path.name.startswith("rp1_monitor_pc-")]) != 1:
        raise ValueError("Resolved wheelhouse must contain exactly one PC application wheel")
    for wheel in wheel_files:
        shutil.copyfile(wheel, destination / wheel.name)

    documentation = stage / "usr/share/doc" / PACKAGE
    shutil.copytree(source / "docs", documentation)
    shutil.copyfile(source / "README.md", documentation / "README.md")
    shutil.copyfile(ROOT / "docs/PC_DEB.md", documentation / "PC_DEB.md")
    icon = stage / "usr/share/pixmaps/rp1-monitor-app.png"
    icon.parent.mkdir(parents=True)
    shutil.copyfile(source / "src/rp1_monitor/web/brand/robot.png", icon)
    write_file(stage / "usr/bin/rp1-monitor-app", LAUNCHER, executable=True)
    for suffix, name, arguments in (
        ("", "RP1 整机监控", ""), ("-factory", "RP1 工厂测试", " factory"),
    ):
        write_file(stage / f"usr/share/applications/rp1-monitor-app{suffix}.desktop", (
            "[Desktop Entry]\nType=Application\nVersion=1.0\n"
            f"Name={name}\nExec=/usr/bin/rp1-monitor-app{arguments}\n"
            "TryExec=/usr/bin/rp1-monitor-app\nIcon=rp1-monitor-app\n"
            "Terminal=false\nCategories=Utility;\nStartupNotify=true\n"
        ))
    write_file(stage / "DEBIAN/postinst", POSTINST, executable=True)
    write_file(stage / "DEBIAN/postrm", POSTRM, executable=True)
    size = (sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()) + 1023) // 1024
    write_file(stage / "DEBIAN/control", (
        f"Package: {PACKAGE}\nVersion: {version}\nSection: utils\nPriority: optional\n"
        f"Architecture: amd64\nInstalled-Size: {size}\n"
        "Maintainer: RP1 Monitor Maintainers\n"
        f"Depends: {', '.join(DEPENDENCIES)}\n"
        "Description: RP1 PC monitor and editable factory result summaries\n"
        " PC-only Qt desktop, local factory summaries, relay and read-only web interface.\n"
        " Python dependencies install from the bundled wheels without network access.\n"
        " Installation does not start services, connect robots or modify user test data.\n"
    ))
    # Normalize copied permissions; ownership is normalized by dpkg-deb below.
    for path in stage.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path not in {stage / "usr/bin/rp1-monitor-app", stage / "DEBIAN/postinst", stage / "DEBIAN/postrm"}:
            path.chmod(0o644)
    return stage


def collector_bundle_for_build(output, *, source=None, supplied=None, wheelhouse=None):
    """Every production PC package ships an installable ARM64 collector payload."""
    candidate = Path(supplied) if supplied else (Path(source) if source else ROOT) / "src/rp1_monitor/data/collector-bundle.zip"
    if not candidate.is_file():
        if supplied:
            raise ValueError("--collector-bundle does not name an existing bundle")
        script = ROOT / "scripts/build_collector_bundle.py"
        if not script.is_file():
            raise ValueError("PC source lacks its collector bundle; provide --collector-bundle")
        candidate = output / "collector-bundle.zip"
        command = [sys.executable, str(script), "--output", str(candidate)]
        if wheelhouse is not None:
            command += ["--wheelhouse", str(wheelhouse)]
        run(command)
    if candidate.is_symlink() or not 0 < candidate.stat().st_size <= 128 * 1024 * 1024:
        raise ValueError("Invalid collector bundle size or symlink")
    with zipfile.ZipFile(candidate) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if not (manifest.get("schema") == 1 and manifest.get("package") == "rp1-monitor-robot"
                and manifest.get("python") == [3, 12] and manifest.get("machine") == "aarch64"):
            raise ValueError("Collector bundle must target Python 3.12 ARM64")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("Collector bundle file manifest is missing")
        if set(archive.namelist()) != {*files, "manifest.json"}:
            raise ValueError("Collector bundle files differ from manifest")
        if sum(item.file_size for item in archive.infolist()) > 256 * 1024 * 1024:
            raise ValueError("Collector bundle expanded size exceeds limit")
        for name, digest in files.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("Collector bundle file checksum mismatch")
    return candidate


def build(output, *, version=DEFAULT_VERSION, wheelhouse=None, source=None, collector_bundle=None, collector_wheelhouse=None):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new directory. No existing package was replaced.")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", version):
        raise ValueError("Invalid Debian version")
    check_host()
    output = output.resolve()
    output.mkdir(parents=True)
    robot_bundle = collector_bundle_for_build(output, source=source, supplied=collector_bundle, wheelhouse=collector_wheelhouse)
    pc_source, resolved = prepare_source_and_wheels(output, wheelhouse, source, robot_bundle)
    stage = prepare_package(pc_source, resolved, output / "package", version)
    target = output / f"{PACKAGE}_{version}_amd64.deb"
    run(["dpkg-deb", "--root-owner-group", "-Zxz", "--threads-max=2", "--build", str(stage), str(target)])
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    write_file(output / "SHA256SUMS", f"{digest}  {target.name}\n")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory; never overwritten")
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--source", type=Path, help="Already exported rp1-monitor-pc source; copied cleanly before wheel building")
    parser.add_argument("--wheelhouse", type=Path, help="Existing complete dependency wheels; disables package-index access")
    parser.add_argument("--collector-bundle", type=Path, help="Verified Python 3.12 ARM64 collector ZIP to include")
    parser.add_argument("--collector-wheelhouse", type=Path, help="Offline ARM64 wheel cache used only when building the collector ZIP")
    args = parser.parse_args()
    try:
        target = build(args.output, version=args.version, wheelhouse=args.wheelhouse, source=args.source,
                       collector_bundle=args.collector_bundle, collector_wheelhouse=args.collector_wheelhouse)
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile, KeyError) as error:
        parser.exit(1, f"Build failed: {error}\nNew partial output, if any, has been kept for inspection.\n")
    print(target)
    print(target.parent / "SHA256SUMS")


if __name__ == "__main__":
    main()
