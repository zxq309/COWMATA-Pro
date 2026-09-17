"""Create historical 3.3 saved plans for replay/recovery regression tests only."""
from datetime import date

from cowmata_tailring.workspace import organization as core


def plan_import(target, sources, start, end=None, note='', cancelled=lambda: False, progress=lambda *_: None, *, category=None):
    core.category_fields(category)
    target = core.safe_path(target)
    start_date, end_date = (date.fromisoformat(start), date.fromisoformat(end or start))
    if end_date < start_date:
        raise ValueError('结束日期不能早于开始日期')
    period = start_date.isoformat() if start_date == end_date else f'{start_date.isoformat()}至{end_date.isoformat()}'
    if not sources:
        raise ValueError('至少选择一份九轴文件或一个素材目录')
    rows, seen, batches, reserved = ([], set(), {}, set())
    token = core.uuid.uuid4().hex
    quarantine = target.parent / '整理隔离' / token
    for spec in sources:
        source = core.safe_path(spec['path'])
        if core.overlaps(source, target):
            raise ValueError('导入来源与目标不能相同或互为父子目录；工程内请用规范视角名称')
        kind = spec['kind']
        camera = spec.get('camera', '')
        if kind not in {'imu', 'video'} or (kind == 'video' and camera not in core.VIEWS):
            raise ValueError('每路录像必须明确选择视角01至视角08')
        for path in core.walk_files(source, cancelled):
            if str(path) in seen:
                raise ValueError('来源重复或互相重叠，请删除重复来源：' + str(path))
            seen.add(str(path))
            row = core.inspect_file(path, kind)
            if row['status'] == 'ready':
                owner = row['folder_name'] if kind == 'imu' else camera
                base = target / '九轴' / owner if kind == 'imu' else target / camera
                row_period = row['record_date'] if kind == 'imu' else period
                if kind == 'imu' and (not start_date.isoformat() <= row_period <= end_date.isoformat()):
                    row.update(status='blocked', message=f'记录采集日期 {row_period} 不在所选日期范围内，请核对本批起止日期；原件保留。')
                    rows.append(row)
                    progress(len(rows), 0, str(path))
                    continue
                key = (str(base), str(path.parent), row_period)
                if key not in batches:
                    batches[key] = base / row_period
                destination = batches[key] / path.name
                if destination.exists() or core.os.path.normcase(str(destination)) in reserved:
                    for number in range(1, 10001):
                        batch = base / f'{row_period}_{number:03d}'
                        candidate = batch / path.name
                        if not candidate.exists() and core.os.path.normcase(str(candidate)) not in reserved:
                            batches[key] = batch
                            break
                    else:
                        raise ValueError('批次编号已用完，请核对目标目录')
                destination = batches[key] / path.name
                reserved.add(core.os.path.normcase(str(destination)))
                row.update(target=str(destination), owner=owner, batch=batches[key].name)
                if core.volume(path) != core.volume(target):
                    row.update(status='blocked', message='跨盘移动需要复制，本功能不执行跨盘搬运')
            elif not row.get('protected') and (row['status'] in {'junk', 'skip'} or row.get('quarantine')):
                row.update(status='quarantine', operation='quarantine', owner='', batch=token, target=str(quarantine / f'{len(rows):06d}' / path.name), message='非素材文件移至工程外隔离；不进入九轴或视角目录')
                if core.volume(path) != core.volume(target):
                    row.update(status='blocked', message='跨盘不能快速隔离，原文件保留')
            rows.append(row)
            progress(len(rows), 0, str(path))
    core.check_identity_ambiguity(rows)
    return {'mode': 'import', 'id': token, 'target': str(target), 'quarantine_root': str(quarantine), 'sources': sources, 'start': start_date.isoformat(), 'end': end_date.isoformat(), 'created_at': core.now(), 'note': note, 'category': category, 'rows': rows}
