import asyncio
import json
import os
import shutil
import subprocess
import time

import pytest

from clipbot.clipper import cut_clip, range_is_recorded, select_segments
from clipbot.recorder import Segment, list_segments

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def segs(*starts):
    return [Segment(path=None, start=s) for s in starts]


def test_select_segments_overlap():
    s = segs(0, 6, 12, 18)
    assert [x.start for x in select_segments(s, 7, 13)] == [6, 12]
    assert [x.start for x in select_segments(s, 6, 12)] == [6]
    assert [x.start for x in select_segments(s, 20, 30)] == [18]
    assert select_segments(s, -10, -1) == []


def test_range_is_recorded():
    assert not range_is_recorded(segs(0, 6), 10)
    assert range_is_recorded(segs(0, 6, 12), 10)


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


@needs_ffmpeg
def test_cut_clip_across_segments(tmp_path):
    seg_dir = tmp_path / "segments"
    seg_dir.mkdir()
    # 30s test video, keyframe every second, split into 6s TS segments named by fake wall time
    subprocess.check_call([
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=30",
        "-f", "lavfi", "-i", "sine=duration=30", "-c:v", "libx264", "-g", "30", "-c:a", "aac", "-shortest",
        "-f", "segment", "-segment_time", "6", "-segment_format", "mpegts", str(seg_dir / "tmp%03d.ts"),
    ])
    base = 1_700_000_000
    for i, p in enumerate(sorted(seg_dir.glob("tmp*.ts"))):
        p.rename(seg_dir / time.strftime("seg_%Y%m%d-%H%M%S.ts", time.gmtime(base + 6 * i)))
    assert len(list_segments(seg_dir)) == 5

    out = asyncio.run(cut_clip(seg_dir, base + 4, base + 16, tmp_path / "clip.mp4"))
    assert out and out.exists()
    assert abs(duration(out) - 12) < 1.2

    out2 = asyncio.run(cut_clip(seg_dir, base + 4, base + 16, tmp_path / "clip2.mp4", reencode=True))
    assert abs(duration(out2) - 12) < 0.2
