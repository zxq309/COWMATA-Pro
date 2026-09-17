def test_history_uses_linked_video_root_without_copying_video(tmp_path):
    import hashlib

    from test_resources_v34 import record

    from cowmata_tailring.annotation.data import load_motion_json
    from cowmata_tailring.workspace.catalog import file_stamp
    from cowmata_tailring.workspace.clocks import ClockMap
    from cowmata_tailring.workspace.label_file import (
        build_label_file,
        load_history,
        save_label_file,
    )
    from cowmata_tailring.workspace.work import SessionWork

    raw = record(tmp_path)
    root = raw.parent
    archive = tmp_path / "video-library"
    archive.mkdir()
    video = archive / "one.mp4"
    video.write_bytes(b"video source")
    motion = load_motion_json(raw)
    work = SessionWork(hashlib.sha256(raw.read_bytes()).hexdigest())
    work.clock = ClockMap.from_capture(motion)
    origin = work.clock.map(0)
    row = dict(
        kind="video",
        state="ready",
        asset_id=hashlib.sha256(video.read_bytes()).hexdigest(),
        path="one.mp4",
        stamp=file_stamp(video),
        metadata={
            "camera": "视角01",
            "intervals": [
                dict(
                    wall_start=origin,
                    wall_end=origin + 10000,
                    media_start=0,
                    media_end=10000,
                    verified=True,
                )
            ],
        },
    )
    doc = build_label_file(
        work, motion, root, [row], {"video_archive": {"archive_root_hint": str(archive)}}
    )
    label = root / "label.标注.json"
    save_label_file(label, doc)
    history = load_history(label)
    assert history.motion.sample_count == 4
    assert len(history.rows) == 1
    assert history.root == archive
    assert not (root / "one.mp4").exists()


def test_moved_label_discovers_its_current_category_root(tmp_path):
    import hashlib

    from test_resources_v34 import record

    from cowmata_tailring.annotation.data import load_motion_json
    from cowmata_tailring.workspace.label_file import (
        build_label_file,
        load_history,
        save_label_file,
    )
    from cowmata_tailring.workspace.work import SessionWork

    raw = record(tmp_path)
    root = raw.parent
    for name in ("Motion", "Video", "PPG", "标注工程"):
        (root / name).mkdir()
    work = SessionWork(hashlib.sha256(raw.read_bytes()).hexdigest())
    doc = build_label_file(work, load_motion_json(raw), root, [], {})
    doc["source"]["project_root_hint"] = str(tmp_path / "another-computer")
    path = root / "标注工程/one.标注.json"
    save_label_file(path, doc)
    assert load_history(path).root == root


def test_organized_local_video_precedes_old_external_link(tmp_path):
    import hashlib
    import json

    from test_resources_v34 import record

    from cowmata_tailring.annotation.data import load_motion_json
    from cowmata_tailring.workspace.clocks import ClockMap
    from cowmata_tailring.workspace.label_file import (
        build_label_file,
        load_history,
        save_label_file,
    )
    from cowmata_tailring.workspace.work import SessionWork
    raw=record(tmp_path)
    root=raw.parent
    old=tmp_path/'archive'
    old.mkdir()
    (old/'old.mp4').write_bytes(b'video')
    local=root/'Video/视角01/new.mp4'
    local.parent.mkdir(parents=True)
    local.write_bytes(b'video')
    asset=hashlib.sha256(b'video').hexdigest()
    work=SessionWork(hashlib.sha256(raw.read_bytes()).hexdigest())
    motion=load_motion_json(raw)
    work.clock=ClockMap.from_capture(motion)
    start=work.clock.map(0)
    metadata={'camera':'视角01','intervals':[dict(wall_start=start,wall_end=start+1000,media_start=0,media_end=1000,verified=True)]}
    row=dict(path='old.mp4',asset_id=asset,kind='video',state='ready',stamp='needs_sha',metadata=metadata)
    doc=build_label_file(work,motion,root,[row],{'video_archive':{'archive_root_hint':str(old)}})
    (root/'资源索引.json').write_text(json.dumps({'records':[dict(path=local.relative_to(root).as_posix(),kind='video',
        sha256=asset,size=5,metadata=metadata)]}),encoding='utf-8')
    path=root/'label.标注.json'
    save_label_file(path,doc)
    loaded=load_history(path)
    assert loaded.root==root
    assert loaded.rows[0]['path']==local.relative_to(root).as_posix()
