#!/usr/bin/env bash
# Computer only. All installed files stay inside this checkout's .venv/runtime.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$project_root"
mode=${1:-desktop}
case "$mode" in desktop|web|setup) ;; *) echo '用法：./start-monitor.sh [desktop|web|setup]' >&2; exit 2;; esac
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv --without-pip .venv
fi
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  if ! .venv/bin/python -m ensurepip >.venv/bootstrap.log 2>&1; then
    if python3 -m pip --version >/dev/null 2>&1; then
      python3 -m pip --python "$project_root/.venv/bin/python" install pip
    else
      echo '需要python3-venv或可用的python3-pip来准备项目环境；未修改系统。' >&2
      exit 1
    fi
  fi
fi
if ! .venv/bin/python -c 'import sys, rp1_monitor, paramiko, PySide6, pyqtgraph, fastapi, uvicorn, sqlalchemy; from pathlib import Path; sys.exit(Path(rp1_monitor.__file__).resolve().parent != Path(sys.argv[1]).resolve())' "$project_root/src/rp1_monitor" >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e '.[desktop,server,station]'
fi
if [[ "$mode" == setup ]]; then
  echo '电脑运行环境已准备好。运行 ./start-monitor.sh 启动上位机，或 ./start-monitor.sh web 打开网页后台。'
elif [[ "$mode" == web ]]; then
  echo '打开只读数据网页；SSH连接与记录配置请在上位机操作。'
  exec .venv/bin/python -m rp1_monitor station --state-dir "$project_root/runtime/station" --port 18980 --background --open-browser
else
  exec .venv/bin/python -m rp1_monitor desktop --quick-connect --station-dir "$project_root/runtime/station"
fi
