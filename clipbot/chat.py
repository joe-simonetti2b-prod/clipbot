"""
Brique 1b — Lecture du chat en temps réel.

  * Twitch  : IRC anonyme en lecture seule (pseudo `justinfan*`, méthode
              documentée par Twitch, aucun compte requis).
  * YouTube : liveChatMessages.list en polling, au rythme imposé par
              `pollingIntervalMillis` (5 unités de quota par appel).
  * Kick    : l'API publique livre le chat par webhooks (événement
              `chat.message.sent`) vers une URL publique. On expose donc une
              file que le serveur webhook (brique ultérieure) alimente.

Chaque lecteur est un générateur asynchrone de `ChatMessage`.
"""
from __future__ import annotations

import asyncio
from collections import deque
import re
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator

import aiohttp

from .models import ChatMessage, StreamCandidate, Platform

log = logging.getLogger(__name__)


class ChatReader(ABC):
    @abstractmethod
    def messages(self) -> AsyncIterator[ChatMessage]: ...


class TwitchChatReader(ChatReader):
    HOST, PORT = "irc.chat.twitch.tv", 6667

    def __init__(self, channel: str):
        self.channel = channel.lower()

    async def messages(self) -> AsyncIterator[ChatMessage]:
        backoff = 1
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.HOST, self.PORT)
                nick = f"justinfan{random.randint(10000, 99999)}"
                writer.write(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
                await writer.drain()
                backoff = 1
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise ConnectionError("IRC fermé par le serveur")
                    line = raw.decode("utf-8", errors="ignore").rstrip("\r\n")
                    if line.startswith("PING"):
                        writer.write(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        await writer.drain()
                        continue
                    msg = self._parse(line)
                    if msg:
                        yield msg
            except (ConnectionError, OSError) as e:
                log.warning("Chat Twitch #%s déconnecté (%s), reconnexion dans %ss",
                            self.channel, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    @staticmethod
    def _parse(line: str) -> ChatMessage | None:
        # Format : ":user!user@user.tmi.twitch.tv PRIVMSG #chan :texte"
        if " PRIVMSG " not in line:
            return None
        prefix, _, rest = line.partition(" PRIVMSG ")
        author = prefix.lstrip(":").split("!", 1)[0]
        _, _, text = rest.partition(" :")
        return ChatMessage(ts=time.time(), author=author, text=text)


class YouTubeChatReader(ChatReader):
    API = "https://www.googleapis.com/youtube/v3/liveChat/messages"

    def __init__(self, live_chat_id: str, api_key: str, session: aiohttp.ClientSession):
        self.live_chat_id = live_chat_id
        self.api_key = api_key
        self.session = session

    async def messages(self) -> AsyncIterator[ChatMessage]:
        page_token: str | None = None
        first_page = True
        while True:
            params = {
                "liveChatId": self.live_chat_id,
                "part": "snippet,authorDetails",
                "maxResults": "2000",
                "key": self.api_key,
            }
            if page_token:
                params["pageToken"] = page_token
            async with self.session.get(self.API, params=params) as r:
                if r.status == 403:
                    log.error("YouTube chat 403 (quota ou chat fermé) — arrêt du lecteur")
                    return
                r.raise_for_status()
                data = await r.json()

            page_token = data.get("nextPageToken")
            now = time.time()
            # La 1re page contient l'historique récent : on l'ignore pour ne pas
            # fausser la ligne de base du détecteur.
            if not first_page:
                for item in data.get("items", []):
                    sn = item.get("snippet", {})
                    text = sn.get("displayMessage") or ""
                    author = item.get("authorDetails", {}).get("displayName", "")
                    if text:
                        yield ChatMessage(ts=now, author=author, text=text)
            first_page = False
            await asyncio.sleep(max(data.get("pollingIntervalMillis", 5000), 2000) / 1000)


KICK_PUSHER = ("wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679"
               "?protocol=7&client=js&version=8.4.0&flash=false")
EMOTE_RE = re.compile(r"\[emote:\d+:([^\]]+)\]")


def parse_kick_event(raw: str) -> tuple[str, ChatMessage | None, str | None]:
    """Décode un message Pusher Kick -> (événement, message de chat ou None, id)."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return "", None, None
    event = msg.get("event", "")
    if "ChatMessage" not in event:
        return event, None, None
    data = msg.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return event, None, None
    data = (data or {}).get("message", data) if isinstance(data, dict) else {}
    text = EMOTE_RE.sub(r"\1", str(data.get("content") or ""))
    sender = data.get("sender") or {}
    author = sender.get("username") or sender.get("slug") or data.get("sender_username") or "?"
    if not text:
        return event, None, None
    return event, ChatMessage(ts=time.time(), author=author, text=text), str(data.get("id") or "")


class KickChatReader(ChatReader):
    """Chat Kick en lecture seule via le websocket public (celui du site kick.com)."""

    def __init__(self, chatroom_id: str, session: aiohttp.ClientSession):
        self.chatroom_id = chatroom_id
        self.session = session

    async def messages(self) -> AsyncIterator[ChatMessage]:
        seen: deque[str] = deque(maxlen=500)
        backoff = 1
        channels = [f"chatrooms.{self.chatroom_id}.v2", f"chatrooms.{self.chatroom_id}",
                    f"chatroom.{self.chatroom_id}"]
        while True:
            try:
                async with self.session.ws_connect(KICK_PUSHER, heartbeat=60,
                                                   timeout=aiohttp.ClientTimeout(total=None)) as ws:
                    backoff = 1
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            if m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        event, msg, mid = parse_kick_event(m.data)
                        if event == "pusher:connection_established":
                            for ch in channels:
                                await ws.send_json({"event": "pusher:subscribe",
                                                    "data": {"auth": "", "channel": ch}})
                        elif event == "pusher:ping":
                            await ws.send_json({"event": "pusher:pong", "data": {}})
                        elif msg:
                            if mid and mid in seen:
                                continue  # même message reçu sur plusieurs canaux
                            if mid:
                                seen.append(mid)
                            yield msg
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Chat Kick %s déconnecté (%s), reconnexion dans %ss",
                            self.chatroom_id, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


class QueueChatReader(ChatReader):
    """Pour Kick : le serveur webhook pousse les messages dans cette file."""

    def __init__(self):
        self.queue: asyncio.Queue[ChatMessage] = asyncio.Queue(maxsize=10_000)

    async def messages(self) -> AsyncIterator[ChatMessage]:
        while True:
            yield await self.queue.get()


def make_chat_reader(c: StreamCandidate, session: aiohttp.ClientSession,
                     youtube_key: str) -> ChatReader:
    if c.platform is Platform.TWITCH:
        return TwitchChatReader(c.chat_ref)
    if c.platform is Platform.YOUTUBE:
        return YouTubeChatReader(c.chat_ref, youtube_key, session)
    if c.platform is Platform.KICK and c.chat_ref and not c.chat_ref.startswith("webhook:"):
        return KickChatReader(c.chat_ref, session)
    return QueueChatReader()
