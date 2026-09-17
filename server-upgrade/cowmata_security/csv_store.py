"""Locked, atomic CSV persistence; no authorization database is created."""
from __future__ import annotations
import csv
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

FIELDS=('kind','id','account','role','product','expires','consumed','revoked','active','count','until','value','at','actor','action','subject')

class CsvStore:
    def __init__(self,path):
        self.path=Path(path).resolve()
        if self.path.suffix.lower()!='.csv':raise ValueError('Authorization storage must be a CSV file')
    @contextmanager
    def transaction(self,*,create=False):
        if create:self.path.parent.mkdir(parents=True,exist_ok=True)
        if not self.path.parent.is_dir():raise ValueError('Authorization directory is not initialized')
        lock=self.path.with_suffix('.lock')
        with lock.open('a+b') as handle:
            handle.seek(0,2)
            if handle.tell()==0:handle.write(b'0');handle.flush()
            deadline=time.monotonic()+15
            while True:
                try:
                    handle.seek(0)
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic()>=deadline:raise TimeoutError('Authorization file is busy')
                    time.sleep(.025)
            try:
                original=self.path.read_bytes() if self.path.exists() else None
                if self.path.exists():
                    if self.path.stat().st_size>32*1024*1024:raise ValueError('Authorization CSV exceeds limit')
                    with self.path.open(encoding='utf-8-sig',newline='') as stream:
                        reader=csv.DictReader(stream)
                        if reader.fieldnames!=list(FIELDS):raise ValueError('Invalid authority CSV header; restore a verified copy')
                        rows=list(reader)
                    seen=set()
                    for row in rows:
                        if None in row or any(v is None or '\0' in v for v in row.values()):raise ValueError('Corrupt authority CSV')
                        key=(row['kind'],row['id'])
                        if key in seen:raise ValueError('Duplicate authority CSV record')
                        seen.add(key)
                elif create:rows=[]
                else:raise ValueError('Authorization CSV is missing; automatic reinitialization is forbidden')
                before=[dict(r) for r in rows]
                yield rows
                if rows!=before or (create and not self.path.exists()):
                    # An external CSV editor does not honor our lock. Never
                    # overwrite a change observed since this transaction began.
                    latest=self.path.read_bytes() if self.path.exists() else None
                    if latest!=original:raise ValueError('授权 CSV 已被外部修改，本次写入已取消，请重试')
                    self._write(rows,original)
            finally:
                handle.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle,fcntl.LOCK_UN)
    def _write(self,rows,original):
        descriptor,name=tempfile.mkstemp(prefix='.authority-',suffix='.tmp',dir=self.path.parent)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
                stream.flush();os.fsync(stream.fileno())
            deadline=time.monotonic()+1.0
            while True:
                try:
                    latest=self.path.read_bytes() if self.path.exists() else None
                    if latest!=original:
                        raise ValueError('授权 CSV 已被外部修改，本次写入已取消，请重试')
                    os.replace(name,self.path)
                    break
                except PermissionError:
                    if time.monotonic()>=deadline:raise
                    time.sleep(.025)
            if os.name!='nt':
                fd=os.open(self.path.parent,os.O_RDONLY)
                try:os.fsync(fd)
                finally:os.close(fd)
        finally:
            if os.path.exists(name):os.unlink(name)

def record(kind,id,**values):
    return {field:str({'kind':kind,'id':id,**values}.get(field,'')) for field in FIELDS}

def find(rows,kind,id):return next((r for r in rows if r['kind']==kind and r['id']==id),None)
