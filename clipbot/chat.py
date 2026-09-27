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
    return QueueChatReader()
