"""Verify actual shell properties and non-interactive installer helper failures."""
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows shell integration')
ROOT = Path(__file__).resolve().parents[1]
# Windows PowerShell must discover its own modules when tests were launched
# from PowerShell 7, whose inherited module path is not compatible with 5.1.
PS_ENV = {k: v for k, v in os.environ.items() if k.lower() != 'psmodulepath'}


@pytest.fixture(scope='module')
def launcher(tmp_path_factory):
    directory = tmp_path_factory.mktemp('native-shortcuts')
    target = directory / 'COWMATA.exe'
    build = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(ROOT/'scripts/build_launcher.ps1'), '-Output', str(target)],
                   capture_output=True, timeout=60, env=PS_ENV)
    assert build.returncode == 0, (build.stdout + build.stderr).decode('utf-8', errors='replace')
    return target


def test_invalid_shortcut_helper_exits_without_modal_dialog(launcher):
    result = subprocess.run([str(launcher), '--register-shortcut', str(launcher.parent/'missing.lnk')],
                            capture_output=True, timeout=10)
    assert result.returncode == 1
    assert result.stderr


@pytest.mark.parametrize('encrypted', [False, True])
def test_shortcut_registration_preserves_target_and_writes_app_id(launcher, tmp_path, encrypted):
    link = tmp_path/'COWMATA Pro™.lnk'
    seed = tmp_path/'seed.lnk'
    script = tmp_path/'shell.ps1'
    script.write_text('''param($Link, $Target, $Mode)
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($Link)
if ($Mode -eq 'create') {
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = '--version'
    $shortcut.IconLocation = "$Target,0"
    $shortcut.Save()
} else {
    $folder = (New-Object -ComObject Shell.Application).Namespace((Split-Path -Parent $Link))
    $item = $folder.ParseName((Split-Path -Leaf $Link))
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    @{ target=$shortcut.TargetPath; arguments=$shortcut.Arguments; app_id=$item.ExtendedProperty('System.AppUserModel.ID') } | ConvertTo-Json -Compress
}
''', encoding='utf-8-sig')
    # WScript's legacy path conversion cannot create a TM filename on a GBK
    # Windows host. NTFS and our Unicode shell helper must still support it.
    command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script), str(seed), str(launcher)]
    subprocess.run(command+['create'], check=True, capture_output=True, timeout=20, env=PS_ENV)
    seed.rename(link)
    if encrypted:
        encrypt = ctypes.WinDLL('advapi32', use_last_error=True).EncryptFileW
        encrypt.argtypes = [ctypes.c_wchar_p]
        if not encrypt(str(link)):
            pytest.skip(f'EFS unavailable on the test volume: Win32 {ctypes.get_last_error()}')
    result = subprocess.run([str(launcher), '--register-shortcut', str(link)], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    link.rename(seed)
    try:
        data = subprocess.run(command+['read'], check=True, capture_output=True, timeout=20, env=PS_ENV)
    finally:
        seed.rename(link)
    values = json.loads(data.stdout.decode('utf-8-sig'))
    assert Path(values['target']) == launcher
    assert values['arguments'] == '--version'
    assert values['app_id'] == 'Cowmata.Annotator'
    if encrypted:
        assert link.stat().st_file_attributes & 0x4000, 'EFS encryption must be preserved'
