"""Windows system folder picker, including multi-selection, off the GUI thread."""

from __future__ import annotations

import os
import queue
import threading


def _windows_folders(hwnd, title, initial, multiple):
    import ctypes as c
    import uuid
    from ctypes import wintypes as w

    class GUID(c.Structure):
        _fields_ = [("bytes", c.c_ubyte * 16)]

    def guid(value):
        return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)

    pointer = c.c_void_p
    hr_type = c.c_long
    ole = c.OleDLL("ole32")
    shell = c.WinDLL("shell32")
    ole.CoInitializeEx.argtypes = [pointer, w.DWORD]
    ole.CoInitializeEx.restype = hr_type
    ole.CoCreateInstance.argtypes = [
        c.POINTER(GUID),
        pointer,
        w.DWORD,
        c.POINTER(GUID),
        c.POINTER(pointer),
    ]
    ole.CoCreateInstance.restype = hr_type
    ole.CoTaskMemFree.argtypes = [pointer]
    shell.SHCreateItemFromParsingName.argtypes = [
        w.LPCWSTR,
        pointer,
        c.POINTER(GUID),
        c.POINTER(pointer),
    ]
    shell.SHCreateItemFromParsingName.restype = hr_type

    def method(obj, slot, restype, *types):
        table = c.cast(obj, c.POINTER(c.POINTER(pointer))).contents
        return c.WINFUNCTYPE(restype, pointer, *types)(table[slot])

    def check(hr):
        if hr < 0:
            raise OSError(f"Windows 目录选择器错误 0x{hr & 0xFFFFFFFF:08X}")

    def release(obj):
        if obj.value:
            method(obj, 2, w.ULONG)(obj)

    check(ole.CoInitializeEx(None, 2))  # This worker owns its STA.
    dialog, items, folder = pointer(), pointer(), pointer()
    try:
        check(
            ole.CoCreateInstance(
                c.byref(guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")),
                None,
                1,
                c.byref(guid("D57C7288-D4AD-4768-BE02-9D969532D960")),
                c.byref(dialog),
            )
        )
        options = w.DWORD()
        check(method(dialog, 10, hr_type, c.POINTER(w.DWORD))(dialog, c.byref(options)))
        # FOS_PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST | DONTADDTORECENT.
        flags = options.value | 0x20 | 0x40 | 0x800 | 0x02000000
        if multiple:
            flags |= 0x200  # FOS_ALLOWMULTISELECT
        check(method(dialog, 9, hr_type, w.DWORD)(dialog, flags))
        check(method(dialog, 17, hr_type, w.LPCWSTR)(dialog, title))
        if initial:
            hr = shell.SHCreateItemFromParsingName(
                os.path.normpath(initial),
                None,
                c.byref(guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")),
                c.byref(folder),
            )
            if hr >= 0:
                check(method(dialog, 12, hr_type, pointer)(dialog, folder))
        hr = method(dialog, 3, hr_type, w.HWND)(dialog, hwnd)
        if hr & 0xFFFFFFFF == 0x800704C7:  # user cancelled
            return []
        check(hr)
        check(method(dialog, 27, hr_type, c.POINTER(pointer))(dialog, c.byref(items)))
        count = w.DWORD()
        check(method(items, 7, hr_type, c.POINTER(w.DWORD))(items, c.byref(count)))
        paths = []
        for index in range(count.value):
            item, text = pointer(), pointer()
            try:
                check(
                    method(items, 8, hr_type, w.DWORD, c.POINTER(pointer))(
                        items, index, c.byref(item)
                    )
                )
                check(
                    method(item, 5, hr_type, w.DWORD, c.POINTER(pointer))(
                        item, 0x80058000, c.byref(text)
                    )
                )
                paths.append(c.wstring_at(text))
            finally:
                if text.value:
                    ole.CoTaskMemFree(text)
                release(item)
        return paths
    finally:
        release(folder)
        release(items)
        release(dialog)
        ole.CoUninitialize()


def choose_folders(parent, title, initial="", *, multiple=False):
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QFileDialog

    if os.name != "nt":
        selected = QFileDialog.getExistingDirectory(parent, title, initial)
        return [selected] if selected else []
    mail = queue.Queue(maxsize=1)

    # Qt and the native picker own different STA threads. Cross-thread owners
    # can deadlock Shell Show; keep the Qt parent disabled until selection returns.
    def run():
        try:
            mail.put((_windows_folders(0, title, initial, multiple), None))
        except Exception as exc:
            mail.put(([], exc))

    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: loop.quit() if not mail.empty() else None)
    enabled = parent.isEnabled() if parent else False
    if parent:
        parent.setEnabled(False)
    try:
        threading.Thread(target=run, name="native-folder-picker", daemon=True).start()
        timer.start()
        loop.exec()
        paths, error = mail.get_nowait()
        if error:
            raise error
        return paths
    finally:
        timer.stop()
        if parent:
            parent.setEnabled(enabled)
