# clipbot — Twitch chat-spike clipper

Records a live Twitch stream, counts the chat in real time, and cuts a clip
every time chat spikes (hype moment, funny fail, big play...).

```
streamlink ──► ffmpeg ──► segments/seg_<UTC time>.ts   (the full recording)
Twitch chat (anonymous IRC) ──► msgs/sec ──► spike detector ──► ffmpeg cut ──► clips/*.mp4
```

No Twitch account or API key needed: the bot reads chat anonymously.

## Setup

Requires Python 3.10+ and `ffmpeg` on your PATH.

```bash
cd twitch-clipper
pip install -r requirements.txt     # installs streamlink
```

## Usage

```bash
python -m clipbot <channel>
```

It waits for the channel to go live, records until the stream ends, and
clips every spike. Ctrl+C stops it cleanly (any spike in progress still gets clipped).

Output:

```
recordings/<channel>/<session UTC time>/
  segments/   full recording as 6s .ts files (play in VLC/mpv, or merge with ffmpeg)
  clips/      014045_x7.3_KEKW.mp4  = peak time (UTC), 7.3x normal chat, top emote
  chat.jsonl  every chat message with a timestamp
  spikes.jsonl one line per spike: peak rate, baseline, chatters, top emotes/words, clip path
```

Merge the full recording into one file:

```bash
cd recordings/<channel>/<session>/segments
ffmpeg -i "concat:$(ls seg_*.ts | paste -sd'|')" -c copy ../full.mp4
```

### Useful options

| option | default | what it does |
|---|---|---|
| `-q 720p60` | `best` | stream quality (`audio_only` works too) |
| `--sensitivity` | `3.0` | std-devs above normal chat to trigger. Lower = more clips |
| `--ratio` | `2.0` | must also be this many times the normal rate |
| `--min-messages` | `10` | floor per 10s window, so small chats don't clip on 4 messages |
| `--pre-roll` / `--post-roll` | `20` / `10` | seconds kept before the spike / after the peak |
| `--max-clip` | `90` | longest clip length |
| `--reencode` | off | frame-accurate cuts (default cuts are instant but snap to keyframes, ~2s) |
| `--keep-minutes 30` | keep all | only keep a rolling 30 min of full recording (saves disk; clips are kept) |
| `--forever` | off | keep running and record the next broadcast too |
| `--streamlink-args "--twitch-low-latency"` | | passed straight to streamlink (e.g. `--twitch-api-header` for ad-free with Turbo) |

Run `python -m clipbot --help` for the full list.

## How spikes are detected

Every second the bot counts messages in the last `--window` (10) seconds and
compares that to a rolling baseline over the last `--history` (300) seconds.
A spike starts when the count is above **all** of: `--min-messages`,
`baseline × --ratio`, and `baseline + --sensitivity × stddev`. So it adapts to
both a 50-viewer chat and a 50k-viewer one. The spike ends when chat falls back
under 75% of the trigger level; then the clip is cut from
`spike start − pre-roll` to `peak + post-roll`. The first `--warmup` (60) seconds only
learn the baseline.

Chat reacts a few seconds *after* viewers see the moment, which is why the
default pre-roll is generous (20s).

## Try it offline

A fake chat server and any video file let you watch the whole pipeline without Twitch:

```bash
ffmpeg -f lavfi -i testsrc=duration=150:size=640x360:rate=30 -f lavfi -i sine=duration=150 \
       -c:v libx264 -g 60 -c:a aac -shortest demo.mp4
python tools/fake_chat.py --port 6667 --first 50 --every 50 &
python -m clipbot demo --input demo.mp4 --irc 127.0.0.1:6667 --warmup 20
```

## Tests

```bash
pip install pytest && python -m pytest
```
