"""Packaging regressions; native installation is verified separately on Windows."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_icon_provenance_and_windows_sizes():
    import struct
    root = ROOT / "assets/app-icon"
    provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    for name, sha in provenance["sha256"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == sha
    data = (root / "cowmata.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1) and count >= 7
    sizes = {data[6 + i * 16] or 256 for i in range(count)}
    assert {16, 24, 32, 48, 64, 128, 256} <= sizes


def test_installer_encoding_shortcuts_and_non_overwrite():
    script = (ROOT / "packaging/installer.nsi").read_text(encoding="utf-8")
    builder = (ROOT / "scripts/build_installer.py").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts/build_launcher.ps1").read_text(encoding="utf-8")
    assert "'/INPUTCHARSET', 'UTF8'" in builder
    assert "/codepage:65001" in launcher and "/win32icon:" in launcher
    assert 'Page custom LocationCreate LocationLeave' in script
    assert 'StrCpy $DesktopEnabled ${BST_CHECKED}' in script
    assert 'CreateShortcut "$DESKTOP\\COWMATA Annotator ${VERSION}.lnk"' in script
    assert '"DisplayIcon" "$INSTDIR\\COWMATA.exe,0"' in script
    assert 'Call ValidateLocation' in script and '$(OccupiedLocation)' in script
    # NSIS GetFullPathName may return empty for nonexistent paths; use Win32.
    assert 'kernel32::GetFullPathNameW' in script
    assert '${If} $0 <> -1' in script and '${If} $1 <> 16' in script
    assert 'RMDir /r' not in script and 'SetCompressor zlib' in script
    assert 'CRCCheck force' in script
    assert 'LangString LaunchText ${LANG_SIMPCHINESE} "启动 COWMATA Annotator"' in script
    assert 'InstallDir "$LOCALAPPDATA\\Programs\\COWMATA Annotator"' in script
    assert 'Call un.CheckNotInUse' in script
    assert '--check-running' in script
    assert '$(UninstallRetained)' in script


def test_only_owned_generated_files_are_in_uninstall_plan():
    import importlib.util
    spec = importlib.util.spec_from_file_location("installer_builder", ROOT / "scripts/build_installer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    listing = "\n".join(module.uninstall_listing(["lib/foo.py", "app.py", "runtime/core.dll"]))
    assert 'lib\\__pycache__\\foo.*.pyc' in listing
    assert '__pycache__\\app.*.pyc' in listing
    assert 'logs\\annotator.log' in listing
    assert 'RMDir /r' not in listing and '\\*.pyc' not in listing
    assert listing.index("Call un.CheckDirectory") < listing.index("Delete ")
    assert listing.index("Call un.CheckDeleteErrors") < listing.index("RMDir ")
    import pytest
    for path in ("../user.json", "C:/user.json", "lib/*.py", 'bad"$path.py'):
        with pytest.raises(ValueError):
            module.uninstall_listing([path])


def test_launchers_disable_bytecode_explicitly():
    launcher = (ROOT / "packaging/Launcher.cs").read_text(encoding="utf-8")
    bat = (ROOT / "START_ANNOTATOR.bat").read_text(encoding="utf-8")
    assert 'new StringBuilder("-I -B ")' in launcher
    assert 'InstallationIsRunning' in launcher and 'process.MainModule.FileName' in launcher
    assert 'pythonw.exe" -I -B' in bat
    assert 'sys.dont_write_bytecode = True' in (ROOT / "portable_start.py").read_text(encoding="utf-8")


def test_about_identity_and_no_unimplemented_update_claim():
    from PySide6.QtWidgets import QApplication, QTextBrowser

    from cowmata_tailring.ui.about import COMPANY, create_about
    app = QApplication.instance() or QApplication([])
    dialog = create_about()
    bodies = "\n".join(widget.toPlainText() for widget in dialog.findChildren(QTextBrowser))
    assert COMPANY == "杨凌园上园智能科技有限公司"
    from cowmata_tailring import __build__, __version__
    assert __version__ in bodies and __build__ in bodies
    assert "支持自动检查" in bodies
    assert "MIT License" in bodies and "Copyright (c) 2026 zxq309" in bodies
    dialog.close()
    assert app is not None


def test_actual_windows_process_identity():
    import ctypes
    import sys

    import pytest

    from cowmata_tailring.app.windows_identity import APP_USER_MODEL_ID, set_taskbar_identity
    if sys.platform != "win32":
        pytest.skip("Windows shell API")
    assert set_taskbar_identity()
    shell = ctypes.WinDLL("shell32")
    get = shell.GetCurrentProcessExplicitAppUserModelID
    get.argtypes = [ctypes.POINTER(ctypes.c_wchar_p)]
    get.restype = ctypes.c_long
    value = ctypes.c_wchar_p()
    assert get(ctypes.byref(value)) == 0
    try:
        assert value.value == APP_USER_MODEL_ID == "Cowmata.Annotator"
    finally:
        free = ctypes.WinDLL("ole32").CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(value, ctypes.c_void_p))


def test_application_icon_is_loadable():
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    icon = QIcon(str(ROOT / "assets/app-icon/cowmata.ico"))
    assert not icon.isNull() and not icon.pixmap(32, 32).isNull()
    assert app is not None
