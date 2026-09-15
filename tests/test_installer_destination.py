"""Run the production NSIS destination resolver against an isolated registry."""
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["explicit", "stage", "register", "automatic"])
def test_native_installer_preserves_requested_destination(tmp_path, mode):
    if sys.platform != "win32":
        pytest.skip("Windows installer")
    compiler = os.environ.get("COWMATA_NSIS_COMPILER") or shutil.which("makensis")
    if not compiler:
        pytest.skip("NSIS compiler is not installed")
    import winreg

    registry = "Software\\COWMATA-Installer-QA\\" + uuid.uuid4().hex
    version_key = registry + "\\COWMATA-99.0.0"
    registered = tmp_path / "registered default"
    registered.mkdir()
    (registered / "COWMATA.exe").write_bytes(b"fixture")
    (registered / "COWMATA.install-id").write_text("COWMATA-99.0.0", encoding="utf-8")
    exact = tmp_path / "explicit other install"
    output = tmp_path / "destination.txt"
    executable = tmp_path / "destination.exe"
    source = (ROOT / "packaging/installer.nsi").read_text(encoding="utf-8")
    on_init = source.split("Function .onInit\n", 1)[1].split("FunctionEnd", 1)[0]
    on_init = on_init.replace(
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall", registry)
    script = '\n'.join([
        "Unicode true", '!include "LogicLib.nsh"', '!include "x64.nsh"',
        '!include "FileFunc.nsh"', '!include "nsDialogs.nsh"', '!include "WordFunc.nsh"',
        f'OutFile "{executable}"', 'InstallDir "$TEMP\\empty-default"',
        "RequestExecutionLevel user", "SilentInstall silent", "AutoCloseWindow true",
        *["Var " + name for name in (
            "DesktopEnabled", "StageOnly", "RegisterOnly", "ParentPath", "FolderName")],
        "Function .onInit", on_init, "FunctionEnd", "Section",
        f'FileOpen $0 "{output}" w', 'FileWriteUTF16LE $0 "$INSTDIR"',
        "FileClose $0", "SetErrorLevel 0", "SectionEnd", "",
    ])
    script_path = tmp_path / "destination.nsi"
    script_path.write_text(script, encoding="utf-8")
    subprocess.run([compiler, "/INPUTCHARSET", "UTF8", "/V2", str(script_path)],
                   check=True, timeout=30, creationflags=0x08000000)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, version_key) as key:
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, "99.0.0")
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(registered))
    try:
        args = [str(executable), "/S"]
        if mode == "stage":
            args.append("/STAGE=1")
        if mode == "register":
            args.append("/REGISTERONLY=1")
        command = subprocess.list2cmdline(args)
        if mode != "automatic":
            command += " /D=" + str(exact)  # NSIS requires this unquoted and last.
        result = subprocess.run(command, timeout=30, creationflags=0x08000000)
        assert result.returncode == 0
        chosen = Path(output.read_text(encoding="utf-16-le"))
        assert chosen == (registered if mode == "automatic" else exact)
        assert not exact.exists()  # The probe only resolves the destination.
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, version_key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry)
