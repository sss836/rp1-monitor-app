"""Explicit commands; demo never connects to existing robot or production services."""

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path


def private_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def demo(args):
    args.runtime.mkdir(parents=True, exist_ok=True)
    config = args.runtime / "device-config.json"
    if config.exists():
        devices = json.loads(config.read_text())
        if any(device["source_kind"] != "simulated" for device in devices):
            raise ValueError("演示目录包含非模拟设备配置，拒绝运行")
        if len(devices) != args.robots:
            raise ValueError("演示目录设备数量与 --robots 不符，请使用新的运行目录")
    else:
        devices = [
            {
                "robot_id": f"DEMO-RP1-{index + 1:02}",
                "name": f"模拟 RP1 {index + 1:02}",
                "source_kind": "simulated",
                "token": secrets.token_urlsafe(32),
            }
            for index in range(args.robots)
        ]
        private_write(config, json.dumps(devices))
    children = []
    logs = []
    try:
        backend_log = (args.runtime / "backend.log").open("a")
        logs.append(backend_log)
        command = [
            sys.executable,
            "-m",
            "rp1_monitor",
            "server",
            "--devices",
            str(config),
            "--database-url",
            args.database_url or f"sqlite:///{(args.runtime / 'monitor.sqlite').resolve()}",
            "--port",
            str(args.port),
            "--demo-read",
        ]
        children.append(subprocess.Popen(command, stdout=backend_log, stderr=subprocess.STDOUT))
        for index, device in enumerate(devices):
            token_path = args.runtime / f"device-{index}-token.txt"
            if not token_path.exists():
                private_write(token_path, device["token"])
            elif token_path.read_text().strip() != device["token"]:
                raise ValueError("演示设备凭据与配置不符，请使用新的运行目录")
            agent_log = (args.runtime / f"agent-{index}.log").open("a")
            logs.append(agent_log)
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "rp1_monitor",
                        "agent",
                        "--source",
                        "simulated",
                        "--robot-id",
                        device["robot_id"],
                        "--state-dir",
                        str(args.runtime / f"agent-{index}"),
                        "--port",
                        str(args.agent_port + index),
                    ],
                    stdout=agent_log,
                    stderr=subprocess.STDOUT,
                )
            )
            relay_log = (args.runtime / f"relay-{index}.log").open("a")
            logs.append(relay_log)
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "rp1_monitor",
                        "relay",
                        "--source",
                        "simulated",
                        "--robot-id",
                        device["robot_id"],
                        "--collector",
                        f"http://127.0.0.1:{args.agent_port + index}",
                        "--backend",
                        f"http://127.0.0.1:{args.port}",
                        "--token-file",
                        str(token_path),
                        "--state-dir",
                        str(args.runtime / f"relay-{index}"),
                        "--port",
                        str(args.relay_port + index),
                    ],
                    stdout=relay_log,
                    stderr=subprocess.STDOUT,
                )
            )
        print(f"模拟大屏：http://127.0.0.1:{args.port}/", flush=True)
        print(f"Qt App：rp1-monitor desktop --url http://127.0.0.1:{args.relay_port}", flush=True)
        print("全部为模拟数据。Ctrl+C 仅停止本次创建的演示进程。", flush=True)
        started = time.monotonic()
        while args.duration <= 0 or time.monotonic() - started < args.duration:
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f"演示进程提前退出，请查看 {args.runtime} 中的日志")
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
        for handle in logs:
            handle.close()


def main(allowed_commands: set[str] | None = None):
    parser = argparse.ArgumentParser(description="RP1整机只读监控")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server")
    server.add_argument("--devices", type=Path, required=True)
    server.add_argument("--database-url", default=os.environ.get("RP1_MONITOR_DATABASE_URL"))
    server.add_argument(
        "--database-url-file", type=Path, help="从私有文件读取数据库连接，不在命令行暴露凭据"
    )
    server.add_argument("--no-initialize", action="store_true")
    server.add_argument("--viewer-token-file", type=Path)
    server.add_argument("--demo-read", action="store_true")
    server.add_argument("--port", type=int, default=8890)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--policy", type=Path, help="中央关键指标、保留预览及监测告警策略JSON")
    server.add_argument("--motor-map", type=Path, help="非秘密的逐机器人关节部位与温度色阶JSON")
    server.add_argument(
        "--admin-config", type=Path, help="私有管理员身份配置；启用数据库持久设备管理"
    )
    server.add_argument(
        "--storage-mounts", type=Path, help="实际数据库/归档卷的显式只读磁盘探测配置"
    )
    server.add_argument(
        "--database-managed", action="store_true", help="设备文件仅补充新登记，不覆盖数据库修改"
    )
    agent = sub.add_parser("agent", help="机器人只读采集与缓存；不连接中央服务")
    agent.add_argument("--source", choices=["simulated", "ros2"], required=True)
    agent.add_argument(
        "--simulated-dof",
        type=int,
        choices=[24, 28],
        help="仅模拟：关节数量；ROS2仍以实际关节反馈为准",
    )
    agent.add_argument(
        "--thermal-demo", action="store_true", help="仅模拟：逐关节温升、超温与降温循环"
    )
    agent.add_argument("--robot-id", required=True)
    agent.add_argument("--state-dir", type=Path, required=True)
    agent.add_argument("--cache-mib", type=int, default=1024)
    agent.add_argument("--reserve-mib", type=int, default=256)
    agent.add_argument(
        "--rp-server-url", help="仅GET读取实机已运行的rp-server；指定后替代BMS日志采集，不启动服务"
    )
    agent.add_argument(
        "--robopi-tui-executor",
        type=Path,
        help="可选：已安装的robopi_TUI plugin_executor.py绝对路径；仅指定被动自检，不自动安装依赖",
    )
    agent.add_argument("--port", type=int, default=8891)
    agent.add_argument(
        "--profile", type=Path, help="经过核对的设备档案JSON；电机诊断须提供身份映射"
    )
    relay = sub.add_parser("relay", help="上位机后台接收、持久缓存和中央上传；独立于Qt窗口")
    relay.add_argument("--robot-id", required=True)
    relay.add_argument("--source", choices=["simulated", "ros2"], required=True)
    relay.add_argument("--collector", required=True, help="网线SSH隧道的本机回环HTTP地址")
    relay.add_argument("--backend", required=True)
    relay.add_argument("--token-file", type=Path, required=True)
    relay.add_argument("--state-dir", type=Path, required=True)
    relay.add_argument("--ca-file")
    relay.add_argument("--cache-mib", type=int, default=1024)
    relay.add_argument("--reserve-mib", type=int, default=256)
    relay.add_argument("--port", type=int, default=8892)
    desktop = sub.add_parser("desktop")
    desktop.add_argument("--url", default="http://127.0.0.1:8892")
    desktop.add_argument("--screenshot", type=Path)
    desktop.add_argument("--quit-after", type=float, default=0)
    desktop.add_argument("--policy", type=Path, help="与中央服务共用的温度限值策略JSON")
    desktop.add_argument("--motor-map", type=Path, help="与网页共用的关节部位与温度色阶JSON")
    desktop.add_argument("--quick-connect", action="store_true", help="启动独立电脑后台，使用SSH地址与密码连接已有监听")
    desktop.add_argument("--station-dir", type=Path, default=Path("runtime/station"))
    desktop.add_argument("--station-port", type=int, default=18980)
    station = sub.add_parser("station", help="电脑快捷连接后台与网页；不在机器人安装或运行命令")
    station.add_argument("--state-dir", type=Path, default=Path("runtime/station"))
    station.add_argument("--port", type=int, default=18980)
    station.add_argument("--background", action="store_true", help="复用或启动独立电脑后台")
    station.add_argument("--open-browser", action="store_true", help="打开本机连接页面")
    demo_parser = sub.add_parser("demo")
    demo_parser.add_argument("--runtime", type=Path, required=True)
    demo_parser.add_argument("--robots", type=int, choices=range(1, 6), default=1)
    demo_parser.add_argument("--database-url")
    demo_parser.add_argument("--port", type=int, default=8890)
    demo_parser.add_argument("--agent-port", type=int, default=18911)
    demo_parser.add_argument("--relay-port", type=int, default=8892)
    demo_parser.add_argument("--duration", type=float, default=0)
    archive = sub.add_parser(
        "archive", help="按UTC小时复制原始摘要及关键指标为gzip归档；不删源数据"
    )
    archive.add_argument("--database-url", default=os.environ.get("RP1_MONITOR_DATABASE_URL"))
    archive.add_argument("--robot-id", required=True)
    archive.add_argument("--start", required=True)
    archive.add_argument("--end", required=True)
    archive.add_argument("--directory", type=Path, required=True)
    replay = sub.add_parser(
        "backfill-key-windows",
        help="显式为旧摘要新增关键投影/容量日账；不覆盖已有投影或改写原始记录",
    )
    replay.add_argument("--database-url", default=os.environ.get("RP1_MONITOR_DATABASE_URL"))
    replay.add_argument("--robot-id", required=True)
    replay.add_argument("--policy", type=Path)
    sub.add_parser("sql")
    admin_init = sub.add_parser(
        "admin-init", help="生成独立管理员登录配置与初始口令，文件仅本人可读"
    )
    admin_init.add_argument("--username", default="admin")
    admin_init.add_argument("--database-url-file", type=Path, required=True)
    admin_init.add_argument("--directory", type=Path, required=True)
    provision = sub.add_parser("provision", help="在新目录生成设备配置和独立只读凭据，不输出秘密")
    provision.add_argument("--directory", type=Path, required=True)
    provision.add_argument("--robot-id", required=True)
    provision.add_argument("--name", required=True)
    provision.add_argument("--source", choices=["simulated", "ros2"], required=True)
    profile_init = sub.add_parser("profile-init", help="生成待核对的设备档案，不探测或初始化硬件")
    profile_init.add_argument("--robot-id", required=True)
    profile_init.add_argument("--output", type=Path, required=True)
    doctor = sub.add_parser("doctor", help="查询只读采集服务的接入自检")
    doctor.add_argument("--url", default="http://127.0.0.1:8892")
    doctor.add_argument("--output", type=Path)
    diagnostics = sub.add_parser("diagnostics", help="保存本地采集服务的诊断记录和证据包")
    diagnostics.add_argument("--url", default="http://127.0.0.1:8892")
    diagnostics.add_argument("--output", type=Path, required=True)
    ask = sub.add_parser("ask", help="只读查询当前机器人，配置模型后可用AI回答")
    ask.add_argument("--server", default="http://127.0.0.1:8890")
    ask.add_argument("--robot-id", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--viewer-token-file", type=Path)
    ask.add_argument("--metric", action="append", default=[])
    ask.add_argument("--start")
    ask.add_argument("--end")
    ask.add_argument("--local-rules", action="store_true")
    if allowed_commands is not None:
        # Remove unsupported commands before parsing so separate distributions
        # reject them normally, without importing modules absent from that role.
        for command in set(sub.choices) - allowed_commands:
            del sub.choices[command]
        sub._choices_actions[:] = [
            action for action in sub._choices_actions if action.dest in allowed_commands
        ]
    args = parser.parse_args()
    if args.command == "server":
        import uvicorn

        from .backend import BackendSettings, create_app

        if args.demo_read and args.host not in {"127.0.0.1", "localhost", "::1"}:
            parser.error("匿名演示只能绑定回环地址")
        database_url = (
            args.database_url_file.read_text().strip()
            if args.database_url_file
            else args.database_url
        )
        if not database_url:
            parser.error("需要 --database-url 或 RP1_MONITOR_DATABASE_URL")
        token = args.viewer_token_file.read_text().strip() if args.viewer_token_file else ""
        app = create_app(
            BackendSettings(
                database_url,
                args.devices,
                token,
                args.demo_read,
                not args.no_initialize,
                policy_path=args.policy,
                admin_config=args.admin_config,
                database_managed=args.database_managed,
                storage_mount_config=args.storage_mounts,
                motor_map_path=args.motor_map,
            )
        )
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    elif args.command == "admin-init":
        from .admin import AdminConfig, password_hash

        args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.directory.stat().st_mode & 0o077:
            parser.error("管理员目录必须仅本人可访问（0700）")
        password = secrets.token_urlsafe(20)
        config = AdminConfig.model_validate(
            {
                "users": [
                    {
                        "username": args.username,
                        "password_hash": password_hash(password),
                        "database_url": args.database_url_file.read_text().strip(),
                    }
                ],
                "device_key_directory": str((args.directory / "device-keys").resolve()),
            }
        )
        data = config.model_dump(mode="json")
        data["users"][0]["password_hash"] = config.users[0].password_hash.get_secret_value()
        data["users"][0]["database_url"] = config.users[0].database_url.get_secret_value()
        private_write(args.directory / "admin.json", json.dumps(data, ensure_ascii=False, indent=2))
        private_write(
            args.directory / "initial-login.txt", f"用户名：{args.username}\n初始密码：{password}\n"
        )
        print(f"私有管理员配置：{args.directory / 'admin.json'}")
        print(f"初始登录文件：{args.directory / 'initial-login.txt'}（不在终端显示口令）")
    elif args.command in {"archive", "backfill-key-windows"}:
        from datetime import datetime

        from pydantic import TypeAdapter

        from .central_data import archive_range, backfill_key_windows
        from .central_policy import load_central_policy
        from .models import RobotId
        from .storage import Store

        robot_id = TypeAdapter(RobotId).validate_python(args.robot_id)
        if not args.database_url:
            parser.error("需要 --database-url 或私有环境变量 RP1_MONITOR_DATABASE_URL")
        store = Store(
            args.database_url,
            initialize=False,
            policy=load_central_policy(getattr(args, "policy", None)),
        )
        try:
            if args.command == "backfill-key-windows":
                print(
                    json.dumps(backfill_key_windows(store, robot_id), ensure_ascii=False, indent=2)
                )
            else:
                print(
                    json.dumps(
                        archive_range(
                            store,
                            robot_id,
                            datetime.fromisoformat(args.start),
                            datetime.fromisoformat(args.end),
                            args.directory,
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        finally:
            store.engine.dispose()
    elif args.command == "agent":
        import uvicorn

        from .agent import AgentSettings, create_agent_app

        settings = AgentSettings(
            robot_id=args.robot_id,
            source_kind=args.source,
            state_dir=args.state_dir,
            cache_bytes=args.cache_mib * 1024**2,
            reserve_bytes=args.reserve_mib * 1024**2,
            rp_server_url=args.rp_server_url,
            profile_path=args.profile,
            thermal_demo=args.thermal_demo,
            simulated_dof=args.simulated_dof,
            robopi_tui_executor=args.robopi_tui_executor,
        )
        uvicorn.run(create_agent_app(settings), host="127.0.0.1", port=args.port, access_log=False)
    elif args.command == "relay":
        import uvicorn

        from .relay import RelaySettings, create_relay_app

        settings = RelaySettings(
            robot_id=args.robot_id,
            source_kind=args.source,
            collector_url=args.collector,
            backend_url=args.backend,
            token=args.token_file.read_text().strip(),
            state_dir=args.state_dir,
            ca_file=args.ca_file,
            cache_bytes=args.cache_mib * 1024**2,
            reserve_bytes=args.reserve_mib * 1024**2,
        )
        uvicorn.run(create_relay_app(settings), host="127.0.0.1", port=args.port, access_log=False)
    elif args.command == "desktop":
        from .desktop import run

        run(args)
    elif args.command == "station":
        from .station import ensure_station, run_station

        if args.background:
            url, _ = ensure_station(args.state_dir, args.port)
            print(f"连接页面：{url}/connect")
            if args.open_browser:
                import webbrowser

                webbrowser.open(url + "/connect")
        else:
            run_station(args.state_dir, args.port)
    elif args.command == "demo":
        demo(args)
    elif args.command == "profile-init":
        from .profiles import RobotProfile, profile_json

        private_write(args.output, profile_json(RobotProfile(robot_id=args.robot_id)))
        print(f"已生成待核对档案：{args.output}")
    elif args.command in {"doctor", "diagnostics", "ask"}:
        from .operations_client import run_cli

        run_cli(args)
    elif args.command == "provision":
        from .models import DeviceRegistration

        device = DeviceRegistration(
            robot_id=args.robot_id,
            name=args.name,
            source_kind=args.source,
            token=secrets.token_urlsafe(32),
        )
        args.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        private_write(
            args.directory / "devices.json",
            json.dumps(
                [
                    {
                        **device.model_dump(exclude={"token"}),
                        "token": device.token.get_secret_value(),
                    }
                ],
                ensure_ascii=False,
                indent=2,
            ),
        )
        private_write(args.directory / "device-token.txt", device.token.get_secret_value() + "\n")
        private_write(args.directory / "viewer-token.txt", secrets.token_urlsafe(32) + "\n")
        print(f"已在 {args.directory} 生成私有凭据文件；设备与只读凭据分开分发，勿提交版本库。")
    else:
        from sqlalchemy import create_mock_engine

        from . import (
            central_tables,  # noqa: F401 - register additive monitoring tables
            report_store,  # noqa: F401 - register diagnostic table
        )
        from .storage import metadata

        engine = None

        def emit(sql, *_args, **_kwargs):
            print(str(sql.compile(dialect=engine.dialect)).strip() + ";\n")

        engine = create_mock_engine("postgresql+psycopg://", emit)
        metadata.create_all(engine)


if __name__ == "__main__":
    main()
