import pytest

from scripts.build_portable import write_checksum


def test_portable_checksum_accepts_chinese_package_name(tmp_path):
    archive = tmp_path / "算法预览.zip"
    write_checksum(archive, "a" * 64)
    assert archive.with_suffix(".zip.sha256").read_text(encoding="utf-8") == "a" * 64 + "  算法预览.zip\n"


def test_portable_checksum_never_overwrites_existing_result(tmp_path):
    archive = tmp_path / "preview.zip"
    write_checksum(archive, "a" * 64)
    with pytest.raises(FileExistsError):
        write_checksum(archive, "b" * 64)
    assert archive.with_suffix(".zip.sha256").read_text(encoding="utf-8").startswith("a" * 64)
