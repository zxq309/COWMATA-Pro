"""Capture the 4.4.3 illustrated tutorial from the real UI on an isolated guide farm.

The guide farm hard-links one real day of sensor JSON and recordings, so
nothing is copied and no original is ever written: every file the app creates
(index, annotations, settings, leases) stays under --work. Run on a desktop
session with the app's private Python, for example:

  runtime\\python.exe -I -B scripts\\capture_operator_guide_443.py ^
      --source <source tree> --components <portable folder> ^
      --farm F:\\1_下载器\\扬大_高邮牧场 --work <scratch folder> --out docs\\operator-guide-443-assets
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
import uuid
from pathlib import Path

DAY = "2026-09-06"
DEVICE = "0C3D5EA22DD6-21267-E3"
COW = "21267"
VIEWS = ("视角01", "视角02", "视角03", "视角04")
REUSED = {"01-login.png": "02-login.png", "40-ledger.png": "04-ledger.png", "41-pending.png": "05-pending.png",
          "42-download.png": "06-download.png", "43-organize.png": "07-organize.png", "44-accounts.png": "03-accounts.png"}


def link_tree(source, target, pattern):
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(source.glob(pattern)):
        destination = target / path.name
        if path.is_file() and not destination.exists():
            os.link(path, destination)
            count += 1
    return count


def build_guide_farm(farm, work):
    guide = work / "guide" / farm.name
    for modality in ("Motion", "PPG", "Temp"):
        origin = farm / "产犊" / modality / DAY / DEVICE
        if origin.is_dir():
            link_tree(origin, guide / "产犊" / modality / DAY / DEVICE, "*.json")
    for view in VIEWS:
        link_tree(farm / "录像" / DAY / view, guide / "录像" / DAY / view, "*.mp4")
    marker = guide / ".cowmata-farm.json"
    if not marker.exists():
        marker.write_text(json.dumps({"schema": "cowmata-farm-v1", "farm_id": str(uuid.uuid4()), "recordings": "录像"}),
                          encoding="utf-8")
    return guide


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--farm", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--record", default="2026-09-06_10")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    env = args.work / "env"
    for name in ("Local", "Roaming"):
        (env / name).mkdir(parents=True, exist_ok=True)
    # Isolate every per-user store (leases, downloader, model and login settings).
    os.environ["LOCALAPPDATA"] = str(env / "Local")
    os.environ["APPDATA"] = str(env / "Roaming")
    os.environ["COWMATA_DECIDER"] = "0"
    os.environ["COWMATA_LEDGER_HOME"] = str(env / "ledger")
    os.environ["VLC_HOME"] = str(args.components / "vendor/vlc")
    os.environ["VLC_PLUGIN_PATH"] = str(args.components / "vendor/vlc/plugins")
    os.environ["FFMPEG_HOME"] = str(args.components / "vendor/ffmpeg/bin")
    sys.path.insert(0, str(args.source))
    guide = build_guide_farm(args.farm, args.work)
    log = (args.work / "capture-log.txt").open("w", encoding="utf-8")

    def note(*parts):
        line = " ".join(str(p) for p in parts)
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    from PySide6.QtCore import QPoint, QSettings, Qt, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QFileDialog

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI", 10)
    font.setWeight(QFont.Weight.DemiBold)
    app.setFont(font)
    app.setOrganizationName("COWMATA-guide-443")
    app.setApplicationName("guide")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(env / "settings"))
    from cowmata_tailring.ui.i18n import install_qt_translations, set_language
    set_language("zh")
    app._guide_translator = install_qt_translations(app)
    from cowmata_tailring.workspace.theme import STYLE
    app.setStyleSheet(STYLE)
    import cowmata_security.qt_ui as qt_ui
    qt_ui.guard_action = lambda parent, capability: True
    from cowmata_tailring.workspace import organization_ui
    organization_ui.task_root = lambda: args.work / "organization-tasks"
    from cowmata_tailring.workspace.modern_window import MainWindow

    manifest = []

    def wait(predicate=lambda: False, timeout=1.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if predicate():
                return True
            time.sleep(.03)
        return bool(predicate())

    def shot(widget, name, caption):
        widget.show()
        widget.raise_()
        wait(timeout=.6)
        path = args.out / name
        assert widget.grab().save(str(path)), name
        manifest.append({"file": name, "caption": caption, "widget": type(widget).__name__})
        note("SHOT", name)

    def menu_shot(menu, name, caption, anchor):
        menu.popup(anchor)
        wait(timeout=.5)
        shot(menu, name, caption)
        menu.hide()
        wait(timeout=.2)

    def modal(call, name, caption, configure=None, accept=False):
        done = []

        def capture():
            dialog = app.activeModalWidget()
            if dialog is None:
                QTimer.singleShot(60, capture)
                return
            try:
                if configure:
                    configure(dialog)
                shot(dialog, name, caption)
            except Exception:
                note("FAIL-IN-MODAL", name, traceback.format_exc())
            done.append(True)
            dialog.accept() if accept else dialog.reject()

        QTimer.singleShot(250, capture)
        call()
        if not done:
            note("FAIL", name, "modal never appeared")

    def step(name, function):
        try:
            function()
        except Exception:
            note("FAIL", name, traceback.format_exc())

    def submenu(menu, title):
        return next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == title)

    def tiles_ready():
        tiles = window.board.tiles
        return bool(tiles) and all(t.ready and not t.pending for t in tiles.values() if t.interval) and any(
            t.interval for t in tiles.values())

    window = MainWindow()
    window.confirm_close = lambda: "save"
    window.resize(1500, 940)
    window.set_glass(False)
    window.show()
    wait(timeout=1.5)
    top = {a.text(): a.menu() for a in window.menuBar().actions()}

    def at(menu_title):
        action = next(a for a in window.menuBar().actions() if a.text() == menu_title)
        geometry = window.menuBar().actionGeometry(action)
        return window.menuBar().mapToGlobal(geometry.bottomLeft())

    step("main-empty", lambda: shot(window, "02-main-empty.png", "未打开工程时的主界面"))
    for index, (title, name) in enumerate((("文件", "file"), ("编辑", "edit"), ("上传", "upload"), ("下载", "download"),
                                           ("标注", "annotate"), ("工具", "tools"), ("帮助", "help")), 3):
        step("menu-" + name, lambda t=title, n=name, i=index: menu_shot(top[t], f"{i:02}-menu-{n}.png", t + " 菜单", at(t)))
    offset = QPoint(260, 0)
    step("menu-file-export", lambda: menu_shot(submenu(top["文件"], "导出"), "03b-menu-file-export.png", "文件 → 导出", at("文件") + offset))
    step("menu-download-organize", lambda: menu_shot(submenu(top["下载"], "数据归类"), "06b-menu-download-organize.png", "下载 → 数据归类", at("下载") + offset))
    step("menu-annotate-team", lambda: menu_shot(submenu(top["标注"], "协作"), "07b-menu-annotate-team.png", "标注 → 协作", at("标注") + offset))
    step("menu-annotate-check", lambda: menu_shot(submenu(top["标注"], "逐项检查"), "07c-menu-annotate-check.png", "标注 → 逐项检查", at("标注") + offset))
    for suffix, title, slug in (("b", "时间同步", "timing"), ("c", "录像", "video"), ("d", "视图", "view")):
        step("menu-tools-" + slug, lambda s=suffix, t=title, g=slug: menu_shot(submenu(top["工具"], t), f"08{s}-menu-tools-{g}.png", "工具 → " + t, at("工具") + offset))

    from cowmata_tailring.workspace.project_picker import ProjectPicker

    def picker():
        dialog = ProjectPicker(guide, window)
        index = dialog.day.findText(DAY)
        if index >= 0:
            dialog.day.setCurrentIndex(index)
        shot(dialog, "10-project-picker.png", "打开工程：确认牧场、类别、数据类型和日期")
        dialog.reject()
    step("project-picker", picker)

    motion_dir = guide / "产犊" / "Motion" / DAY / DEVICE
    record = next((p for p in sorted(motion_dir.glob("*.json")) if p.name.startswith(args.record)), None) or sorted(motion_dir.glob("*.json"))[len(list(motion_dir.glob("*.json"))) // 3]
    note("RECORD", record.name)
    window.open_project(guide / "产犊", day=DAY, preferred_json=record)
    if not wait(lambda: window.motion is not None and window.work is not None, 120):
        note("FAIL", "record did not load")
    if not window.source_panel.isVisible():
        window.toggle_sources()
    if not window.event_panel.isVisible():
        window.toggle_events()
    window.body.setSizes([250, 900, 380, 0])
    if not window.work.project.cow_id:
        window.work.project.cow_id = COW
        window.cow.setText(COW)
    window.board.play(False)
    start = window.work.clock.map(window.motion.duration_ms * .45)
    window.board.seek(start)
    ready = wait(tiles_ready, 180)
    note("TILES", ready, [(c, t.ready, bool(t.interval)) for c, t in window.board.tiles.items()])
    step("main-loaded", lambda: shot(window, "11-main-loaded.png", "打开一条九轴记录：左侧记录与视角，中间录像与波形，右侧标注列表"))

    def alignment():
        window.pin()
        wait(timeout=.8)
        shot(window, "12-alignment.png", "一次对齐：分别拖动录像和九轴到同一时刻，再点“完成对齐”")
        wait(tiles_ready, 60)
        window.complete_alignment()
        wait(timeout=1)
        shot(window, "12b-aligned.png", "完成对齐后：恢复同步跟随，本份记录可以确认真值")
    step("alignment", alignment)

    label_index = 0
    window.labels.setCurrentIndex(label_index)

    def record_action():
        window.board.seek(start)
        wait(tiles_ready, 90)
        window.mark(label_index)
        wait(timeout=.5)
        shot(window, "13-recording.png", "开始记录动作：按钮变成“结束…”，下方提示正在记录")
        window.board.seek(start + 8000)
        wait(tiles_ready, 90)
        window.mark(label_index)
        wait(timeout=.5)
        shot(window, "14-draft.png", "结束动作：右侧出现一条“视频草稿 · 九轴待确认”")
    step("record-action", record_action)

    def operations_menu():
        button = next(b for b in window.event_panel.findChildren(type(window.annotation_more)) if b.text() == "操作")
        menu_shot(button.menu(), "15-ops-menu.png", "右侧“操作”菜单", button.mapToGlobal(button.rect().bottomLeft()))
    step("ops-menu", operations_menu)

    def blocked():
        tile = window.board.tiles.get(window.board.main_camera)
        evidence = [{"camera": window.board.main_camera, "asset_id": tile.asset_id, "frame_ready": True,
                     "verified_interval": False, "reference_ms": window.board.reference_ms}]
        message, row = window.evidence_problem(evidence)
        visible = window.isVisible
        modal(lambda: window.explain_blocked("还不能确认真值", message, row), "16-confirm-blocked.png",
              "录像时间未核验时：提示写明是哪个视角、哪个录像，并可直接“去核验该录像”")
        assert visible()
    step("confirm-blocked", blocked)

    def confirm():
        draft = next(d for d in window.work.drafts if d.get("confirmation") != "confirmed")
        window.refresh_events(preferred=("draft", draft["id"]))
        window.board.seek(draft["reference_start"])
        wait(tiles_ready, 90)
        window.confirm_selected()
        wait(lambda: window._capture_dialog is not None, 5)
        wait(timeout=2.5)
        if window._capture_dialog is not None:
            shot(window._capture_dialog, "18-evidence.png", "确认后自动打开“留存多视角证据图”，可为每个视角保存一张原片截图")
            window._capture_dialog.close()
        wait(timeout=.5)
        shot(window, "17-confirmed.png", "确认真值后：状态变为“已确认”")
    step("confirm", confirm)

    def source_manager():
        original = window.rows
        tile = window.board.tiles.get(window.board.main_camera)
        changed = []
        for row in original:
            if row["kind"] == "video" and tile is not None and row["asset_id"] == tile.asset_id:
                metadata = dict(row["metadata"])
                metadata["intervals"] = [dict(i, verified=False) for i in metadata.get("intervals", [])]
                metadata["warnings"] = ["教程示例：此行临时标为“待复核”，用于演示黄色行"]
                row = dict(row, state="review", metadata=metadata)
            changed.append(row)
        window.rows = changed
        try:
            modal(window.source_manager, "19-source-manager.png", "左侧“核验”：默认只看当前记录用到的录像，黄色行就是需要核验时间的录像")

            def verify_scope(dialog):
                from PySide6.QtWidgets import QComboBox
                dialog.findChild(QComboBox).setCurrentIndex(1)
                wait(timeout=.6)
            modal(window.source_manager, "19b-source-manager-verify.png", "切换为“需要核验时间的录像”，一次看全所有待核验的录像", configure=verify_scope)
            target = next(r for r in changed if r["state"] == "review")

            def readings(dialog):
                dialog.load_start()
                wait(lambda: dialog.frame is not None, 60)
                dialog.accept_reading()
                dialog.frame = None
                dialog.load_end()
                wait(lambda: dialog.frame is not None, 60)
                dialog.resize(1000, 760)
                wait(timeout=.5)
            modal(lambda: window.edit_source(target), "20-time-dialog.png",
                  "录像时间核验：读取开头画面并确认读数，再读取结尾画面；预填读数需与画面核对", configure=readings)
        finally:
            window.rows = original
    step("source-manager", source_manager)

    def layouts():
        for index in range(min(4, window.cameras.count())):
            window.cameras.item(index).setCheckState(Qt.CheckState.Checked)
        wait(timeout=1)
        window.set_presentation("B")
        wait(tiles_ready, 120)
        shot(window, "21-multiview.png", "多视角：同时查看最多 8 路录像")
        window.set_presentation("C")
        wait(timeout=1)
        shot(window, "22-waveform.png", "波形：放大九轴 / PPG / 温度曲线，录像变为画中画")
        window.set_presentation("A")
        for index in range(1, window.cameras.count()):
            window.cameras.item(index).setCheckState(Qt.CheckState.Unchecked)
        wait(tiles_ready, 60)
        window.enlarge_video()
        wait(timeout=1)
        shot(window, "23-enlarge.png", "放大视频（Ctrl+E）：录像占满，波形保留为窄条")
        window.set_presentation("A")
        wait(timeout=.5)
    step("layouts", layouts)

    def edit_dialog():
        event = window.work.project.events[0]
        window.refresh_events(preferred=("event", event.id))
        modal(window.edit_selected, "24-edit-dialog.png", "编辑标注：修改标签、起止和备注；修改后需重新确认")
    step("edit-dialog", edit_dialog)

    step("finish", lambda: modal(window.finish_record, "25-finish.png", "完成本份（Ctrl+Enter）：保存并标为已完成"))

    def candidates():
        window.open_candidates()
        wait(lambda: window._candidate_window is not None, 5)
        wait(timeout=1.5)
        shot(window._candidate_window, "26-candidates.png", "自动生成候选：模型给出候选位置，人工看录像确认")
        window._candidate_window.hide()
    step("candidates", candidates)

    def history():
        target = args.work / "exports" / "guide.标注.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        original = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), "JSON"))
        try:
            window.export_work()
            wait(lambda: target.is_file() and not window._export_running, 60)
        finally:
            QFileDialog.getSaveFileName = original
        from cowmata_tailring.workspace.history_window import HistoryWindow
        viewer = HistoryWindow(target, guide / "产犊")
        viewer.resize(1450, 900)
        viewer.show()
        wait(lambda: viewer.data is not None, 60)
        if viewer.events.count():
            viewer.events.setCurrentRow(0)
        wait(timeout=2)
        shot(viewer, "27-history.png", "历史回看：只读打开导出的标注文件，核对标签与录像")
        viewer.close()
    step("history", history)

    from cowmata_tailring.workspace.collaboration_ui import CollaborationDialog, open_dispatch

    def dispatch():
        dialog = open_dispatch(window, str(guide))
        wait(lambda: dialog.status.text().startswith("扫描完成"), 120)
        dialog.count.setValue(2)
        wait(timeout=1)
        shot(dialog, "28-dispatch.png", "派发原始数据包：自动扫描，按包勾选日期，一键派包（不等下载）")
        dialog.close()
        wait(timeout=.5)
    step("dispatch", dispatch)
    for mode, name, caption in (("returns", "29-returns.png", "生成标注数据包：只含标注和证据图"),
                                ("receive", "30-receive.png", "接收标注数据包：先完整校验，再接收"),
                                ("open", "31-open-package.png", "打开协作数据包：核验并解包后直接打开工程")):
        def collaboration(m=mode, n=name, c=caption):
            dialog = CollaborationDialog(m, window, str(guide))
            shot(dialog, n, c)
            dialog.reject()
            dialog.pool.shutdown(wait=False)
        step(mode, collaboration)

    def mapping():
        from cowmata_tailring.workspace.dialogs import MappingDialog
        dialog = MappingDialog(window.work.clock, window)
        shot(dialog, "32-mapping.png", "精细校准：越播越偏时增加对应点")
        dialog.reject()
    step("mapping", mapping)

    def settings():
        window.presentation_settings()
        wait(timeout=.8)
        shot(window.options, "33-settings.png", "工具 → 设置：播放、画中画、波形与外观")
        window.options.hide()
    step("settings", settings)

    def about():
        from cowmata_tailring.ui.about import create_about
        dialog = create_about(window)
        shot(dialog, "34-about.png", "帮助 → 关于：版本、更新、教程与提示信息说明")
        dialog.close()
    step("about", about)
    step("quick-help", lambda: modal(window.quick_help, "35-quick-help.png", "帮助 → 快速开始（F1）"))

    source_assets = args.source / "docs" / "operator-guide-440-assets"
    for target, origin in REUSED.items():
        if (source_assets / origin).is_file():
            shutil.copy2(source_assets / origin, args.out / target)
            manifest.append({"file": target, "caption": "沿用 4.4.1 界面截图（界面未变化）", "widget": "reused"})
    (args.work / "capture-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    window.save_current()
    window.close()
    wait(lambda: getattr(window, "_closed", False), 20)
    note("DONE", len(manifest), "screenshots")
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
