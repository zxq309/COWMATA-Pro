"""Python 3.8-compatible private entry. No GUI imports, pip or network.

Only invoked by the reviewed-pack runner after content verification. This is
resource isolation, not a security sandbox for arbitrary pickle/code uploads.
"""
import ctypes
import os
import runpy
import socket
import sys
from pathlib import Path


def deny_network(*args, **kwargs):
    raise OSError("Offline event worker: network disabled")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ[name] = "2"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel.GetCurrentProcess()
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetPriorityClass(handle, 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        kernel.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        allowed, system = ctypes.c_size_t(), ctypes.c_size_t()
        if kernel.GetProcessAffinityMask(handle, ctypes.byref(allowed), ctypes.byref(system)):
            bits = [1 << i for i in range(64) if allowed.value & (1 << i)]
            if not kernel.SetProcessAffinityMask(handle, sum(bits[-2:])):
                raise OSError("Could not apply event-worker CPU limit")
        else:
            raise OSError("Could not inspect event-worker CPU affinity")
    from importlib.metadata import version
    expected = {"numpy": "1.20.1", "pandas": "1.2.4", "scipy": "1.6.2",
                "scikit-learn": "0.24.1", "joblib": "1.0.1", "numba": "0.53.1"}
    if sys.version_info[:2] != (3, 8) or any(version(k) != v for k, v in expected.items()):
        raise RuntimeError("Portable event runtime version mismatch; restore the complete portable package")
    entry = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(entry.parent))
    sys.argv = [str(entry)] + sys.argv[2:]
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
