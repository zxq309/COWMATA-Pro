"""Exercise the real portable entry point while update transport returns HTTP 403."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError


def main():
    result_path = Path(sys.argv[1]).resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ['LOCALAPPDATA'] = str(result_path.parent / 'startup-appdata')
    os.environ['APPDATA'] = str(result_path.parent / 'startup-roaming')
    from PySide6 import QtWidgets
    from PySide6.QtCore import QSettings, QTimer

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(result_path.parent / 'startup-settings'))
    from cowmata_tailring.app import update_core

    def offline(*_args, **_kwargs):
        raise OSError('Network disabled during startup verification')

    def limited(*_args, **_kwargs):
        raise HTTPError(update_core.API, 403, 'rate limit exceeded', {}, None)

    socket.create_connection = offline
    socket.socket.connect = offline
    update_core.check_update = limited
    checks = {'entrypoint': 'portable_start.main', 'network_disabled': True, 'update_http_status': 403}
    original_app = QtWidgets.QApplication

    class ProbeApplication(original_app):
        def __init__(self, args):
            super().__init__(args)
            self.started = time.monotonic()
            self.checked = False
            self.probe = QTimer(self)
            self.probe.setInterval(100)
            self.probe.timeout.connect(self.inspect_window)
            self.probe.start()

        def inspect_window(self):
            windows = list(self.topLevelWidgets())
            gate = next((w for w in windows if type(w).__name__ == 'StartupUpdateDialog' and w.isVisible()), None)
            if gate is not None:
                checks['error'] = 'A startup update gate blocked the workspace'
                gate.reject()
                self.finish()
                return
            window = next((w for w in windows if hasattr(w, 'stage') and hasattr(w, 'updater') and w.isVisible()), None)
            if window is not None:
                if not self.checked:
                    checks['workspace_visible'] = True
                    checks['startup_seconds'] = round(time.monotonic()-self.started, 3)
                    self.update_started = time.monotonic()
                    self.checked = True
                    window.updater.check()
                    return
                elif not window.updater.busy:
                    checks['visible_after_403'] = window.isVisible() and window.isEnabled()
                    checks['background_status'] = window.updater.status
                    checks['no_startup_dialog'] = True
                    window.close()
                    self.finish()
                    return
            limit = 30 if self.checked else 180
            origin = self.update_started if self.checked else self.started
            if time.monotonic()-origin > limit:
                checks['error'] = 'Startup verification timed out'
                self.finish()

        def finish(self):
            self.probe.stop()
            result_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
            self.closeAllWindows()
            self.quit()

    QtWidgets.QApplication = ProbeApplication
    import portable_start

    sys.argv = ['COWMATA.exe']
    original_hook = sys.excepthook
    try:
        code = portable_start.main()
    finally:
        sys.excepthook = original_hook
    result_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    assert code == 0 and checks.get('workspace_visible') and checks.get('visible_after_403'), checks
    assert '403' in checks.get('background_status', ''), checks


if __name__ == '__main__':
    main()
