import csv
import io

from cowmata_tailring.workspace.classification_report import LiveReport


def test_records_are_split_by_camera_and_keep_selection_during_live_updates(qt_application, tmp_path):
    from cowmata_tailring.workspace.classification_viewer import ClassificationReportWindow
    report = LiveReport(tmp_path)
    rows = [dict(source=f'{camera}-{i}.mp4', owner=camera, status='pending')
            for camera in ('视角02', '视角01') for i in range(2)]
    report.seed(rows)
    w = ClassificationReportWindow(None, lambda: tmp_path)
    try:
        assert hasattr(w, 'sheet_tabs'), 'Camera records still share one scrolling table'
        assert list(w.sheets) == ['视角01', '视角02']
        w.sheet_tabs.setCurrentIndex(1)
        w.table.selectRow(1)
        report.row({**rows[1], 'status': 'done'})
        report.flush(force=True)
        w.refresh()
        assert w.current_sheet() == '视角02'
        assert w.table.currentIndex().row() == 1
        assert len(w.model.rows) == 2 and {r['owner'] for r in w.model.rows} == {'视角02'}
        assert w.model.rows[1]['status'] == 'done'
        current = list(csv.DictReader(io.StringIO(w.export_payload(False).decode('utf-8-sig'))))
        whole = list(csv.DictReader(io.StringIO(w.export_payload(True).decode('utf-8-sig'))))
        assert len(current) == 2 and len(whole) == 4
    finally:
        w.close()


def test_new_job_clears_old_sheets_and_selected_details(qt_application, tmp_path):
    from cowmata_tailring.workspace.classification_viewer import ClassificationReportWindow
    first = tmp_path/'old'
    LiveReport(first).seed([dict(source='old.mp4', owner='视角01', status='done')])
    second = tmp_path/'new'
    LiveReport(second).seed([dict(source='new.mp4', owner='视角07', status='pending')])
    job = [first]
    w = ClassificationReportWindow(None, lambda: job[0])
    try:
        assert hasattr(w, 'sheet_tabs'), 'Independent record sheets are missing'
        w.table.selectRow(0)
        w.show_details(w.model.index(0, 0))
        job[0] = second
        w.refresh()
        assert list(w.sheets) == ['视角07'] and 'old.mp4' not in w.details.text()
        job[0] = None
        w.refresh()
        assert not w.model.rows and w.snapshot is None
    finally:
        w.close()


def test_organization_has_one_record_button_and_reuses_one_live_window(qt_application, tmp_path):
    from PySide6.QtWidgets import QMainWindow, QPushButton

    from cowmata_tailring.workspace.organization_ui import OrganizationWindow
    owner = QMainWindow()
    w = OrganizationWindow(owner)
    w.job = tmp_path
    LiveReport(tmp_path).seed([dict(source='one.mp4', owner='视角01', status='done')])
    try:
        buttons = [b for b in w.findChildren(QPushButton) if b.text() in {'打开实时 CSV', '打开完整记录', '查看归类记录'}]
        assert len(buttons) == 1 and buttons[0].text() == '查看归类记录'
        w.export_report()
        first = w._report_window
        w.open_full_records()
        assert w._report_window is first
    finally:
        w.close()
