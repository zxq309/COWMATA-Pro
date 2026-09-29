"""4.4.1: the downloader is one button, one table and a short 更多 menu."""
from test_download_427 import FILES
from test_relative_download_412 import isolated_store  # noqa: F401


def test_one_table_and_short_more_menu(isolated_store, qt_application):  # noqa: F811
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog

    def row(number, day, device, eligibility="eligible", reason="ok"):
        return {"source": FILES[0], "eligibility": eligibility, "row": number, "reason": reason,
                "device": device, "cow": "C" + str(number), "mark": "", "category": "产犊", "warnings": "",
                "start": f"2026-09-{day:02d}T09:00:00+08:00", "end": f"2026-09-{day + 1:02d}T09:00:00+08:00"}

    class Plan:
        issues = []
        local_status = {2: dict(state="downloaded", files=5), 3: dict(state="missing", files=0),
                        4: dict(state="partial", files=2), 5: dict(state="invalid", files=0),
                        6: dict(state="today", files=0), 7: dict(state="current", files=4)}

        def preview(self):
            return [row(2, 20, "AAAA"), row(3, 18, "BBBB"), row(4, 21, "CCCC"),
                    row(5, 22, "DDDD", "excluded", "九轴无效，不下载此条记录的三类数据"),
                    row(6, 24, "EEEE"), row(7, 19, "FFFF")]

    dialog = ProDownloadDialog(store=isolated_store)
    try:
        dialog.receive_plan(Plan(), "")

        def shown():
            return sorted(dialog.plan_table.item(i, 2).text() for i in range(dialog.plan_table.rowCount()))

        assert shown() == ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"]
        dialog.search.setText("cccc")
        assert shown() == ["CCCC"]
        dialog.search.clear()
        top = [a.text() for a in dialog.more_menu.actions() if not a.isSeparator()]
        assert top == ["设置…", "运行记录…", "问题清单…", "备注记录…", "只刷新台账", "检测连接", "重新下载所选…",
                       "打开目录", "决策 APP"]
        assert dialog.download_button.objectName() == "primary" and dialog.download_button.isCheckable()
    finally:
        dialog.deleteLater()
