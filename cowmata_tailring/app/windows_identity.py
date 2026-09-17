"""Identify the hosted Python GUI as our product, not as Python, on Windows."""
import ctypes
import sys

APP_USER_MODEL_ID = "Cowmata.Annotator"


def set_taskbar_identity():
    if sys.platform != "win32":
        return False
    function = ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_long
    result = function(APP_USER_MODEL_ID)
    if result < 0:
        raise OSError(f"Taskbar application identity failed: 0x{result & 0xffffffff:08x}")
    return True
