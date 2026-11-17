"""Glue: record the stream, watch chat, clip every spike."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .chat import TWITCH_IRC_PORT, ChatMessage, chat_stream
from .clipper import cut_clip, list_segments, range_is_recorded
from .detector import Spike, SpikeDetector
from .recorder import Recorder

log = logging.getLogger("clipbot")


@dataclass
class Config:
    channel: str
    out_dir: Path = Path("recordings")
    quality: str = "best"
    # detection
    window: int = 10
    history: int = 300
    sensitivity: float = 3.0
    ratio: float = 2.0
    min_messages: int = 10
    warmup: int = 60
    cooldown: int = 20
    max_spike: int = 60
    # clips
    pre_roll: float = 20.0
    post_roll: float = 10.0
    max_clip: float = 90.0
    reencode: bool = False
    # recording
    segment_time: int = 6
    keep_minutes: float = 0  # 0 = keep the full recording
    wait: bool = True
    forever: bool = False
    save_chat: bool = True
    streamlink_args: list[str] = field(default_factory=list)
    # testing / advanced
    input_url: str | None = None
    irc_host: str | None = None  # None = Twitch (WebSocket)
    irc_port: int = TWITCH_IRC_PORT
    irc_tls: bool = True


WORD_RE = re.compile(r"[\w']{2,}", re.UNICODE)
STOPWORDS = set("""
a an and are as at be but by can do for from get got had has have he her him his how i i'm if in is it it's
just like me my no not of on or our out so that that's the their them then there they this to up was we
what when who why will with would you your yeah yes im its dont don't thats u ur he's she she's
""".split())


def summarize(messages: list[ChatMessage], n: int = 5) -> tuple[list[tuple[str, int]], int]:
    """Most used emotes/words (one vote per user per token) and unique chatters."""
    votes: Counter[str] = Counter()
    for m in messages:
        tokens = set(m.emotes) or {w for w in WORD_RE.findall(m.text) if w.lower() not in STOPWORDS}
        votes.update(tokens)
    return votes.most_common(n), len({m.user for m in messages})


def slug(s: str) -> str:
    return re.sub(r"[^\w-]+", "", s)[:24]


class ClipBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.session = cfg.out_dir / cfg.channel.lower() / stamp
        self.seg_dir = self.session / "segments"
        self.clip_dir = self.session / "clips"
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self.detector = SpikeDetector(
            window=cfg.window, history=cfg.history, sensitivity=cfg.sensitivity, ratio=cfg.ratio,
            min_messages=cfg.min_messages, warmup=cfg.warmup, max_duration=cfg.max_spike, cooldown=cfg.cooldown,
        )
        self.recorder = Recorder(
            cfg.channel, self.seg_dir, quality=cfg.quality, segment_time=cfg.segment_time,
            input_url=cfg.input_url, streamlink_args=cfg.streamlink_args,
        )
        self.recent: deque[ChatMessage] = deque()
        self.clip_queue: asyncio.Queue[tuple[Spike, list[ChatMessage]]] = asyncio.Queue()
        self.total_messages = 0
        self.clips_made = 0
        self._stop = asyncio.Event()
        self._chat_file = open(self.session / "chat.jsonl", "a", encoding="utf-8") if cfg.save_chat else None

    # -- tasks ---------------------------------------------------------------
    async def _chat_loop(self) -> None:
        async for msg in chat_stream(self.cfg.channel, self.cfg.irc_host, self.cfg.irc_port, self.cfg.irc_tls):
            # use local receive time: tmi-sent-ts can drift from our clock
            msg.ts = time.time()
            self.total_messages += 1
            self.detector.add(msg.ts)
            self.recent.append(msg)
            if self._chat_file:
                self._chat_file.write(json.dumps({"ts": round(msg.ts, 3), "user": msg.user, "text": msg.text}) + "\n")

    async def _tick_loop(self) -> None:
        last_status = 0.0
        while True:
            await asyncio.sleep(1)
            now = time.time()
            keep = self.cfg.max_spike + self.cfg.window + 30
            while self.recent and self.recent[0].ts < now - keep:
                self.recent.popleft()
            if self._chat_file:
                self._chat_file.flush()
            spike = self.detector.update(now)
            if self.detector.just_started:
                log.info("spike started: %d msgs/%ds (threshold %.0f)", self.detector.last_count,
                         self.cfg.window, self.detector.last_threshold)
            if spike:
                self._enqueue(spike)
            if now - last_status >= 30:
                last_status = now
                mean, _ = self.detector.baseline()
                log.info(
                    "chat %d msgs/%ds | baseline %.1f | trigger at %.0f%s | %d msgs total | %d clips",
                    self.detector.last_count, self.cfg.window, mean, self.detector.last_threshold,
                    "" if self.detector.warmed_up else " (warming up)", self.total_messages, self.clips_made,
                )
            if self.cfg.keep_minutes > 0:
                self.recorder.prune(self.cfg.keep_minutes * 60 + self.cfg.max_clip + 60, now)

    def _enqueue(self, spike: Spike) -> None:
        msgs = [m for m in self.recent if spike.start <= m.ts <= spike.end]
        log.info("spike: peak %d msgs/%ds (%.1fx baseline), %.0fs long", spike.peak, self.cfg.window,
                 spike.intensity, spike.end - spike.start)
        self.clip_queue.put_nowait((spike, msgs))

    def clip_range(self, spike: Spike) -> tuple[float, float]:
        start = spike.start - self.cfg.pre_roll
        end = max(spike.peak_time, spike.end - self.cfg.window) + self.cfg.post_roll
        if end - start > self.cfg.max_clip:
            end = start + self.cfg.max_clip
        return start, end

    async def _clip_worker(self) -> None:
        while True:
            spike, msgs = await self.clip_queue.get()
            try:
                await self._make_clip(spike, msgs)
            except Exception:  # noqa: BLE001
                log.exception("clip failed")
            finally:
                self.clip_queue.task_done()

    async def _make_clip(self, spike: Spike, msgs: list[ChatMessage]) -> None:
        start, end = self.clip_range(spike)
        # wait until the video for the whole range has been written
        deadline = time.time() + (end - time.time()) + self.cfg.segment_time * 3 + 30
        while not range_is_recorded(list_segments(self.seg_dir), end):
            if self.recorder.finished.is_set() or time.time() > deadline:
                break
            await asyncio.sleep(1)

        top, chatters = summarize(msgs)
        label = slug(top[0][0]) if top else "spike"
        name = time.strftime("%H%M%S", time.gmtime(spike.peak_time)) + f"_x{spike.intensity:.1f}_{label}.mp4"
        path = await cut_clip(self.seg_dir, start, end, self.clip_dir / name, reencode=self.cfg.reencode)
        record = {
            **{k: round(v, 3) if isinstance(v, float) else v for k, v in asdict(spike).items()},
            "intensity": round(spike.intensity, 2),
            "clip_start": round(start, 3),
            "clip_end": round(end, 3),
            "messages": len(msgs),
            "chatters": chatters,
            "top": top,
            "clip": str(path) if path else None,
        }
        with open(self.session / "spikes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        if path:
            self.clips_made += 1
            log.info("clip saved: %s  (top: %s)", path, ", ".join(t for t, _ in top[:3]))
        else:
            log.info("spike at %s not clipped (no video recorded for it)",
                     time.strftime("%H:%M:%S", time.gmtime(spike.peak_time)))

    # -- lifecycle -------------------------------------------------------------
    async def run(self) -> None:
        log.info("session dir: %s", self.session)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass

        rec = asyncio.create_task(self.recorder.run(wait_for_live=self.cfg.wait, forever=self.cfg.forever))
        workers = [
            asyncio.create_task(self._chat_loop()),
            asyncio.create_task(self._tick_loop()),
            asyncio.create_task(self._clip_worker()),
        ]
        stopper = asyncio.create_task(self._stop.wait())
        await asyncio.wait([rec, stopper], return_when=asyncio.FIRST_COMPLETED)

        log.info("shutting down...")
        spike = self.detector.flush(time.time())
        if spike:
            self._enqueue(spike)
        await self.recorder.stop()
        if not rec.done():
            rec.cancel()
        try:
            await asyncio.wait_for(self.clip_queue.join(), 120)
        except asyncio.TimeoutError:
            log.warning("gave up waiting for pending clips")
        for t in workers + [stopper]:
            t.cancel()
        await asyncio.gather(*workers, stopper, rec, return_exceptions=True)
        if self._chat_file:
            self._chat_file.close()
        log.info("done: %d messages, %d clips in %s", self.total_messages, self.clips_made, self.clip_dir)
