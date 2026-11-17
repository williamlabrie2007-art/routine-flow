"""Anonymous read-only Twitch chat client (IRC over TLS, no account needed)."""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
from dataclasses import dataclass
from typing import AsyncIterator

log = logging.getLogger("clipbot.chat")

TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697


@dataclass
class ChatMessage:
    ts: float
    user: str
    text: str
    tags: dict[str, str]

    @property
    def emotes(self) -> list[str]:
        """Emote names used in the message, from the IRCv3 `emotes` tag."""
        raw = self.tags.get("emotes", "")
        out: list[str] = []
        if not raw:
            return out
        for part in raw.split("/"):
            if ":" not in part:
                continue
            _, ranges = part.split(":", 1)
            for rng in ranges.split(","):
                try:
                    a, b = (int(x) for x in rng.split("-"))
                except ValueError:
                    continue
                out.append(self.text[a : b + 1])
        return out


def _unescape_tag(v: str) -> str:
    return (
        v.replace("\\s", " ").replace("\\:", ";").replace("\\r", "\r").replace("\\n", "\n").replace("\\\\", "\\")
    )


def parse_line(line: str, now: float | None = None) -> ChatMessage | None:
    """Parse one raw IRC line; returns a ChatMessage for PRIVMSG, else None."""
    tags: dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for kv in raw_tags.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape_tag(v)
    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    command, _, params = rest.partition(" ")
    if command != "PRIVMSG":
        return None
    _channel, _, text = params.partition(" :")
    user = tags.get("display-name") or prefix.split("!", 1)[0]
    ts = now if now is not None else time.time()
    if "tmi-sent-ts" in tags:
        try:
            ts = int(tags["tmi-sent-ts"]) / 1000.0
        except ValueError:
            pass
    return ChatMessage(ts=ts, user=user, text=text, tags=tags)


async def chat_stream(
    channel: str,
    host: str = TWITCH_IRC_HOST,
    port: int = TWITCH_IRC_PORT,
    use_tls: bool = True,
) -> AsyncIterator[ChatMessage]:
    """Yield chat messages forever, reconnecting with backoff on errors."""
    channel = channel.lower().lstrip("#")
    backoff = 1.0
    while True:
        writer = None
        try:
            ctx = ssl.create_default_context() if use_tls else None
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), 15)
            nick = f"justinfan{random.randint(10000, 99999)}"
            for cmd in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE", f"NICK {nick}", f"JOIN #{channel}"):
                writer.write((cmd + "\r\n").encode())
            await writer.drain()
            log.info("connected to chat #%s as %s", channel, nick)
            backoff = 1.0
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=360)  # Twitch PINGs every ~5 min
                if not raw:
                    raise ConnectionError("chat connection closed")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("PING"):
                    writer.write(("PONG" + line[4:] + "\r\n").encode())
                    await writer.drain()
                    continue
                if " RECONNECT" in line:
                    raise ConnectionError("server asked us to reconnect")
                msg = parse_line(line)
                if msg is not None:
                    yield msg
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - keep the bot alive
            log.warning("chat error: %s; reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if writer is not None:
                writer.close()
