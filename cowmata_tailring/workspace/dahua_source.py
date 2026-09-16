"""Read-only DHFS 4.1 / DHAV boundaries. No carving or deleted-record recovery.

The layout is documented in docs/dahua-import-393.md. Disk reads never request
write access. Unknown layouts fail closed instead of scanning for stale DHAV.
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
MAX_PACKET = 32 * 1024**2


def u32(data, offset=0):
    return struct.unpack_from('<I', data, offset)[0]


def packed_ms(value):
    return int(datetime(2000 + (value >> 26), (value >> 22) & 15,
        (value >> 17) & 31, (value >> 12) & 31, (value >> 6) & 63,
        value & 63, tzinfo=TZ).timestamp() * 1000)


def check(cancelled):
    if cancelled():
        raise InterruptedError('任务已暂停，可继续；原始数据保持不变')


def _read_at(stream, offset, count):
    # Windows physical devices require sector-aligned offsets and read lengths.
    aligned = offset - offset % 512
    delta = offset - aligned
    length = ((delta + count + 511) // 512) * 512
    stream.seek(aligned)
    data = stream.read(length)[delta:delta + count]
    if len(data) != count:
        raise OSError('读取不完整：设备断开或录像范围越界')
    return data


def disk_info(number):
    """Windows storage descriptor and size through read-only IOCTLs."""
    if os.name != 'nt':
        raise OSError('物理磁盘读取需要 Windows')
    import ctypes as c
    from ctypes import wintypes as w
    k = c.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p,
                             w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD,
                                 c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    k.CloseHandle.argtypes = [w.HANDLE]
    device = r'\\.\PhysicalDrive' + str(int(number))
    handle = k.CreateFileW(device, 0x80000000, 3, None, 3, 0, None)
    if handle == w.HANDLE(-1).value:
        raise OSError(c.get_last_error(), '无法只读打开磁盘')
    try:
        def ioctl(code, source=b'', size=4096):
            output = c.create_string_buffer(size)
            count = w.DWORD()
            inp = c.create_string_buffer(source) if source else None
            if not k.DeviceIoControl(handle, code, inp, len(source), output,
                                     size, c.byref(count), None):
                raise OSError(c.get_last_error(), '磁盘信息读取失败')
            return output.raw[:count.value]
        length = struct.unpack('<Q', ioctl(0x7405c, size=8))[0]
        desc = ioctl(0x2d1400, bytes(12))
        def field(offset):
            pos = u32(desc, offset)
            return desc[pos:].split(b'\0', 1)[0].decode('ascii', 'replace').strip() if pos else ''
        model = ' '.join(filter(None, (field(12), field(16))))
        serial = field(24)
        with open(device, 'rb', buffering=0) as stream:
            header = _read_at(stream, 0, 512)
            table = _read_at(stream, 0x3c00, 1024)
        signature = hashlib.sha256(header + table).hexdigest()
        stable = hashlib.sha256(f'{model}|{serial}|{length}|{signature}'.encode()).hexdigest()
        return dict(path=device, number=number, model=model, serial=serial,
                    size=length, identity=stable, header_sha256=signature,
                    dhfs=header.startswith(b'DHFS4.1'))
    finally:
        k.CloseHandle(handle)


def disk_letters():
    """Map mounted letters to physical disks without opening the filesystem."""
    if os.name!='nt':return {}
    import ctypes as c
    from ctypes import wintypes as w
    k=c.WinDLL('kernel32',use_last_error=True)
    k.GetLogicalDrives.restype=w.DWORD
    k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
    k.CreateFileW.restype=w.HANDLE
    k.DeviceIoControl.argtypes=[w.HANDLE,w.DWORD,c.c_void_p,w.DWORD,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p]
    k.CloseHandle.argtypes=[w.HANDLE]
    k.SetThreadErrorMode.argtypes=[w.DWORD,c.POINTER(w.DWORD)]
    previous=w.DWORD();changed=k.SetThreadErrorMode(0x8001,c.byref(previous));result={}
    try:
        mask=k.GetLogicalDrives()
        for i in range(26):
            if not mask & (1<<i):continue
            letter=chr(65+i)+':'
            handle=k.CreateFileW('\\\\.\\'+letter,0,3,None,3,0,None)
            if handle==w.HANDLE(-1).value:continue
            try:
                buffer=c.create_string_buffer(4096);used=w.DWORD()
                if k.DeviceIoControl(handle,0x560000,None,0,buffer,len(buffer),c.byref(used),None):
                    count=struct.unpack_from('<I',buffer.raw)[0]
                    for offset in range(8,min(used.value,8+count*24),24):
                        if offset+24<=used.value:
                            number=struct.unpack_from('<I',buffer.raw,offset)[0]
                            result.setdefault(number,[]).append(letter)
            finally:k.CloseHandle(handle)
    finally:
        if changed:k.SetThreadErrorMode(previous,None)
    return result


def disks():
    result = []
    letters = disk_letters()
    for number in range(32):
        try:
            row=disk_info(number);row["letters"]=letters.get(number,[])
            result.append(row)
        except OSError:
            continue
    return result


class DHFSReader:
    def __init__(self, source, size, identity, cancelled=lambda: False):
        self.source, self.size, self.identity = str(source), int(size), identity
        self.cancelled = cancelled
        self.stream = open(source, 'rb', buffering=0)
        self.partitions = []
        try:
            if _read_at(self.stream, 0, 512)[:7] != b'DHFS4.1':
                raise ValueError('不是支持的 DHFS4.1 磁盘或镜像')
            for index in range(64):
                check(cancelled)
                entry = _read_at(self.stream, 0x3c34 + index*64, 64)
                if entry[:4] == b'\xaa\x55\xaa\x55':
                    break
                base = struct.unpack_from('<Q', entry, 48)[0]*512
                sb_offset = base + u32(entry, 20)*512
                if sb_offset < 0 or sb_offset + 512 > self.size:
                    raise ValueError('DHFS 分区超出磁盘边界')
                sb = _read_at(self.stream, sb_offset, 512)
                block, fragments = u32(sb, 44), u32(sb, 48)
                count = u32(sb, 76)
                desc, video = base + u32(sb, 68)*block, base + u32(sb, 72)*block
                fragment = fragments*block
                if (block not in (512, 4096) or fragment != 2097152 or
                    not 0 < count <= 8_000_000 or desc + count*32 > self.size or
                    video + count*fragment > self.size or desc < base):
                    raise ValueError('不支持的 DHFS 布局，停止读取')
                self.partitions.append(dict(index=index, base=base, fragment=fragment,
                    block=block, video=video, count=count, desc_offset=desc,
                    descriptors=_read_at(self.stream, desc, count*32)))
            else:
                raise ValueError('DHFS 分区表缺少结束标记')
            if not self.partitions:
                raise ValueError('DHFS 没有有效分区')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def descriptor(self, part, index):
        if not 0 <= index < part['count']:
            raise ValueError('录像链索引越界')
        return part['descriptors'][index*32:(index+1)*32]

    def chain(self, part, head):
        first = self.descriptor(part, head)
        if first[0] != 1:
            raise ValueError('录像链首描述符类型错误')
        count = int.from_bytes(first[2:4], 'little') + 1
        current, previous, result = head, 0, []
        seen = set()
        for position in range(count):
            check(self.cancelled)
            if current in seen:
                raise ValueError('录像链循环')
            seen.add(current)
            desc = self.descriptor(part, current)
            if position and (desc[0] != 2 or u32(desc, 24) != head or
                             u32(desc, 20) != previous or
                             int.from_bytes(desc[2:4], 'little') != position or
                             desc[1] != first[1]):
                raise ValueError('录像链所属通道、序号或前后链接不一致')
            result.append(current)
            previous, current = current, u32(desc, 12)
        if current not in (0, 0xffffffff):
            raise ValueError('录像链数量不符')
        last_size = u32(first, 16)*part['block']
        if not 0 < last_size <= part['fragment']:
            raise ValueError('录像末片长度无效')
        return result, last_size

    def recordings(self):
        rows = []
        for part in self.partitions:
            data = part['descriptors']
            for index in range(part['count']):
                if index % 10000 == 0:
                    check(self.cancelled)
                desc = data[index*32:(index+1)*32]
                if desc[0] != 1 or u32(desc, 4) == u32(desc, 8):
                    continue
                row = dict(partition=part['index'], descriptor=index,
                           channel_raw=desc[1], channel=str(desc[1]-48+1),
                           source=self.source, source_identity=self.identity,
                           stream='未知', status='indexed')
                row['id'] = hashlib.sha256(f'{self.identity}:{part["index"]}:{index}'.encode()).hexdigest()
                try:
                    chain, last_size = self.chain(part, index)
                    start, end = packed_ms(u32(desc, 4)), packed_ms(u32(desc, 8))
                    if end <= start:
                        raise ValueError('录像索引时间倒置')
                    fingerprint = hashlib.sha256(b''.join(self.descriptor(part, i) for i in chain)).hexdigest()
                    row.update(chain=chain, last_size=last_size, index_start_ms=start,
                               index_end_ms=end, fingerprint=fingerprint)
                except (ValueError, OSError) as exc:
                    row.update(status='invalid', message=str(exc))
                rows.append(row)
        return rows

    def chunks(self, row):
        part = self.partitions[row['partition']]
        chain, last_size = self.chain(part, row['descriptor'])
        fingerprint = hashlib.sha256(b''.join(self.descriptor(part, i) for i in chain)).hexdigest()
        if fingerprint != row['fingerprint']:
            raise ValueError('录像索引已经变化，请重新扫描')
        for position, index in enumerate(chain):
            check(self.cancelled)
            size = last_size if position == len(chain)-1 else part['fragment']
            raw = _read_at(self.stream, part['video'] + index*part['fragment'], size)
            if size == part['fragment']:
                tail = raw[-4096:]
                if (u32(tail, 4) != 0x31755713 or u32(tail, 28) != row['descriptor'] or
                    u32(tail, 8) != (1 if position == 0 else 2)):
                    raise ValueError('录像片尾结构不受支持或属于其他录像')
                raw = raw[:-4096]
            yield raw

    def extract(self, row, destination):
        return normalize_chunks(self.chunks(row), destination, self.cancelled)


class ChunkReader:
    def __init__(self, chunks):
        self.chunks, self.buffer = iter(chunks), bytearray()

    def read(self, size):
        while len(self.buffer) < size:
            try:
                self.buffer.extend(next(self.chunks))
            except StopIteration:
                break
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value


def normalize_chunks(chunks, destination, cancelled=lambda: False):
    """Use DHII's first-frame entry; keep cross-fragment keyframes and tiny packets."""
    stream = ChunkReader(chunks)
    head = stream.read(80)
    prefix = b''
    if head[:4] == b'DHII':
        offset, length, timestamp = u32(head, 64), u32(head, 68), u32(head, 72)
        if not 80 <= offset <= 16*1024**2 or not 32 <= length <= MAX_PACKET:
            raise ValueError('DHII 首帧指针无效，不能用旧残留包替代')
        if len(stream.read(offset - 80)) != offset-80:
            raise ValueError('DHII 首帧越界')
        prefix = stream.read(24)
        if prefix[:4] != b'DHAV' or u32(prefix, 12) != length or u32(prefix, 16) != timestamp:
            raise ValueError('DHII 首帧索引与实际包不一致')
    elif head[:4] == b'DHAV':
        stream.buffer[:0] = head
    else:
        raise ValueError('不是完整 DAV/DHAV 码流；未扫描残留录像')
    count, video_count, start, end = 0, 0, None, None
    digest = hashlib.sha256()
    channels, packet_types = set(), set()
    with open(destination, 'xb') as out:
        while True:
            check(cancelled)
            header = prefix or stream.read(24)
            prefix = b''
            if not header:
                break
            if header == b'\xff'*len(header):
                while True:
                    tail = stream.read(1024**2)
                    if not tail:
                        break
                    if tail != b'\xff'*len(tail):
                        raise ValueError('录像尾部混入未知内容')
                break
            if len(header) != 24 or header[:4] != b'DHAV':
                raise ValueError('DHAV 包边界不连续')
            length = u32(header, 12)
            if not 32 <= length <= MAX_PACKET:
                raise ValueError('DHAV 包长无效')
            body = stream.read(length-24)
            if len(body) != length-24 or body[-8:-4] != b'dhav' or u32(body, len(body)-4) != length:
                raise ValueError('DHAV 包被截断或头尾长度不一致')
            packet = header+body
            out.write(packet)
            digest.update(packet)
            count += 1
            channels.add(header[6])
            packet_types.add(header[4])
            if header[4] in (0xfc, 0xfd):
                moment = packed_ms(u32(header, 16))
                start = moment if start is None else min(start, moment)
                end = moment if end is None else max(end, moment)
                video_count += 1
    if not video_count:
        raise ValueError('没有完整视频帧')
    return dict(sha256=digest.hexdigest(), packets=count, video_packets=video_count,
                start_ms=start, end_ms=end, internal_channels=sorted(channels),
                packet_types=sorted(packet_types), time_basis='unix_epoch_ms',
                timezone_offset_minutes=480)


def normalize_file(source, destination, cancelled=lambda: False):
    with open(source, 'rb') as stream:
        return normalize_chunks(iter(lambda: stream.read(2*1024**2), b''), destination, cancelled)
