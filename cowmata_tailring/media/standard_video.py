"""Export verified continuous camera video to a standard MP4 without replacing sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .ffmpeg_tools import find_ffmpeg
from .native_ps import read_native_index
from .subprocess_tools import run_cancellable


def _sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def export_standard_video(source,target):
    source,target=Path(source).resolve(),Path(target).resolve()
    partial=target.with_name(target.name+'.partial')
    report_path=target.with_name(target.name+'.source.json')
    if source==target or target.exists() or partial.exists() or report_path.exists():
        raise ValueError('不能覆盖原始录像或已有结果，请选择新的输出文件。')
    before=_sha(source)
    native=read_native_index(source)
    if not native:
        raise ValueError('未找到连续且可核验的流内时间，本功能不猜测录像时长。')
    ffmpeg,ffprobe=find_ffmpeg()
    target.parent.mkdir(parents=True,exist_ok=True)
    command=[str(ffmpeg),'-hide_banner','-loglevel','warning','-n','-fflags','+genpts','-i',str(source),
             '-map','0:v:0','-an','-sn','-c:v','copy','-t',f"{native['duration_ms']/1000:.6f}",
             '-avoid_negative_ts','make_zero','-movflags','+faststart','-f','mp4',str(partial)]
    result=run_cancellable(command,timeout=1800)
    if result.returncode:
        raise ValueError('标准 MP4 导出失败，原始录像保留：'+result.stderr.decode('utf-8','replace')[-300:])
    probe=run_cancellable([str(ffprobe),'-v','error','-count_packets','-select_streams','v:0',
        '-show_entries','stream=duration,nb_read_packets,start_time:format=duration','-of','json',str(partial)],timeout=180)
    if probe.returncode:
        raise ValueError('标准 MP4 未通过完整帧数校验，保留临时结果供排查。')
    data=json.loads(probe.stdout)
    stream=data['streams'][0]
    duration=float(stream['duration'])*1000
    frames=int(stream['nb_read_packets'])
    if frames!=native['frame_count'] or abs(duration-native['duration_ms'])>max(200,native['frame_ms']*2):
        raise ValueError('标准 MP4 的帧数或时长与流内记录不一致，未交付为有效结果。')
    if _sha(source)!=before:
        raise ValueError('原始录像在导出期间发生变化，请重试。')
    report=dict(source=str(source),source_sha256=before,output_sha256=_sha(partial),
        duration_ms=duration,video_frames=frames,video_reencoded=False,audio_included=False,
        native_family=native['family'],native_start_ms=native['wall_start'],
        native_valid_end=native['valid_end'],original_unchanged=True,
        note='仅导出已校验的视频画面；原始文件和其音频、私有数据包仍保留。')
    # Hard-link publication refuses replacement on both Windows and POSIX.
    import os
    if os.name=='nt':
        partial.rename(target)
    else:
        os.link(partial,target)
        partial.unlink()
    with report_path.open('x',encoding='utf-8') as output:
        json.dump(report,output,ensure_ascii=False,indent=2)
    return report
