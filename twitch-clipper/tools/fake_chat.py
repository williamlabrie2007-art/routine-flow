"""Tiny fake Twitch IRC server for trying the bot offline.

Sends background chat at --rate msgs/sec and a burst of --burst-rate msgs/sec
every --every seconds (first one after --first), lasting --burst-len seconds.

    python tools/fake_chat.py --port 6667 &
    python -m clipbot demo --input video.mp4 --irc 127.0.0.1:6667 --warmup 20
"""

import argparse
import asyncio
import random
import time

WORDS = ["hello", "lol", "nice", "gg", "what", "true", "chat", "ok", "wow", "monkaS"]
HYPE = ["LUL", "KEKW", "POGGERS", "OMEGALUL", "W"]


async def handle(reader, writer, a):
    t0 = time.time()
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), 0.05) if not reader.at_eof() else b""
            if line.startswith(b"JOIN"):
                break
    except asyncio.TimeoutError:
        pass
    n = 0
    while not writer.is_closing():
        el = time.time() - t0
        burst = el >= a.first and (el - a.first) % a.every < a.burst_len
        rate = a.burst_rate if burst else a.rate
        words = HYPE if burst else WORDS
        await asyncio.sleep(random.expovariate(rate))
        n += 1
        user = f"user{random.randint(1, 500)}"
        msg = " ".join(random.choice(words) for _ in range(random.randint(1, 3)))
        writer.write(f":{user}!{user}@{user}.tmi.twitch.tv PRIVMSG #demo :{msg}\r\n".encode())
        try:
            await writer.drain()
        except ConnectionError:
            break


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=6667)
    p.add_argument("--rate", type=float, default=2.0)
    p.add_argument("--burst-rate", type=float, default=15.0)
    p.add_argument("--first", type=float, default=60)
    p.add_argument("--every", type=float, default=60)
    p.add_argument("--burst-len", type=float, default=8)
    a = p.parse_args()
    server = await asyncio.start_server(lambda r, w: handle(r, w, a), "127.0.0.1", a.port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
