"""Immediate recorder-disk reset: keep the DHFS4.1 format, drop every recording.

The wipe rewrites only the per-fragment descriptor tables (plus the first data
fragment of each partition), so the disk keeps its original header, partition
geometry and superblocks — exactly the format the recorder wrote — while its
recording index becomes empty. New recordings therefore start on a clean disk
without waiting for the full classification pass or a hours-long overwrite.
A post-wipe re-scan must enumerate zero recordings before success.
"""
from __future__ import annotations

import ctypes
import struct
import time
from ctypes import wintypes as w

from .dahua_source import DHFSReader, disk_info
from .dahua_tasks import check, task_root
from .storage import ProjectLock

WIPE_CHUNK = 2 * 1024 * 1024
KERNEL = None


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _kernel():
    global KERNEL
    if KERNEL is None:
        KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
        KERNEL.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.HANDLE, w.DWORD, w.DWORD, w.HANDLE]
        KERNEL.CreateFileW.restype = w.HANDLE
        KERNEL.SetFilePointer.argtypes = [w.HANDLE, ctypes.c_long, ctypes.POINTER(ctypes.c_long), w.DWORD]
        KERNEL.SetFilePointer.restype = ctypes.c_long
        KERNEL.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), w.HANDLE]
        KERNEL.FlushFileBuffers.argtypes = [w.HANDLE]
    return KERNEL


def survey(number):
    """Read-only plan for the confirmation dialog; never writes."""
    info = disk_info(number)
    if not info.get("dhfs"):
        raise ValueError("所选磁盘不是 DHFS4.1 录像机原盘；清盘功能只用于录像机原盘")
    with DHFSReader(info["path"], info["size"], info["identity"]) as reader:
        existing = 0
        for part in reader.partitions:
            data = part["descriptors"]
            for index in range(part["count"]):
                desc = data[index * 32:(index + 1) * 32]
                if desc[0] == 1 and u32(desc, 4) != u32(desc, 8):
                    existing += 1
        plan = [dict(partition=part["index"], descriptors=part["count"],
                     descriptor_bytes=part["count"] * 32,
                     descriptor_offset=part["desc_offset"],
                     video_offset=part["video"],
                     fragment=part["fragment"]) for part in reader.partitions]
    return dict(number=info["number"], model=info["model"], serial=info["serial"],
                size=info["size"], identity=info["identity"], letters=info.get("letters", []),
                existing_recordings=existing, partitions=plan)


def _dismount_volumes(letters):
    """Mounted volume sectors reject raw writes; dismount each first."""
    kernel = _kernel()
    for letter in letters:
        handle = kernel.CreateFileW("\\\\.\\\\" + letter, 0xC0000000, 1 | 2, None, 3, 0, None)
        if handle == w.HANDLE(-1).value:
            continue
        try:
            if not kernel.DeviceIoControl(handle, 0x00090020, None, 0, None, 0, None, None):
                raise OSError(f"无法卸载卷 {letter}；请关闭使用该盘的程序后重试")
        finally:
            kernel.CloseHandle(handle)


def _write_zeros(handle, offset, length, label, progress, cancelled):
    kernel = _kernel()
    high = ctypes.c_long(offset >> 32)
    low = kernel.SetFilePointer(handle, ctypes.c_long(offset & 0xFFFFFFFF), ctypes.byref(high), 0)
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        raise OSError(ctypes.get_last_error(), f"定位失败：{label}")
    done = 0
    full = bytes(WIPE_CHUNK)
    written = w.DWORD()
    while done < length:
        check(cancelled)
        size = min(WIPE_CHUNK, length - done)
        payload = full if size == WIPE_CHUNK else bytes(size)
        if not kernel.WriteFile(handle, payload, size, ctypes.byref(written), None) or written.value != size:
            raise OSError(ctypes.get_last_error(), f"写入失败：{label}")
        done += size
        progress(done, length, label)


def wipe(number, expected_identity, progress=lambda *_: None, cancelled=lambda: False):
    """Reset the recorder disk to its original empty DHFS4.1 format."""
    info = disk_info(number)
    if info["identity"] != expected_identity:
        raise ValueError("磁盘身份已变化；请刷新原盘后重新选择")
    if not info.get("dhfs"):
        raise ValueError("所选磁盘不是 DHFS4.1 录像机原盘；已拒绝清盘")
    if any(letter.upper() == "C:" for letter in info.get("letters", [])):
        raise ValueError("该磁盘包含系统卷，已拒绝清盘")
    lockdir = task_root() / "disk-locks"
    lockdir.mkdir(parents=True, exist_ok=True)
    device_lock = ProjectLock(lockdir / (info["identity"] + ".lock"))
    if not device_lock.acquired:
        device_lock.close()
        raise ValueError("此原盘正在被归类任务读取；请先暂停任务再清盘")
    try:
        with DHFSReader(info["path"], info["size"], info["identity"], cancelled,
                        lazy_descriptors=True) as reader:
            jobs = []
            for part in reader.partitions:
                jobs.append(dict(label=f"分区{part['index']} 描述符表",
                                 offset=part["desc_offset"], length=part["count"] * 32))
                jobs.append(dict(label=f"分区{part['index']} 数据首块",
                                 offset=part["video"], length=part["fragment"]))
            total = sum(job["length"] for job in jobs)
        _dismount_volumes(info.get("letters", []))
        kernel = _kernel()
        handle = kernel.CreateFileW(r"\\.\PhysicalDrive" + str(int(number)),
                                    0xC0000000, 1 | 2, None, 3, 0, None)
        if handle == w.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if error in {5, 32}:
                raise OSError(error, "无法以写入方式打开原盘（需要管理员权限，且不能有程序占用该盘）")
            raise OSError(error, "无法打开原盘执行清盘")
        try:
            done = 0
            for job in jobs:
                base = done
                # _write_zeros reports the local byte count, the local total,
                # and the label.  The previous adapter accepted only
                # ``current`` and therefore every real wipe failed before the
                # first write with ``takes from 1 to 2 positional arguments
                # but 3 were given``.  Keep the extra values explicit so the
                # callback contract remains stable if progress reporting is
                # extended again.
                def step(current, _local_total=None, _label=job["label"]):
                    progress(base + current, total, _label)
                _write_zeros(handle, job["offset"], job["length"], job["label"], step, cancelled)
                done += job["length"]
            if not kernel.FlushFileBuffers(handle):
                raise OSError(ctypes.get_last_error(), "清盘数据落盘失败")
        finally:
            kernel.CloseHandle(handle)
        time.sleep(0.2)
        with DHFSReader(info["path"], info["size"], info["identity"], cancelled) as reader:
            remaining = 0
            for part in reader.partitions:
                data = part["descriptors"]
                for index in range(part["count"]):
                    desc = data[index * 32:(index + 1) * 32]
                    if desc[0] == 1 and u32(desc, 4) != u32(desc, 8):
                        remaining += 1
        if remaining:
            raise ValueError(f"清盘后仍发现 {remaining} 条录像索引；请检查磁盘或重试")
        return dict(wiped_bytes=total, partitions=len(jobs) // 2,
                    remaining_recordings=remaining, identity=info["identity"])
    finally:
        device_lock.close()
