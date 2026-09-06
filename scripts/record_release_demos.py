"""Record the real Windows application, using an isolated, marked demo project.

No results or screenshots are fabricated. Qt drives the actual controls, while
FFmpeg desktop duplication captures the exact topmost app rectangle, including
GPU video surfaces. Demo alignment/labels are not field truth.
Raw project files stay local; only short recordings and screenshots are public.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton
from cowmata_tailring.app.resources import prioritize_ui
from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
from cowmata_tailring.workspace.label_file import read_label_file
from cowmata_tailring.workspace.modern_window import MainWindow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    marker = json.loads((args.project / "DEMO-ONLY.json").read_text(encoding="utf-8"))
    assert marker["not_ground_truth"] and marker["artificial_alignment"]
    args.out.mkdir(parents=True, exist_ok=False)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setOrganizationName("COWMATA-Demo")
    app.setApplicationName("Isolated-release-demo")
    prioritize_ui()
    window = MainWindow()
    window.setWindowTitle("COWMATA Demo - real application - sample alignment only")
    window.show()
    window.open_project(args.project)
    process = None
    log = None
    actions = []
    started = time.monotonic()

    def capture_input(owner):
        owner.raise_()
        owner.activateWindow()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos(int(owner.winId()), -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
        point, rect = wintypes.POINT(), wintypes.RECT()
        user32.ClientToScreen(wintypes.HWND(int(owner.winId())), ctypes.byref(point))
        user32.GetClientRect(wintypes.HWND(int(owner.winId())), ctypes.byref(rect))
        assert point.x >= 0 and point.y >= 0
        return f"ddagrab=framerate=12:draw_mouse=1:offset_x={point.x}:offset_y={point.y}:video_size={rect.right}x{rect.bottom}"

    def click(widget):
        widget.setFocus()
        QCursor.setPos(widget.mapToGlobal(widget.rect().center()))
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)

    def button(owner, text):
        return next(b for b in owner.findChildren(QPushButton) if b.text() == text)

    def ready():
        return bool(window.motion and len(window.board.tiles) == 3 and all(t.ready for t in window.board.tiles.values()))

    def begin(name, owner):
        nonlocal process, log
        assert process is None
        owner.raise_()
        owner.activateWindow()
        target = args.out / (name + ".mp4")
        log = (args.out / (name + ".ffmpeg.log")).open("wb")
        # Crop the protected, topmost app rectangle; do not record the desktop.
        # Desktop duplication captures GPU overlays omitted by GDI window DCs.
        process = subprocess.Popen([str(find_ffmpeg()[0]), "-hide_banner", "-loglevel", "warning",
            "-f", "lavfi", "-i", capture_input(owner),
            "-an", "-vf", "hwdownload,format=bgra,scale=1280:-2", "-c:v", "libx264", "-threads", "2", "-preset", "fast",
            "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, creationflags=0x08000000)

    def stop():
        nonlocal process, log
        if process is not None:
            process.communicate(b"q\n", timeout=20)
            code = process.returncode
            process = None
            log.close()
            log = None
            assert code == 0, "Screen recorder failed"

    def photo(name, owner):
        subprocess.run([str(find_ffmpeg()[0]), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", capture_input(owner),
            "-vf", "hwdownload,format=bgra", "-frames:v", "1", "-q:v", "3", str(args.out / (name+".jpg"))],
            check=True, timeout=15, creationflags=0x08000000)

    def steps():
        yield "wait for real sample project", 0, ready, lambda: None
        window.root_label.setText("DEMO · 真实录像与九轴 · 示例同步/标签，不作科研真值")
        window.playback_policy.setCurrentIndex(1)
        yield "record observation workflow", 2, ready, lambda: begin("01-multiview", window)
        yield "real overview screenshot", 2, ready, lambda: photo("workspace-live", window)
        yield "play three actual scenes", 1, None, lambda: click(window.play_button)
        yield "switch grid", 5, None, lambda: click(window.layout_buttons.button(1))
        yield "grid screenshot", 2, None, lambda: photo("multiview-live", window)
        yield "promote another camera", 2, None, lambda: window.board.set_main("演示视角2")
        yield "pause for original PTS", 3, None, lambda: window.board.play(False)
        yield "waveform picture in picture", 2, ready, lambda: click(window.layout_buttons.button(2))
        yield "hover actual raw values", 2, None, lambda: QTest.mouseMove(window.plot.wave, QPoint(500, 90))
        yield "return to observation", 3, None, lambda: click(window.layout_buttons.button(0))
        yield "seek before file boundary", 2, None, lambda: window.seek_imu(507000)
        yield "play across separate recordings", 1, ready, lambda: window.board.play(True)
        yield "pause after automatic continuation", 7, None, lambda: window.board.play(False)
        yield "verify actual next file", 2, ready, lambda: None
        assert all(t.interval.path.endswith("002.mp4") for t in window.board.tiles.values())
        stop()
        window.open_candidates()
        dialog = window._candidate_window
        dialog.setWindowTitle("COWMATA Demo - actual model candidates - human review required")
        dialog.resize(1040, 650)
        yield "record real inference", 2, None, lambda: begin("02-candidates", dialog)
        yield "scan original IMU with five frozen models", 2, None, lambda: click(dialog.start_button)
        yield "wait for actual model results", 1, lambda: not dialog.running, lambda: None
        runs = window.work.project.extras.get("event_model_runs", {})
        assert len(runs) == 5 and not window.work.project.events
        actions.append(dict(actual_five_model_results={r["identity"]["model_id"]: len(r["candidates"]) for r in runs.values()}))
        eligible = []
        for i in range(dialog.items.count()):
            dialog.items.setCurrentRow(i)
            _, candidate = dialog.selected()
            if candidate and 480000 <= candidate["point_ms"] < 540000:
                eligible.append((abs(candidate["point_ms"] - 490000), i))
        assert eligible, "This demo sample needs a real candidate within its demo video interval"
        dialog.items.setCurrentRow(min(eligible)[1])
        yield "show candidate audit", 3, None, lambda: click(dialog.details)
        yield "real candidate screenshot", 3, None, lambda: photo("candidates-live", dialog)
        yield "review candidate on shared timeline", 3, None, lambda: click(button(dialog, "定位九轴与录像"))
        yield "finish candidate recording", 2, None, stop
        dialog.close()
        yield "prepare annotation interval", 1, None, lambda: window.seek_imu(490000)
        window.labels.setCurrentIndex(0)
        yield "record actual label workflow", 2, ready, lambda: begin("03-label-export", window)
        yield "mark interval start", 2, ready, lambda: click(window.mark_button)
        assert window.active_event is not None
        yield "observe the interval", 1, None, lambda: click(window.play_button)
        yield "pause to confirm end frame", 6, None, lambda: window.board.play(False)
        yield "mark interval end", 2, ready, lambda: click(window.mark_button)
        assert len(window.work.drafts) == 1
        window.work.drafts[0]["note"] = "DEMO ONLY: operation example, not scientific ground truth"
        window.refresh_events()
        yield "open separate label list", 2, None, window.toggle_events
        window.events.selectRow(0)
        yield "demonstrate explicit human confirmation", 3, None, window.confirm_selected
        assert len(window.work.project.events) == 1
        yield "label list screenshot", 2, None, lambda: photo("labels-live", window)
        exported = args.out / "DEMO-ONLY.snippet.annotations.json"
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(exported), "JSON"))
        window.selection = (488000, 505000)
        yield "export one traceable annotation snippet", 3, None, lambda: window.export_work(snippet=True)
        yield "wait for real export completion", 1, lambda: not window._export_running, lambda: None
        document = read_label_file(exported)
        assert 488000 <= document["embedded_imu"]["parent_start_ms"] < 488025
        yield "finish label recording", 3, None, stop
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(exported), "JSON"))
        window.open_history()
        history = window._history_windows[-1]
        history.setWindowTitle("COWMATA Demo - independent annotation history")
        yield "wait for independent history", 1, lambda: history.data is not None and len(history.board.tiles) == 3 and all(t.ready for t in history.board.tiles.values()), lambda: None
        yield "record restored original-relative snippet", 2, None, lambda: begin("04-history", history)
        yield "history screenshot", 2, None, lambda: photo("history-live", history)
        yield "play restored history", 2, None, lambda: click(history.play_button)
        yield "switch history grid", 4, None, lambda: history.view.setCurrentIndex(1)
        yield "review another view", 3, None, lambda: history.board.set_main("演示视角3")
        yield "pause at snippet end", 10, lambda: not history.board.playing, stop
        assert abs(history.board.reference_ms - history.data.work.clock.map(history.bounds()[1])) < 1
        history.dispose()

    sequence = steps()
    current = None
    due = 0
    error = None

    def tick():
        nonlocal current, due, error
        try:
            if time.monotonic() - started > 300:
                raise TimeoutError(str(current[0] if current else "starting"))
            if process is not None and process.poll() is not None:
                raise RuntimeError("Recording process exited before completion")
            if current is None:
                current = next(sequence)
                due = time.monotonic() + current[1]
            if time.monotonic() >= due and (current[2] is None or current[2]()):
                current[3]()
                actions.append(dict(step=current[0], seconds=round(time.monotonic()-started, 3)))
                print(current[0], flush=True)
                current = None
        except StopIteration:
            finish()
        except Exception as exc:
            error = repr(exc)
            finish()

    def finish():
        timer.stop()
        try:
            stop()
        finally:
            report = dict(passed=error is None, error=error, real_app=True, sample_alignment_not_truth=True, actions=actions)
            (args.out / "recording-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(dict(passed=error is None, error=error)), flush=True)
            window.close()
            app.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(100)
    app.exec()
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
