"""Per-process UI scheduling only; never modifies Windows power/GPU settings."""
import ctypes
import os


def prioritize_ui():
    if os.name != "nt":
        return "normal (non-Windows)"
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetPriorityClass.restype = ctypes.c_int
        # Above-normal remains preemptible. Never use HIGH/REALTIME classes.
        if kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x8000):
            return "above-normal UI; background workers independently limited"
    except (AttributeError, OSError):
        pass
    return "normal (priority request unavailable)"
