"""Command line: ``python -m cowmata_engine request.json [-o result.json]`` or ``serve``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cowmata_engine", description="COWMATA 行为识别与综合决策算法引擎")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="执行一个 JSON 请求")
    run.add_argument("request", help="请求 JSON 文件，或 - 表示标准输入")
    run.add_argument("-o", "--output", help="结果 JSON 文件（默认打印到标准输出）")
    serve = sub.add_parser("serve", help="启动本机 HTTP JSON 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    sub.add_parser("info", help="显示引擎版本、操作与特征")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from .api import handle

    if args.command == "serve":
        from .server import serve as run_server

        run_server(args.host, args.port)
        return 0
    if args.command == "run":
        text = sys.stdin.read() if args.request == "-" else Path(args.request).read_text(encoding="utf-8-sig")

        def progress(done, total, message):
            sys.stderr.write(f"{message} {done}/{total}\n")

        response = handle(json.loads(text), progress=progress)
    else:
        response = handle(dict(action="engine.info"))
    payload = json.dumps(response, ensure_ascii=False, indent=1, default=str)
    if getattr(args, "output", None):
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
