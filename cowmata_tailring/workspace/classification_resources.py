"""Conservative budgets for classification workers; never change global settings."""

from __future__ import annotations

import ctypes
import os
from dataclasses import asdict, dataclass

_GIB = 1024**3
_jobs = []


def resource_snapshot():
    total = available = 2 * _GIB
    if os.name == "nt":

        class Memory(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                ("total", ctypes.c_ulonglong),
                ("available", ctypes.c_ulonglong),
                ("page_total", ctypes.c_ulonglong),
                ("page_available", ctypes.c_ulonglong),
                ("virtual_total", ctypes.c_ulonglong),
                ("virtual_available", ctypes.c_ulonglong),
                ("extended", ctypes.c_ulonglong),
            ]

        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            total, available = memory.total, memory.available
    return dict(logical_cpus=os.cpu_count() or 2, total_bytes=total, available_bytes=available)


@dataclass(frozen=True)
class Budget:
    heavy_workers: int
    copy_workers: int
    cpu_percent: int = 35
    block_bytes: int = 2 * 1024**2
    batch_bytes: int = 8 * 1024**2

    def to_dict(self):
        return asdict(self)


def resource_budget(snapshot=None):
    snapshot = snapshot or resource_snapshot()
    constrained = snapshot["available_bytes"] < 6 * _GIB or snapshot["logical_cpus"] <= 4
    return Budget(heavy_workers=1 if constrained else 2, copy_workers=2 if constrained else 3)


def limit_worker(budget=None):
    """Called only in the private worker. Children inherit its affinity and job."""
    budget = budget or resource_budget()
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[name] = "1"
    result = budget.to_dict() | {"cpu_hard_cap": False, "affinity_cpus": None}
    if os.name != "nt":
        return result
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.SetPriorityClass.argtypes = [w.HANDLE, w.DWORD]
    kernel.GetProcessAffinityMask.argtypes = [
        w.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.SetProcessAffinityMask.argtypes = [w.HANDLE, ctypes.c_size_t]
    process = kernel.GetCurrentProcess()
    kernel.SetPriorityClass(process, 0x4000)
    current, system = ctypes.c_size_t(), ctypes.c_size_t()
    if kernel.GetProcessAffinityMask(process, ctypes.byref(current), ctypes.byref(system)):
        bits = [
            1 << i for i in range(ctypes.sizeof(ctypes.c_size_t) * 8) if current.value & (1 << i)
        ]
        count = max(1, len(bits) * budget.cpu_percent // 100)
        if kernel.SetProcessAffinityMask(process, sum(bits[:count])):
            result["affinity_cpus"] = count

    class CpuRate(ctypes.Structure):
        _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", w.DWORD),
            ("min_ws", ctypes.c_size_t),
            ("max_ws", ctypes.c_size_t),
            ("active", w.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", w.DWORD),
            ("scheduling", w.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if job:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # Kill only this worker's descendants when it exits.
        kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        rate = CpuRate(0x1 | 0x4, budget.cpu_percent * 100)
        configured = kernel.SetInformationJobObject(
            job, 15, ctypes.byref(rate), ctypes.sizeof(rate)
        )
        if kernel.AssignProcessToJobObject(job, process):
            _jobs.append(job)
            result["cpu_hard_cap"] = bool(configured)
        else:
            kernel.CloseHandle(job)
    return result


_slots = []


def acquire_preparation_slot(cancelled=lambda: False, progress=lambda *_: None):
    """Shared admission for old and new preparation workers, outside data roots."""
    import atexit
    import time
    from pathlib import Path

    from .storage import ProjectLock

    root = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "COWMATA Annotator"
        / "preparation-slots"
    )
    root.mkdir(parents=True, exist_ok=True)
    count = resource_budget().heavy_workers
    last = 0
    while True:
        if cancelled():
            raise InterruptedError("已暂停，尚未占用转码资源")
        for i in range(count):
            slot = ProjectLock(root / f"{i}.lock")
            if slot.acquired:
                _slots.append(slot)
                atexit.register(slot.close)
                return slot
            slot.close()
        if time.monotonic() - last > 1:
            progress(0, 0, "等待其他归类任务释放资源；标注仍可使用")
            last = time.monotonic()
        time.sleep(0.1)
