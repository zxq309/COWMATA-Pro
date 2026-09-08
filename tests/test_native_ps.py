from datetime import datetime

import pytest

from cowmata_tailring.media.dahua_stream import _encode_mpeg_timestamp
from cowmata_tailring.media.native_ps import (
    EPOCH,
    hk_clock,
    native_hint,
    packets,
    pts_value,
    read_native_index,
    shenmo_clock,
)
from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
from cowmata_tailring.workspace.demand import next_video_task

PACK = bytes.fromhex('000001ba440004000401001003f8')


def hk(stamp):
    d = datetime.fromisoformat(stamp)
    bits = ((d.year-2000)<<40 | d.month<<36 | d.day<<31 | d.hour<<26 | d.minute<<20 | d.second<<14)
    descriptor = b'\x40\x0eHK\x01\x00' + bits.to_bytes(6,'big') + b'\0'*4
    body = b'\xfb\xff' + len(descriptor).to_bytes(2,'big') + descriptor + b'\0'*6
    return b'\0\0\1\xbc' + len(body).to_bytes(2,'big') + body


def pes(ms, wall=None, key=True):
    optional = _encode_mpeg_timestamp(ms*90,2)
    if wall is not None:
        optional += b'\x01\x89\x80' + int(wall).to_bytes(8,'little')
    payload = b'\0\0\0\1' + (b'\x67' if wall is not None else b'\x40') if key else b'\0\0\1\x02'
    body = bytes((0x80,0x80 | int(wall is not None),len(optional))) + optional + payload + b'test'
    return b'\0\0\1\xe0' + len(body).to_bytes(2,'big') + body


@pytest.mark.parametrize('ms',[0,70,1000,12345,60000000])
def test_pts_markers(ms):
    assert pts_value(_encode_mpeg_timestamp(ms*90,2)) == pytest.approx(ms)
    with pytest.raises(ValueError):
        pts_value(b'\0'*5)


def test_hk_calendar_and_invalid_descriptor():
    packet=hk('2026-08-03 11:34:57')
    assert hk_clock(packet) == (datetime(2026,8,3,11,34,57)-EPOCH).total_seconds()*1000
    assert hk_clock(packet[:15]) is None
    assert hk_clock(packet.replace(b'HK',b'NO')) is None


def test_shenmo_timezone_explicit_not_windows_clock():
    raw=1785728621689
    header=pes(0,raw)
    assert shenmo_clock(header,480)==raw+8*3600000
    assert shenmo_clock(header,0)==raw
    assert shenmo_clock(header,-300)==raw-5*3600000
    assert shenmo_clock(header[:16]) is None


def test_full_native_and_bounded_trailer(tmp_path):
    path=tmp_path/'001.mp4'
    path.write_bytes(b''.join(PACK+hk(f'2026-08-03 11:34:{57+i:02d}')+pes(i*1000) for i in range(3))+b'padding'*30)
    index=read_native_index(path)
    assert index['duration_ms']==3000
    assert len(index['keys'])==3
    assert index['valid_end']<path.stat().st_size
    assert native_hint(path)['start_ms']==index['wall_start']
    stat=path.stat()
    tl=MediaTimelineIndex(str(path),stat.st_size,stat.st_mtime_ns,0,1000,(TimelineSegment(0,3000,0,3000),),(),native=index)
    assert MediaTimelineIndex.from_dict(tl.to_dict()).native==index


def test_private_timestamp_jump_falls_back_not_interpolated(tmp_path):
    path=tmp_path/'001.mp4'
    path.write_bytes(PACK+hk('2026-08-03 11:34:57')+pes(0)+PACK+hk('2026-08-03 12:34:58')+pes(1000))
    with pytest.raises(ValueError,match='时钟跳变'):
        read_native_index(path)


def test_shenmo_monotonic_clock_and_seek_candidates(tmp_path):
    path=tmp_path/'001.mp4'
    path.write_bytes(b''.join(PACK+pes(i*70,1785728621689+i*70) for i in range(3)))
    result=read_native_index(path)
    assert result['wall_start']==1785728621689+8*3600000
    assert result['duration_ms']==210
    assert result['family']=='shenmo-pes2-epoch'


def test_no_marker_search_inside_payload_and_cancel(tmp_path):
    data=PACK+pes(0)+b'corrupt'+PACK+pes(1000)
    assert list(packets(data))[-1][2]==len(PACK+pes(0))
    with pytest.raises(InterruptedError):
        list(packets(data,cancelled=lambda:True))
    path=tmp_path/'unknown.mp4'
    path.write_bytes(b'mp4-like')
    assert native_hint(path) is None
    assert read_native_index(path) is None


def test_native_miss_still_runs_ocr_and_closed_span_not_predecessor():
    rows=[dict(path='cam/001.mp4',kind='video',asset_id=None,state='pending',metadata={})]
    assert next_video_task(rows,{'cam/001.mp4':{'native_checked':True}},100,200)[0]=='hint'
    assert next_video_task(rows,{'cam/001.mp4':{'start_ms':0,'end_ms':50}},100,200) is None


def test_filename_order_does_not_exclude_overlapping_native_spans():
    rows=[dict(path=f'cam/{i:03d}.mp4',kind='video',asset_id=None,state='pending',metadata={}) for i in range(3)]
    hints={rows[0]['path']:{'start_ms':200,'end_ms':400}, rows[1]['path']:{'start_ms':0,'end_ms':350},
           rows[2]['path']:{'start_ms':900,'end_ms':1000}}
    task=next_video_task(rows,hints,300,310)
    assert task[0]=='full' and task[1]['path'] in [r['path'] for r in rows[:2]]


def test_bind_validated_location_keeps_content_clock_and_updates_only_stamp():
    import json

    from cowmata_tailring.workspace.catalog import bind_location_metadata
    raw={'timeline':{'source':{'size':256,'mtimeNs':1},'native':{'source_size':256,'source_mtime_ns':1,'wall_start':123}}}
    rebound=bind_location_metadata(raw,json.dumps([256,7,8,9]))
    assert rebound['timeline']['native']['source_mtime_ns']==7
    assert rebound['timeline']['source']['mtimeNs']==7
    assert rebound['timeline']['native']['wall_start']==123
    assert raw['timeline']['native']['source_mtime_ns']==1
    with pytest.raises(ValueError):
        bind_location_metadata(raw,json.dumps([123,7,8,9]))
