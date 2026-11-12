import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def deployment(tmp_path):
    project = Path(__file__).resolve().parents[1]
    script = tmp_path / "update_deploy.sh"
    ros_setup = tmp_path / "setup.bash"
    ros_setup.write_text("export ROS_DISTRO=jazzy\n")
    source = (project / "scripts/update_deploy.sh").read_text()
    script.write_text(source.replace("/opt/ros/jazzy/setup.bash", str(ros_setup)))

    deploy_root = tmp_path / "roboparty_deploy"
    deploy_root.mkdir()
    (deploy_root / "old-source").touch()
    receipt = tmp_path / ".local/state/rp1-monitor/calibration/runtime-build.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("old receipt")
    (tmp_path / "calibration_runtime.py").write_text('''
import os
import sys
from pathlib import Path

def check_processes():
    if os.environ.get("TEST_PROCESS_BUSY"):
        raise ValueError("robot busy")

if __name__ == "__main__":
    check_processes()
    root = Path(sys.argv[sys.argv.index("--root") + 1])
    assert root == Path.home() / "roboparty_deploy"
    os.fstat(9)
    assert os.environ["ROS_DISTRO"] == "jazzy"
    assert (root / "new-source").is_file()
    receipt = Path.home() / ".local/state/rp1-monitor/calibration/runtime-build.json"
    if "--build" in sys.argv:
        assert sys.argv[sys.argv.index("--lock-fd") + 1] == "9"
        (root / "compiled").touch()
        receipt.write_text("new receipt")
        if os.environ.get("TEST_BUILD_FAILURE"):
            sys.exit(45)
    else:
        assert "--lock-fd" not in sys.argv
        assert (root / "compiled").is_file()
        assert receipt.read_text() == "new receipt"
''')
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text('''#!/usr/bin/env bash
set -e
if [[ $1 == clone ]]; then
  [[ $* == *"--branch main --recurse-submodules https://github.com/Roboparty/roboparty_deploy.git"* ]]
  mkdir -- "${!#}"
  touch -- "${!#}/new-source"
  if [[ ${TEST_CLONE_FAILURE:-} ]]; then exit 42; fi
else
  [[ $1 == -C && -f $2/compiled && $3 == log ]]
  printf '%s\n' 'newcommit Test fixture'
fi
''')
    git.chmod(0o700)

    def run(**scenario):
        env = {**os.environ, **scenario, "HOME": str(tmp_path), "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}
        result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
        assert not list(tmp_path.glob(".rp1-deploy-update.*")), result.stderr
        return result

    return deploy_root, receipt, run


def test_update_builds_final_path_and_removes_old_deploy(deployment):
    root, receipt, run = deployment
    result = run()
    assert result.returncode == 0, result.stderr
    assert {path.name for path in root.iterdir()} == {"compiled", "new-source"}
    assert receipt.read_text() == "new receipt"
    assert "newcommit" in result.stdout


@pytest.mark.parametrize("scenario, status", [
    ({"TEST_CLONE_FAILURE": "1"}, 42), ({"TEST_BUILD_FAILURE": "1"}, 45),
    ({"TEST_PROCESS_BUSY": "1"}, 1),
])
def test_failed_update_preserves_previous_deploy_and_receipt(deployment, scenario, status):
    root, receipt, run = deployment
    result = run(**scenario)
    assert result.returncode == status, result.stderr
    assert {path.name for path in root.iterdir()} == {"old-source"}
    assert receipt.read_text() == "old receipt"


def test_update_stops_if_old_deploy_contains_test_data(deployment):
    root, receipt, run = deployment
    test_data = root / "recording.mcap"
    test_data.write_text("test fixture")
    result = run()
    assert result.returncode == 1
    assert "旧部署含测试数据" in result.stderr
    assert test_data.read_text() == "test fixture"
    assert receipt.read_text() == "old receipt"
