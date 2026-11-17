"""python -m clipbot <channel> [options]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shlex
import shutil
import sys
from pathlib import Path

from .bot import ClipBot, Config


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(
        prog="clipbot",
        description="Record a Twitch stream, count chat messages, and clip every chat spike.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("channel", help="Twitch channel name (e.g. xqc)")
    p.add_argument("-o", "--out", type=Path, default=Path("recordings"), help="output directory")
    p.add_argument("-q", "--quality", default="best", help="streamlink quality (best, 720p60, 480p, audio_only...)")

    d = p.add_argument_group("spike detection")
    d.add_argument("--window", type=int, default=10, help="seconds of chat counted per sample")
    d.add_argument("--history", type=int, default=300, help="seconds of chat used as the baseline")
    d.add_argument("--sensitivity", type=float, default=3.0, help="std-devs above baseline needed to trigger")
    d.add_argument("--ratio", type=float, default=2.0, help="also need at least this multiple of the baseline")
    d.add_argument("--min-messages", type=int, default=10, help="absolute minimum messages per window")
    d.add_argument("--warmup", type=int, default=60, help="seconds of chat to learn the baseline first")
    d.add_argument("--cooldown", type=int, default=20, help="seconds to ignore chat after a spike")
    d.add_argument("--max-spike", type=int, default=60, help="close a spike after this many seconds")

    c = p.add_argument_group("clips")
    c.add_argument("--pre-roll", type=float, default=20.0, help="seconds of video before the spike (chat reacts late)")
    c.add_argument("--post-roll", type=float, default=10.0, help="seconds of video after the peak")
    c.add_argument("--max-clip", type=float, default=90.0, help="maximum clip length in seconds")
    c.add_argument("--reencode", action="store_true", help="frame-accurate cuts (slower, uses CPU)")

    r = p.add_argument_group("recording")
    r.add_argument("--segment-time", type=int, default=6, help="length of recording segments in seconds")
    r.add_argument("--keep-minutes", type=float, default=0,
                   help="only keep the last N minutes of full recording (0 = keep everything)")
    r.add_argument("--no-wait", action="store_true", help="exit immediately if the channel is offline")
    r.add_argument("--forever", action="store_true", help="keep running and record the next broadcast too")
    r.add_argument("--no-chat-log", action="store_true", help="don't save chat.jsonl")
    r.add_argument("--streamlink-args", default="", help='extra streamlink args, e.g. "--twitch-low-latency"')

    t = p.add_argument_group("testing")
    t.add_argument("--input", help="record this file/URL with ffmpeg instead of the live stream via streamlink")
    t.add_argument("--irc", default=None, help="chat server host:port (plain TCP) instead of Twitch")
    p.add_argument("-v", "--verbose", action="store_true")

    a = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config(
        channel=a.channel, out_dir=a.out, quality=a.quality,
        window=a.window, history=a.history, sensitivity=a.sensitivity, ratio=a.ratio,
        min_messages=a.min_messages, warmup=a.warmup, cooldown=a.cooldown, max_spike=a.max_spike,
        pre_roll=a.pre_roll, post_roll=a.post_roll, max_clip=a.max_clip, reencode=a.reencode,
        segment_time=a.segment_time, keep_minutes=a.keep_minutes, wait=not a.no_wait, forever=a.forever,
        save_chat=not a.no_chat_log, streamlink_args=shlex.split(a.streamlink_args), input_url=a.input,
    )
    if a.irc:
        host, _, port = a.irc.rpartition(":")
        cfg.irc_host, cfg.irc_port, cfg.irc_tls = host, int(port), False
    return cfg


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    if not shutil.which("ffmpeg"):
        print("error: ffmpeg not found on PATH", file=sys.stderr)
        return 1
    asyncio.run(ClipBot(cfg).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
