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
TWITCH_WS_URL = "wss://irc-ws.chat.twitch.tv:443"


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


class _TcpConn:
    """Raw IRC over TCP/TLS."""

    def __init__(self, host: str, port: int, use_tls: bool):
        self.host, self.port, self.use_tls = host, port, use_tls

    async def open(self):
        ctx = ssl.create_default_context() if self.use_tls else None
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port, ssl=ctx), 15)

    async def send(self, line: str):
        self.writer.write((line + "\r\n").encode())
        await self.writer.drain()

    async def lines(self) -> list[str]:
        raw = await asyncio.wait_for(self.reader.readline(), timeout=360)  # Twitch PINGs every ~5 min
        if not raw:
            raise ConnectionError("chat connection closed")
        return [raw.decode("utf-8", "replace").rstrip("\r\n")]

    async def close(self):
        if getattr(self, "writer", None) is not None:
            self.writer.close()


class _WsConn:
    """IRC over WebSocket (port 443) - works behind HTTP(S) proxies (HTTPS_PROXY is honored)."""

    def __init__(self, url: str = TWITCH_WS_URL):
        self.url = url

    async def open(self):
        import websockets  # optional dependency

        self.ws = await asyncio.wait_for(websockets.connect(self.url, max_size=2**22), 15)

    async def send(self, line: str):
        await self.ws.send(line)

    async def lines(self) -> list[str]:
        data = await asyncio.wait_for(self.ws.recv(), timeout=360)
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        return [ln for ln in data.split("\r\n") if ln]

    async def close(self):
        if getattr(self, "ws", None) is not None:
            await self.ws.close()


def _default_transport() -> str:
    try:
        import websockets  # noqa: F401

        return "ws"
    except ImportError:
        return "tcp"


async def chat_stream(
    channel: str,
    host: str | None = None,
    port: int = TWITCH_IRC_PORT,
    use_tls: bool = True,
) -> AsyncIterator[ChatMessage]:
    """Yield chat messages forever, reconnecting with backoff on errors.

    With no host, connects to Twitch over WebSocket (or TLS IRC if the
    `websockets` package is missing). With a host, uses plain IRC to it.
    """
    channel = channel.lower().lstrip("#")
    backoff = 1.0
    while True:
        if host is None and _default_transport() == "ws":
            conn = _WsConn()
        else:
            conn = _TcpConn(host or TWITCH_IRC_HOST, port, use_tls)
        try:
            await conn.open()
            nick = f"justinfan{random.randint(10000, 99999)}"
            for cmd in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE", f"NICK {nick}", f"JOIN #{channel}"):
                await conn.send(cmd)
            log.info("connected to chat #%s as %s (%s)", channel, nick, "websocket" if isinstance(conn, _WsConn) else "irc")
            backoff = 1.0
            while True:
                for line in await conn.lines():
                    if line.startswith("PING"):
                        await conn.send("PONG" + line[4:])
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
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass
