import json

from cowmata_tailring.workspace.catalog import Catalog, file_stamp
from cowmata_tailring.workspace.dataset_access import DatasetLease


def owned_cache(meta):
    folder = meta / 'cache/compatibility'
    folder.mkdir(parents=True, exist_ok=True)
    asset = 'a' * 64
    path = folder / (asset + '.mkv')
    path.write_bytes(b'rebuildable video container')
    (folder / 'manifest.json').write_text(json.dumps({asset: {
        'stamp': file_stamp(path), 'size': path.stat().st_size,
        'last_use': 1, 'method': 'stream_copy',
    }}), encoding='utf-8')
    return path


def owned_cache_with_bad_stamp(meta):
    folder = meta / 'cache/compatibility'
    folder.mkdir(parents=True, exist_ok=True)
    asset = 'b' * 64
    path = folder / (asset + '.mkv')
    path.write_bytes(b'cache changed outside manifest')
    manifest_path = folder / 'manifest.json'
    entries = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    entries[asset] = {
        'stamp': '[0, 0, 0, 0]', 'size': path.stat().st_size,
        'last_use': 1, 'method': 'stream_copy',
    }
    manifest_path.write_text(json.dumps(entries), encoding='utf-8')
    return path


def test_open_after_version_change_cleans_owned_cache_once_and_keeps_work(tmp_path, monkeypatch):
    import cowmata_tailring
    monkeypatch.setattr(cowmata_tailring, '__version__', '4.0.0')
    catalog = Catalog(tmp_path)
    meta = catalog.meta
    catalog.close()
    stale = owned_cache(meta)
    stale_mismatch = owned_cache_with_bad_stamp(meta)
    keep = {}
    for name in ('annotations/human.json', 'video_corrections/manual.json',
                 'evidence/photo.jpg', 'cache/my-video.mkv', 'cache/compatibility/unlisted.mkv'):
        path = meta / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'preserve exact content')
        keep[path] = path.read_bytes()
    original = tmp_path / 'recording.mp4'
    original.write_bytes(b'original source')
    monkeypatch.setattr(cowmata_tailring, '__version__', '4.1.0')
    catalog = Catalog(tmp_path)
    catalog.close()
    assert not stale.exists(), 'An updated version must remove its obsolete derived video cache'
    assert not stale_mismatch.exists(), 'Manifest-owned caches are disposable even after an interrupted write'
    assert all(p.read_bytes() == data for p, data in keep.items())
    assert original.read_bytes() == b'original source'
    current = owned_cache(meta)
    catalog = Catalog(tmp_path)
    catalog.close()
    assert current.exists(), 'Reopening the same version must reuse the newly built cache'


def test_other_live_reader_defers_upgrade_cleanup(tmp_path, monkeypatch):
    import cowmata_tailring
    monkeypatch.setattr(cowmata_tailring, '__version__', '4.0.0')
    catalog = Catalog(tmp_path)
    meta = catalog.meta
    catalog.close()
    stale = owned_cache(meta)
    monkeypatch.setattr(cowmata_tailring, '__version__', '4.1.0')
    with DatasetLease([tmp_path]):
        catalog = Catalog(tmp_path)
        catalog.close()
        assert stale.exists()
    catalog = Catalog(tmp_path)
    catalog.close()
    assert not stale.exists(), 'Deferred cleanup must retry once the old reader releases the project'


def test_version_open_requeues_videos_blocked_only_by_missing_ffmpeg(tmp_path, monkeypatch):
    import cowmata_tailring
    monkeypatch.setattr(cowmata_tailring, '__version__', '4.0.0')
    catalog = Catalog(tmp_path)
    path = tmp_path / 'camera.mp4'
    path.write_bytes(b'video')
    catalog.scan(now=1)
    with catalog.db:
        catalog.db.execute(
            "update locations set state='invalid', error=? where path=?",
            ('便携包缺少 ffmpeg.exe/ffprobe.exe', 'camera.mp4'),
        )
    catalog.close()
    monkeypatch.setattr(cowmata_tailring, '__version__', '4.1.0')
    catalog = Catalog(tmp_path)
    try:
        row = catalog.rows(kind='video')[0]
        assert row['state'] == 'pending'
        assert row['error'] == '等待重新解析：版本更新后已恢复媒体工具'
    finally:
        catalog.close()


def test_version_open_leaves_other_video_parser_errors_visible(tmp_path, monkeypatch):
    import cowmata_tailring
    monkeypatch.setattr(cowmata_tailring, '__version__', '4.0.0')
    catalog = Catalog(tmp_path)
    path = tmp_path / 'camera.mp4'
    path.write_bytes(b'video')
    catalog.scan(now=1)
    with catalog.db:
        catalog.db.execute(
            "update locations set state='invalid', error=? where path=?",
            ('视频流没有可用的 PTS/DTS 时间戳', 'camera.mp4'),
        )
    catalog.close()
    monkeypatch.setattr(cowmata_tailring, '__version__', '4.1.0')
    catalog = Catalog(tmp_path)
    try:
        row = catalog.rows(kind='video')[0]
        assert row['state'] == 'invalid'
        assert row['error'] == '视频流没有可用的 PTS/DTS 时间戳'
    finally:
        catalog.close()
