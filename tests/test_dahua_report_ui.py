from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.dahua_ui import DahuaPanel


def row(key, owner, status, **extra):
    return dict(
        event_kind="task_record",
        source_id=key,
        owner=owner,
        status=status,
        source="recorder",
        targets=[],
        **extra,
    )


def test_report_button_opens_live_view_sheets_without_external_spreadsheet(tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    panel = DahuaPanel()
    try:
        panel.job = tmp_path
        panel.run_tables.accept(row("a", "视角01", "processing", phase="convert"))
        panel.run_tables.accept(row("b", "视角02", "waiting"))
        panel.run_tables.flush()
        panel.open_report()
        report = getattr(panel, "report_dialog", None)
        assert report is not None, "The report button only selected the mixed results table"
        assert report.isVisible() and not report.isModal()
        assert [report.view_tabs.tabText(i) for i in range(report.view_tabs.count())] == [
            "总览",
            "视角01",
            "视角02",
        ]
        assert report.overview.rowCount() == 2
        assert "正在处理 1 个视角" in report.status.text()
        panel.open_report()
        assert panel.report_dialog is report
    finally:
        panel.close()


def test_view_sheet_filters_live_timing_and_results_and_tracks_updates(tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    panel = DahuaPanel()
    try:
        panel.job = tmp_path
        table = panel.run_tables
        table.accept(row("a", "视角01", "processing", phase="convert", file_seconds=3))
        table.accept(row("b", "视角02", "waiting"))
        table.accept(
            dict(event_kind="archive_record", status="done", owner="视角02", target="two.mp4")
        )
        table.flush()
        panel.open_report()
        report = getattr(panel, "report_dialog", None)
        assert report is not None
        report.view_tabs.setCurrentIndex(1)
        assert report.live.model().rowCount() == 1
        assert report.live.model().index(0, 2).data() == "视角01"
        assert report.timing.model().rowCount() == 1
        assert report.results.model().rowCount() == 0
        table.accept(row("a", "视角01", "done", file_seconds=7))
        table.flush()
        report.refresh()
        assert report.live.model().index(0, 0).data() == "已归档"
        assert report.live.model().index(0, 5).data() == "7"
        report.view_tabs.setCurrentIndex(2)
        assert report.live.model().index(0, 2).data() == "视角02"
        assert report.results.model().rowCount() == 1
        table.begin()
        report.refresh()
        assert report.view_tabs.count() == 1
        assert report.overview.rowCount() == 0
    finally:
        panel.close()


def test_overview_counts_actual_active_views_and_never_calls_waiting_parallel(tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    panel = DahuaPanel()
    try:
        panel.job = tmp_path
        for item in [
            row("a", "视角01", "processing", phase="read"),
            row("b", "视角01", "waiting"),
            row("c", "视角02", "processing", phase="convert"),
            row("d", "视角03", "waiting"),
            row("e", "视角03", "blocked", message="bad clock"),
        ]:
            panel.run_tables.accept(item)
        panel.run_tables.flush()
        panel.open_report()
        report = getattr(panel, "report_dialog", None)
        assert report is not None
        assert "正在处理 2 个视角" in report.status.text()
        assert report.overview.item(0, 3).text() == "1"
        assert report.overview.item(2, 3).text() == "0"
        assert report.overview.item(2, 4).text() == "1"
        assert report.overview.item(2, 5).text() == "1"
    finally:
        panel.close()
