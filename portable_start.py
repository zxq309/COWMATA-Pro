"""Self-contained launcher with readable crash recovery; no environment setup."""
from __future__ import annotations

import ctypes
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    os.environ["VLC_PLUGIN_PATH"] = str(root / "vendor" / "vlc" / "plugins")
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["OPENBLAS_NUM_THREADS"] = "2"
    os.environ["QT_LOGGING_RULES"] = "qt.multimedia.*=false"
    log_dir = root / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
    except OSError:
        log_dir = Path(os.environ.get("LOCALAPPDATA", str(root))) / "COWMATA" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "annotator.log"
    stream = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = stream

    def report(exc_type, exc, tb):
        stream.write(f"\n{datetime.now().isoformat()}\n")
        traceback.print_exception(exc_type, exc, tb, file=stream)
        stream.flush()
        ctypes.windll.user32.MessageBoxW(None,
            "程序遇到错误，原始视频和九轴文件没有被修改。\n请保留工程和以下日志用于排查：\n" + str(log_path),
            "COWMATA 运行错误", 0x10)

    sys.excepthook = report
    try:
        from cowmata_tailring.app.main import main as app_main
        args = sys.argv[1:]
        if "--mode" not in args:
            args = ["--mode", "workspace", *args]
        return app_main(args)
    except Exception:
        report(*sys.exc_info())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
