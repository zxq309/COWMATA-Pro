import zipfile

import pytest

from cowmata_tailring.workspace.docx_intake import stage_docx_upload, validate_docx


def make_docx(path, *, unsafe=False):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("../escape.txt" if unsafe else "word/document.xml", "<document/>")


def test_docx_upload_is_staged_atomically(tmp_path):
    source = tmp_path / "bug.docx"
    make_docx(source)
    target = stage_docx_upload(source, tmp_path / "staged")
    assert target.read_bytes() == source.read_bytes()
    assert not target.with_name(target.name + ".part").exists()


def test_docx_upload_rejects_partial_or_unsafe_package(tmp_path):
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="损坏"):
        validate_docx(broken)
    unsafe = tmp_path / "unsafe.docx"
    make_docx(unsafe, unsafe=True)
    with pytest.raises(ValueError, match="不安全"):
        validate_docx(unsafe)
