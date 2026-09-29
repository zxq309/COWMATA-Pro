"""Distinct Windows identity and shared icon for the COWMATA field-ledger utility."""
import os
from pathlib import Path
APP_ID="COWMATA.FieldLedger"
def identify_process():
    if os.name=="nt":
        import ctypes
        function=ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
        function.argtypes=[ctypes.c_wchar_p];function.restype=ctypes.c_long
        return function(APP_ID)==0
    return False
def configure_application(app,root):
    from PySide6.QtGui import QIcon
    app.setApplicationName("COWMATA Field Ledger");app.setApplicationDisplayName("COWMATA 现场台账");app.setOrganizationName("COWMATA")
    app.setWindowIcon(QIcon(str(Path(root)/"assets"/"ledger.ico")))
    refresh_shortcut_icons(root)


def refresh_shortcut_icons(root):
    """Repair old pins once after an update, including portable upgrades."""
    import json,subprocess
    install=os.environ.get("COWMATA_LEDGER_INSTALL_ROOT")
    helper=Path(root)/"LedgerShell.exe"
    if not install or not helper.is_file():return
    icon=str(Path(root)/"assets"/"ledger.ico")
    try:
        receipt=json.loads((Path(install)/"data/updates/icon-refresh.json").read_text(encoding="utf-8-sig"))
        if os.path.normcase(receipt.get("icon",""))==os.path.normcase(icon) and not receipt.get("errors"):return
    except (OSError,ValueError):pass
    try:subprocess.Popen([str(helper),install,icon],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    except OSError:pass
