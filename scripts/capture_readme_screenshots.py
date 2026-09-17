"""Render reproducible README annotation examples from a local IMU JSON.

The generated labels are deliberately illustrative UI fixtures, not scientific
ground truth for the source recording. Real farm data stays local.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from cowmata_tailring.app.basic_window import MainWindow
from cowmata_tailring.ui.i18n import set_language


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", type=Path, help="Local nine-axis source JSON")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "assets" / "screenshots",
    )
    return parser


def process_ui(application: QApplication, window: MainWindow) -> None:
    for _ in range(8):
        application.processEvents()
    window.repaint()
    application.processEvents()


def save_window(window: MainWindow, target: Path) -> None:
    pixmap = window.grab()
    if pixmap.isNull() or not pixmap.save(str(target), "JPG", 92):
        raise RuntimeError(f"Could not save screenshot: {target}")


def main() -> int:
    args = build_parser().parse_args()
    source = args.json_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # The overview image already demonstrates Chinese. Rendering these two
    # examples in English also shows the bilingual UI and avoids platform-font
    # differences in headless CI environments.
    set_language("en")
    application = QApplication(["capture-readme-screenshots"])
    application.setStyle("Fusion")
    for font_path in (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    ):
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                application.setFont(QFont(families[0], 9))
                break
    window = MainWindow()
    window.resize(1600, 1000)
    window.show()
    window._autosave = lambda: None
    window.open_json(str(source))

    label_by_code = {
        str(label.get("code", "")): index
        for index, label in enumerate(window.labels)
    }

    def add_example(code: str, start_ms: float, end_ms: float | None, note: str) -> int:
        window._add_event(label_by_code[code], start_ms, end_ms)
        event = window.events[-1]
        event["note"] = note
        window._refresh_events()
        return int(event["id"])

    standing_id = add_example(
        "STANDING",
        10_000.0,
        24_000.0,
        "UI example: drag either boundary to refine the interval",
    )
    window.selected_event_id = standing_id
    window.plot.focus_event(standing_id, minimum_span_ms=30_000.0)
    window._sync_plot_event_selection()
    window.set_playhead(17_000.0, seek_video=False)
    process_ui(application, window)
    save_window(window, output_dir / "annotation-interval-example.jpg")

    tail_id = add_example(
        "TAIL_RAISED",
        15_000.0,
        20_500.0,
        "UI example: event overlaps the body-state interval",
    )
    add_example(
        "SYNC_ANCHOR",
        12_000.0,
        None,
        "UI example: video-to-IMU synchronization anchor",
    )
    add_example(
        "STANDING_UP",
        29_000.0,
        35_000.0,
        "UI example: posture-transition interval",
    )
    window.selected_event_id = tail_id
    window.plot.set_view(0.0, 45_000.0, True)
    window._sync_plot_event_selection()
    window.set_playhead(18_000.0, seek_video=False)
    process_ui(application, window)
    save_window(window, output_dir / "annotation-multilabel-example.jpg")

    window.close()
    application.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
