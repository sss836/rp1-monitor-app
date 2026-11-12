#!/usr/bin/env bash
# Run on the robot as the deployment owner, from the installed collector release.
set -eo pipefail

deploy_root="$HOME/roboparty_deploy"
build_state="$HOME/.local/state/rp1-monitor/calibration"
lock_path="$build_state/build.lock"
receipt="$build_state/runtime-build.json"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
build_tool="$script_dir/calibration_runtime.py"

if [[ $EUID == 0 || ! -f "$build_tool" || -L "$build_tool" ]]; then
  printf '%s\n' '请使用普通机器人账号，并先安装含构建工具的新版采集包。' >&2
  exit 1
fi
if [[ -L "$deploy_root" || ( -e "$deploy_root" && ! -d "$deploy_root" ) ||
      -L "$build_state" || -L "$lock_path" || -L "$receipt" ]]; then
  printf '%s\n' '部署路径或构建记录不安全，已停止更新。' >&2
  exit 1
fi

unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH PYTHONHOME LD_LIBRARY_PATH
source /opt/ros/jazzy/setup.bash
install -d -m 700 -- "$build_state"
exec 9>>"$lock_path"
if ! flock -n 9; then
  printf '%s\n' '已有标定构建或部署更新任务，请等待任务结束。' >&2
  exit 1
fi

update_work_dir=$(mktemp -d -- "$HOME/.rp1-deploy-update.XXXXXXXX")
candidate="$update_work_dir/checkout"
previous="$update_work_dir/previous"
saved_receipt="$update_work_dir/runtime-build.json"
swap_started=0
committed=0

restore() {
  if [[ -e "$previous" || ! -e "$candidate" ]]; then
    if [[ -e "$deploy_root" ]]; then
      mv -T -- "$deploy_root" "$update_work_dir/failed" || return 1
    fi
    if [[ -e "$previous" ]]; then
      mv -T -- "$previous" "$deploy_root" || return 1
    fi
  fi
  if [[ -f "$saved_receipt" ]]; then
    mv -T -- "$saved_receipt" "$receipt" || return 1
  else
    rm -f -- "$receipt" || return 1
  fi
}

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if (( swap_started && ! committed )); then
    if ! restore; then
      printf '自动恢复失败，保留本次工作目录：%s\n' "$update_work_dir" >&2
      exit 1
    fi
    printf '%s\n' '更新未完成，已恢复更新前的部署与构建记录。' >&2
  fi
  if ! rm -rf --one-file-system -- "${update_work_dir:?}"; then
    printf '未能清理本次工作目录：%s\n' "$update_work_dir" >&2
    exit 1
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

git clone --branch main --recurse-submodules \
  https://github.com/Roboparty/roboparty_deploy.git "$candidate"

python3 -B - "$build_tool" <<'PY'
import runpy
import sys

runpy.run_path(sys.argv[1])["check_processes"]()
PY

if [[ -d "$deploy_root" ]]; then
  raw_data=$(find "$deploy_root" -xdev -type f \( -iname '*.bag' -o -iname '*.mcap' -o \
    -iname '*.db3*' -o -iname '*.sqlite*' \) -print -quit)
  if [[ -n "$raw_data" ]]; then
    printf '旧部署含测试数据，请先移出部署目录：%s\n' "$raw_data" >&2
    exit 1
  fi
fi
if [[ -f "$receipt" ]]; then
  cp -p -- "$receipt" "$saved_receipt"
fi

# Colcon records absolute paths. Move the checkout into its final path before building.
swap_started=1
if [[ -d "$deploy_root" ]]; then
  mv -T -- "$deploy_root" "$previous"
fi
mv -T -- "$candidate" "$deploy_root"
python3 -B "$build_tool" --root "$deploy_root" --build --lock-fd 9
python3 -B "$build_tool" --root "$deploy_root"
git -C "$deploy_root" log -1 --oneline
committed=1
