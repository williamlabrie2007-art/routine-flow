"""Cuts a wall-clock time range out of the recorded segments with ffmpeg."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .recorder import Segment, list_segments

log = logging.getLogger("clipbot.clipper")


def select_segments(segs: list[Segment], start: float, end: float) -> list[Segment]:
    """Segments overlapping [start, end). A segment runs until the next one starts."""
    out = []
    for i, s in enumerate(segs):
        seg_end = segs[i + 1].start if i + 1 < len(segs) else float("inf")
        if seg_end > start and s.start < end:
            out.append(s)
    return out


def range_is_recorded(segs: list[Segment], end: float) -> bool:
    """True once the segment containing `end` is closed (a later one has started)."""
    return bool(segs) and segs[-1].start >= end


async def cut_clip(
    seg_dir: Path,
    start: float,
    end: float,
    out_path: Path,
    reencode: bool = False,
) -> Path | None:
    segs = select_segments(list_segments(seg_dir), start, end)
    if not segs:
        log.warning("no recorded video for %.0f-%.0f", start, end)
        return None
    offset = max(0.0, start - segs[0].start)
    duration = end - max(start, segs[0].start)
    # MPEG-TS is byte-concatenable and timestamps are continuous across
    # segments (we don't reset them), so the concat: protocol is enough.
    src = "concat:" + "|".join(str(s.path) for s in segs)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{offset:.3f}", "-i", src, "-t", f"{duration:.3f}"]
    if reencode:  # frame-accurate cut, slower
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k"]
    else:  # instant, cut snaps to nearest keyframe (~2s on Twitch)
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0 or not out_path.exists():
        log.error("ffmpeg failed cutting %s: %s", out_path.name, err.decode(errors="replace").strip()[-500:])
        return None
    return out_path
