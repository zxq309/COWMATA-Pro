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


def test_docx_upload_same_name_is_idempotent_and_collision_safe(tmp_path):
    first = tmp_path / "bug.docx"
    make_docx(first)
    staged = tmp_path / "staged"
    original = stage_docx_upload(first, staged)
    assert stage_docx_upload(first, staged) == original

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = second_dir / "bug.docx"
    with zipfile.ZipFile(second, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types><Default/></Types>")
        archive.writestr("word/document.xml", "<document><body/></document>")
    collision = stage_docx_upload(second, staged)
    assert collision != original
    assert collision.suffix == ".docx"
    assert not list(staged.glob("*.part"))


def test_docx_upload_rejects_bad_crc(tmp_path):
    source = tmp_path / "bad.docx"
    make_docx(source)
    data = bytearray(source.read_bytes())
    # Corrupt a payload byte without changing the ZIP directory CRC.
    marker = b"<document/>"
    position = data.index(marker)
    data[position] ^= 0xFF
    source.write_bytes(data)
    with pytest.raises(ValueError, match="损坏|校验"):
        validate_docx(source)
