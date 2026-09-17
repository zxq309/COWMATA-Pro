def test_chinese_legacy_csv_preserves_gb18030_text(tmp_path):
    from cowmata_tailring.workspace.legacy_labels import read_legacy_table

    path = tmp_path / "one.csv"
    path.write_bytes(
        "session_id,cow_id,label,time\n2026-08-27,19162B7,犊牛完全娩出,17:05:00\n".encode("gb18030")
    )
    result = read_legacy_table(path)
    assert result["encoding"] == "gb18030"
    assert result["events"][0]["code"] == "CALF_FULLY_EXPELLED"
    assert result["events"][0]["cow_id"] == "19162"
