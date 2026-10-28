"""Explicit client operations; bearer credentials never cross unencrypted networks."""

import json
from urllib.parse import urlparse

import httpx


def validate_url(url, token="", *, local=False):
    parsed = urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
    ):
        raise ValueError("地址必须是无凭据、无查询参数的HTTP(S)地址")
    if local and not loopback:
        raise ValueError("采集服务通过本机回环或SSH隧道访问")
    if token and parsed.scheme != "https" and not loopback:
        raise ValueError("只读凭据必须通过HTTPS发送")
    return url.rstrip("/")


def run_cli(args):
    token = (
        args.viewer_token_file.read_text().strip()
        if args.command == "ask" and args.viewer_token_file
        else ""
    )
    url = validate_url(
        args.server if args.command == "ask" else args.url, token, local=args.command != "ask"
    )
    with httpx.Client(
        timeout=100,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    ) as client:
        if args.command == "doctor":
            response = client.get(url + "/api/v1/doctor")
            response.raise_for_status()
            text = json.dumps(response.json(), ensure_ascii=False, indent=2)
            if args.output:
                from .cli import private_write

                private_write(args.output, text + "\n")
            else:
                print(text)
        elif args.command == "diagnostics":
            response = client.post(url + "/api/v1/diagnostics")
            response.raise_for_status()
            report_id = response.json()["report_id"]
            bundle = client.get(url + f"/api/v1/diagnostics/{report_id}/bundle")
            bundle.raise_for_status()
            with args.output.open("xb") as stream:
                stream.write(bundle.content)
            print(f"已导出诊断记录 {report_id}：{args.output}")
        else:
            from pydantic import TypeAdapter

            from .models import RobotId

            robot_id = TypeAdapter(RobotId).validate_python(args.robot_id)
            response = client.post(
                url + f"/api/v1/robot-monitors/{robot_id}/assistant",
                json={
                    "question": args.question,
                    "metrics": args.metric,
                    "start": args.start,
                    "end": args.end,
                    "use_model": not args.local_rules,
                },
            )
            response.raise_for_status()
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
