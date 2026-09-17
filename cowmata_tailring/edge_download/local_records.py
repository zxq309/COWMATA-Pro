"""Non-destructive cross-category index of existing Motion/PPG/Temp originals."""
import hashlib
import json
import os
import re
from pathlib import Path

from .core import MODALITIES, DownloadError, fingerprint, validate_payload
from .deduplication import MAX_JSON_BYTES, _check, _safe, _signature


class LocalRecords:
    def __init__(self, root, db, cancel, log=lambda text: None):
        self.root, self.db, self.cancel, self.log = Path(root).resolve(), db, cancel, log
        db.execute('CREATE TABLE IF NOT EXISTS local_raw_records ('
                   'path TEXT PRIMARY KEY, signature TEXT, kind TEXT, device TEXT, uid TEXT, '
                   'stamp INTEGER, sha TEXT, fingerprint TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS local_raw_uid ON local_raw_records(kind,device,uid)')
        db.execute('CREATE INDEX IF NOT EXISTS local_raw_fingerprint ON local_raw_records(kind,fingerprint)')

    def remember(self, file, kind):
        _check(self.cancel)
        file = Path(file)
        if not _safe(self.root, file) or not file.is_file():
            return None
        relative = file.relative_to(self.root).as_posix()
        before = _signature(file.stat())
        if before[0] > MAX_JSON_BYTES:
            return None
        try:
            raw = file.read_bytes()
            _check(self.cancel)
            if _signature(file.stat()) != before:
                return None
            data = json.loads(raw)
            validate_payload(data, kind)
            device = str(data.get('device', '')).upper()
            if not re.fullmatch('[0-9A-F]{12}', device):
                raise ValueError('缺少完整设备号')
            uid = str(data.get('uid', ''))
            uid = str(int(uid)) if uid.isdigit() and int(uid) > 0 else ''
            row = (relative, json.dumps(before), kind, device, uid, int(data['create_time']),
                   hashlib.sha256(raw).hexdigest(), fingerprint(data, kind))
            self.db.execute('INSERT OR REPLACE INTO local_raw_records VALUES (?,?,?,?,?,?,?,?)', row)
            return row
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.db.execute('DELETE FROM local_raw_records WHERE path=?', (relative,))
            self.log('本地文件保留，未计入已下载：' + relative + '（' + str(exc) + '）')
            return None

    def refresh(self):
        count = 0
        modalities = {v.casefold(): k for k, v in MODALITIES.items()}
        modalities.update(pulse='pulse')
        self.log('先核对本地 Motion、PPG、Temp 原始文件；已有文件和分类位置保持不变…')
        # Walk all categories, including pregnancy subfolders and old device/day layouts.
        for folder, directories, files in os.walk(self.root, followlinks=False):
            _check(self.cancel)
            base = Path(folder)
            directories[:] = [n for n in directories if not n.startswith('.') and _safe(self.root, base / n)]
            parts = base.relative_to(self.root).parts
            kinds = {modalities[p.casefold()] for p in parts if p.casefold() in modalities}
            if len(kinds) != 1:
                continue
            kind = next(iter(kinds))
            for name in files:
                _check(self.cancel)
                if not name.lower().endswith('.json') or name.startswith('.'):
                    continue
                file = base / name
                if not _safe(self.root, file):
                    continue
                relative = file.relative_to(self.root).as_posix()
                cached = self.db.execute('SELECT signature FROM local_raw_records WHERE path=?', (relative,)).fetchone()
                signature = json.dumps(_signature(file.stat()))
                if cached and cached[0] == signature or self.remember(file, kind):
                    count += 1
                if count and count % 200 == 0:
                    self.log(f'已核对 {count} 个本地原始文件…')
        self.db.commit()
        self.log(f'本地核对完成：{count} 个有效文件；开始查询缺失记录。')

    def _find(self, clause, values, lo=None, hi=None):
        rows = self.db.execute('SELECT * FROM local_raw_records WHERE ' + clause + ' ORDER BY path', values).fetchall()
        matches = []
        for row in rows:
            _check(self.cancel)
            file = self.root / row[0]
            if not _safe(self.root, file) or not file.is_file():
                self.db.execute('DELETE FROM local_raw_records WHERE path=?', (row[0],))
                continue
            # Revalidate contents as well as stat before suppressing a network transfer.
            current = self.remember(file, row[2])
            if current is None or current[2:6] != row[2:6] or current[7] != row[7]:
                continue
            if lo is not None and not lo <= current[5] < hi:
                continue
            matches.append((file, current))
        if len({row[7] for _, row in matches}) > 1:
            raise DownloadError('同一设备和 UID 的本地数据内容冲突，保留原件，请核对')
        return matches[0][0] if matches else None

    def find_uid(self, kind, device, uid, lo, hi):
        return self._find('kind=? AND device=? AND uid=?',
                          (kind, device.upper(), str(uid)), int(lo.timestamp()*1000), int(hi.timestamp()*1000))

    def find_data(self, kind, data):
        return self._find('kind=? AND fingerprint=?', (kind, fingerprint(data, kind)))
