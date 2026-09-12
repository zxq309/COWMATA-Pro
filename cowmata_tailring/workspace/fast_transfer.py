"""Bounded read-ahead, HDD batching and verified resume for task-owned partials.

Independent implementation informed by FastCopy's public I/O description.
No FastCopy binary, source, license key or privileged allocation is required.
Python buffering is disabled; Windows filesystem caching remains enabled.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

BLOCK_SIZE = 8 * 1024 * 1024


@lru_cache(maxsize=32)
def drive_profile(anchor):
    """Read media properties without administrator privileges; unknown is safe."""
    result = {'volume': anchor, 'disk': None, 'seek_penalty': None}
    if os.name != 'nt' or not anchor or anchor.startswith('\\\\'):
        return result
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW('\\\\.\\' + anchor.rstrip('\\/'), 0, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return result
    try:
        output, count = ctypes.create_string_buffer(1024), wintypes.DWORD()
        if kernel.DeviceIoControl(handle, 0x560000, None, 0, output, 1024, ctypes.byref(count), None) and count.value >= 12:
            # VOLUME_DISK_EXTENTS: DWORD count, 8-byte-aligned DISK_EXTENT.
            if int.from_bytes(output.raw[:4], 'little') == 1:
                result['disk'] = int.from_bytes(output.raw[8:12], 'little')
        query = (wintypes.DWORD * 3)(7, 0, 0)  # StorageDeviceSeekPenaltyProperty
        if kernel.DeviceIoControl(handle, 0x2D1400, query, 12, output, 1024, ctypes.byref(count), None) and count.value >= 9:
            result['seek_penalty'] = bool(output.raw[8])
    finally:
        kernel.CloseHandle(handle)
    return result


def transfer_policy(source, destination):
    first, second = (drive_profile(str(Path(p).resolve().anchor)) for p in (source, destination))
    if first['seek_penalty'] is False and second['seek_penalty'] is False:
        return 'parallel'
    if first['disk'] is not None and second['disk'] is not None and first['disk'] != second['disk']:
        return 'parallel'
    return 'bulk'


def copy_verified(source, partial, expected_sha, *, cancelled=lambda: False,
                  progress=lambda *_: None, policy=None, block_size=BLOCK_SIZE):
    """Copy to a private partial, resume checked bytes, flush and re-read target.

    The caller holds a source write lock and owns the final non-overwrite rename.
    A cancellation retains the partial; it is never presented as a completed file.
    """
    source, partial = Path(source), Path(partial)
    if source.resolve() == partial.resolve() or partial.is_symlink():
        raise ValueError('复制临时文件路径不安全')
    if partial.exists() and (partial.stat().st_nlink > 1 or not partial.is_file()):
        raise ValueError('复制临时文件不是独立的普通文件')
    if block_size <= 0:
        raise ValueError('Invalid block size')
    policy = policy or transfer_policy(source, partial)
    if policy not in {'parallel', 'bulk'}:
        raise ValueError('Invalid copy policy')
    started = time.monotonic()
    size = source.stat().st_size
    hasher = hashlib.sha256()
    offset = 0

    def check():
        if cancelled():
            raise InterruptedError('整理已暂停，已复制部分保留，下次校验后续传')

    with source.open('rb', buffering=0) as inp:
        if partial.exists() and partial.stat().st_size <= size:
            with partial.open('rb', buffering=0) as saved:
                while block := saved.read(block_size):
                    check()
                    if inp.read(len(block)) != block:
                        offset = 0
                        hasher = hashlib.sha256()
                        break
                    hasher.update(block)
                    offset += len(block)
        inp.seek(offset)
        resumed = offset
        with partial.open('r+b' if partial.exists() else 'xb', buffering=0) as out:
            out.truncate(offset)
            out.seek(offset)

            def write(block):
                nonlocal offset
                check()
                hasher.update(block)
                remaining = memoryview(block)
                while remaining:
                    count = out.write(remaining)
                    if not count:
                        raise OSError('写入未完成，请检查磁盘空间或连接')
                    remaining = remaining[count:]
                offset += len(block)
                progress(offset, size, 'copy')

            if policy == 'parallel':
                # One bounded read-ahead task overlaps source reads with
                # destination writes and hashing; never queues entire videos.
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix='cowmata-read') as pool:
                    future = pool.submit(inp.read, block_size)
                    while block := future.result():
                        check()
                        future = pool.submit(inp.read, block_size)
                        write(block)
            else:
                # Read up to 64 MiB, then write the batch to reduce HDD seeks.
                while True:
                    batch = []
                    for _ in range(max(1, (64 * 1024 * 1024) // block_size)):
                        check()
                        block = inp.read(block_size)
                        if not block:
                            break
                        batch.append(block)
                    if not batch:
                        break
                    for block in batch:
                        write(block)
            out.flush()
            os.fsync(out.fileno())
    if offset != size or hasher.hexdigest() != expected_sha:
        raise ValueError('复制期间来源内容发生变化，未发布目标文件')
    verified = hashlib.sha256()
    with partial.open('rb', buffering=0) as saved:
        count = 0
        while block := saved.read(block_size):
            check()
            verified.update(block)
            count += len(block)
            progress(count, size, 'verify')
    if verified.hexdigest() != expected_sha:
        raise ValueError('复制后内容校验不一致，原件和临时文件保留')
    return {'policy': policy, 'resumed_bytes': resumed, 'written_bytes': size-resumed,
            'verified_bytes': size, 'seconds': time.monotonic()-started}
