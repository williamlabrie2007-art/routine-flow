"""Records the live stream into wall-clock-named MPEG-TS segments.

    streamlink --stdout twitch.tv/<channel> best | ffmpeg -c copy -f segment ...

Each segment is named ``seg_YYYYmmdd-HHMMSS.ts`` (UTC) from the moment ffmpeg
opened it, so any wall-clock time (e.g. a chat spike) maps straight onto the
recording without having to track stream timestamps.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("clipbot.recorder")

SEGMENT_FMT = "seg_%Y%m%d-%H%M%S.ts"


@dataclass
class Segment:
    path: Path
    start: float  # wall-clock epoch seconds


def list_segments(seg_dir: Path) -> list[Segment]:
    out = []
    for p in seg_dir.glob("seg_*.ts"):
        try:
            t = time.strptime(p.name, SEGMENT_FMT)
        except ValueError:
            continue
        out.append(Segment(p, float(calendar.timegm(t))))
    out.sort(key=lambda s: s.start)
    return out


class Recorder:
    def __init__(
        self,
        channel: str,
        seg_dir: Path,
        quality: str = "best",
        segment_time: int = 6,
        input_url: str | None = None,
        streamlink_args: list[str] | None = None,
        retry_delay: int = 30,
    ):
        self.channel = channel
        self.seg_dir = seg_dir
        self.quality = quality
        self.segment_time = segment_time
        self.input_url = input_url  # bypass streamlink: any URL/file ffmpeg can read
        self.streamlink_args = streamlink_args or []
        self.retry_delay = retry_delay
        self.live = asyncio.Event()
        self.finished = asyncio.Event()
        self._procs: list[asyncio.subprocess.Process] = []
        seg_dir.mkdir(parents=True, exist_ok=True)

    def _ffmpeg_cmd(self, src: str) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.input_url:
            cmd += ["-re"]  # play local files at real-time speed
        cmd += [
            "-i", src,
            "-map", "0", "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.segment_time),
            "-segment_format", "mpegts",
            "-strftime", "1",
            str(self.seg_dir / SEGMENT_FMT),
        ]
        return cmd

    async def _run_once(self) -> int:
        env = {**os.environ, "TZ": "UTC"}  # segment names are UTC
        if self.input_url:
            ff = await asyncio.create_subprocess_exec(*self._ffmpeg_cmd(self.input_url), env=env)
            self._procs = [ff]
            self.live.set()
            return await ff.wait()

        if not shutil.which("streamlink"):
            raise RuntimeError("streamlink not found - pip install streamlink")
        r, w = os.pipe()
        sl = await asyncio.create_subprocess_exec(
            "streamlink", "--stdout", "--loglevel", "warning",
            "--twitch-disable-ads", *self.streamlink_args,
            f"https://twitch.tv/{self.channel}", self.quality,
            stdout=w,
        )
        os.close(w)
        ff = await asyncio.create_subprocess_exec(*self._ffmpeg_cmd("pipe:0"), stdin=r, env=env)
        os.close(r)
        self._procs = [sl, ff]

        async def mark_live():
            # consider the stream live once the first segment shows up
            while ff.returncode is None:
                if list_segments(self.seg_dir):
                    self.live.set()
                    return
                await asyncio.sleep(1)

        watcher = asyncio.create_task(mark_live())
        await sl.wait()
        rc = await ff.wait()
        watcher.cancel()
        return sl.returncode or rc

    async def run(self, wait_for_live: bool = True, forever: bool = False) -> None:
        """Record one broadcast. wait_for_live: poll while offline. forever: also wait for the next broadcast."""
        try:
            while True:
                before = len(list_segments(self.seg_dir))
                try:
                    rc = await self._run_once()
                except RuntimeError as e:
                    log.error("%s", e)
                    return
                got_video = len(list_segments(self.seg_dir)) > before
                if self.input_url:
                    log.info("input finished (rc=%s)", rc)
                    return
                if got_video:
                    log.info("stream ended or dropped (rc=%s)", rc)
                    self.live.clear()
                    if not forever:
                        # a short grace retry covers network blips mid-stream
                        await asyncio.sleep(5)
                        before = len(list_segments(self.seg_dir))
                        await self._run_once()
                        if len(list_segments(self.seg_dir)) == before:
                            return
                        continue
                elif not wait_for_live:
                    log.info("channel is offline")
                    return
                log.info("waiting %ss for %s to be live...", self.retry_delay, self.channel)
                await asyncio.sleep(self.retry_delay)
        finally:
            self.finished.set()

    async def stop(self) -> None:
        for p in self._procs:
            if p.returncode is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass
        for p in self._procs:
            try:
                await asyncio.wait_for(p.wait(), 10)
            except asyncio.TimeoutError:
                p.kill()

    def prune(self, keep_seconds: float, now: float | None = None) -> int:
        """Delete segments that ended more than keep_seconds ago. Returns count removed."""
        now = now if now is not None else time.time()
        segs = list_segments(self.seg_dir)
        removed = 0
        for seg, nxt in zip(segs, segs[1:]):
            if nxt.start < now - keep_seconds:
                seg.path.unlink(missing_ok=True)
                removed += 1
        return removed
