"""Single entry point for the Cowmata TailRing desktop application."""

from __future__ import annotations

import argparse
import sys

APP_ORG = "Cowmata"
APP_NAME = "Cowmata TailRing"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cowmata-annotator",
        description="Video and nine-axis IMU annotation workstation for cattle behaviour.",
    )
    parser.add_argument(
        "--mode",
        choices=("model-assist", "basic", "workspace"),
        default="model-assist",
        help="Window to launch: model-assisted review (default) or basic annotation.",
    )
    parser.add_argument(
        "--lang",
        choices=("auto", "en", "zh"),
        default="auto",
        help="Interface language (default: follow system locale).",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="Open a nine-axis sensor JSON file after launch.",
    )
    parser.add_argument(
        "--video",
        metavar="PATH",
        help="Open a synchronized video file after launch.",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    parser.add_argument("--project", metavar="DIRECTORY", help="Open a multiview data project directory.")
    parser.add_argument("--annotations", metavar="PATH", help="Open one annotation JSON for independent read-only review; --project can relink its sources.")
    parser.add_argument("--workspace-ui", choices=("modern", "classic"), default="modern",
                        help="Workspace presentation; classic remains available for comparison.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from cowmata_tailring import __version__

    if args.version:
        print(f"cowmata-annotator {__version__}")
        return 0

    from cowmata_tailring.app.resources import prioritize_ui
    print("COWMATA scheduling: " + prioritize_ui(), flush=True)

    from PySide6.QtCore import QCoreApplication, Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.ui.i18n import set_language

    set_language(args.lang)

    QCoreApplication.setOrganizationName(APP_ORG)
    QCoreApplication.setApplicationName(APP_NAME)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    application = QApplication(sys.argv[:1])
    application.setStyle("Fusion")

    if args.annotations:
        from cowmata_tailring.workspace.history_window import HistoryWindow
        window = HistoryWindow(args.annotations, args.project)
        window.show()
        return application.exec()

    if (args.mode == "workspace" and not (args.json or args.video)) or args.project:
        if args.workspace_ui == "classic":
            from cowmata_tailring.workspace.window import MainWindow
        else:
            from cowmata_tailring.workspace.modern_window import MainWindow
    elif args.mode in {"basic", "workspace"}:
        from cowmata_tailring.app.basic_window import MainWindow
    else:
        from cowmata_tailring.app.model_assist_window import MainWindow

    window = MainWindow()
    window.show()

    def open_startup_files() -> None:
        if args.project:
            window.open_project(args.project)
            return
        if args.json:
            window.open_json(args.json)
        if args.video:
            window.open_video(args.video)

    if args.project or args.json or args.video:
        QTimer.singleShot(0, open_startup_files)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
