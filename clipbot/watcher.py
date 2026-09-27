"""
Orchestration de la surveillance : un `StreamWatcher` par live suivi
(capture + chat + détecteur), piloté par un `Orchestrator` qui relance la
veille périodiquement, fait tourner les lives selon l'audience, et respecte
la pause / les chaînes ajoutées depuis Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import aiohttp

from .chat import QueueChatReader, make_chat_reader
from .config import Settings
from .discovery import TrendScanner
from .hype import HypeDetector
from .models import ChatMessage, HypeEvent, Platform, StreamCandidate
from .recorder import ClipExtractor, RawClip, StreamRecorder
from .storage import Store

log = logging.getLogger(__name__)

OnClip = Callable[[StreamCandidate, HypeEvent, RawClip], None]


class StreamWatcher:
    def __init__(self, c: StreamCandidate, settings: Settings,
                 session: aiohttp.ClientSession, extractor: ClipExtractor, on_clip: OnClip):
        self.c = c
        self.recorder = StreamRecorder(c, settings.capture)
        self.detector = HypeDetector(c.key, settings.hype)
        self.min_score = settings.hype.min_score
        self.chat = make_chat_reader(c, session, settings.discovery.youtube_api_key)
        self.extractor = extractor
        self.on_clip = on_clip
        self.clips = 0
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        log.info("▶ Suivi %s (%s, %d viewers) — %s",
                 self.c.channel, self.c.platform.value, self.c.viewers, self.c.title[:60])
        self._tasks = [
            asyncio.create_task(self.recorder.run(), name=f"rec:{self.c.key}"),
            asyncio.create_task(self.recorder.janitor(), name=f"jan:{self.c.key}"),
            asyncio.create_task(self._consume_chat(), name=f"chat:{self.c.key}"),
            asyncio.create_task(self._detect_loop(), name=f"hype:{self.c.key}"),
        ]

    @property
    def finished(self) -> bool:
        return bool(self._tasks) and self._tasks[0].done()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.recorder.stop()
        log.info("■ Arrêt du suivi %s", self.c.channel)

    async def _consume_chat(self) -> None:
        async for msg in self.chat.messages():
            self.detector.add(msg)

    async def _detect_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            ev = self.detector.tick(time.time())
            if ev and ev.score < self.min_score:
                log.debug("Pic ignoré sur %s (score %.1f < %.1f)", self.c.channel, ev.score, self.min_score)
                continue
            if ev and self.recorder.alive.is_set():
                log.info("🔥 Hype %s sur %s (score %.1f, %.1f msg/s, %s)",
                         ev.reason, self.c.channel, ev.score, ev.msgs_per_s, ev.top_tokens)
                asyncio.create_task(self._extract(ev))  # la détection ne s'arrête jamais

    async def _extract(self, ev: HypeEvent) -> None:
        raw = await self.extractor.extract(self.recorder, ev)
        if raw:
            self.clips += 1
            self.on_clip(self.c, ev, raw)


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, session: aiohttp.ClientSession,
                 on_clip: OnClip):
        self.s = settings
        self.store = store
        self.session = session
        self.on_clip = on_clip
        self.watchers: dict[str, StreamWatcher] = {}
        self.scanner = TrendScanner(settings.discovery, session,
                                    extra_channels=lambda: store.get("extra_channels", []))
        self.extractor = ClipExtractor(settings.capture, settings.hype)
        self._rescan = asyncio.Event()

    @property
    def paused(self) -> bool:
        return self.store.get("paused", False)

    def rescan(self) -> None:
        self._rescan.set()

    def kick_message(self, broadcaster_user_id: str, author: str, text: str) -> None:
        """Appelé par le webhook Kick : route le message vers le bon live."""
        for w in self.watchers.values():
            if w.c.platform is Platform.KICK and w.c.chat_ref == broadcaster_user_id \
                    and isinstance(w.chat, QueueChatReader):
                try:
                    w.chat.queue.put_nowait(ChatMessage(time.time(), author, text))
                except asyncio.QueueFull:
                    pass

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self._rebalance()
                except Exception:
                    log.exception("Cycle de veille en échec")
                self._rescan.clear()
                try:
                    await asyncio.wait_for(self._rescan.wait(), self.s.discovery.poll_interval_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stop_all()

    async def stop_all(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.watchers.values()), return_exceptions=True)
        self.watchers.clear()

    async def _rebalance(self) -> None:
        for key in [k for k, w in self.watchers.items() if w.finished]:
            await self.watchers.pop(key).stop()
        if self.paused:
            if self.watchers:
                await self.stop_all()
            return

        limit = self.s.discovery.max_concurrent_streams
        # Économie de CPU : quand toutes les places sont prises, on ne sonde que les
        # chaînes prioritaires (placées avant celles suivies dans la liste).
        probe_until = None
        if len(self.watchers) >= limit:
            order = [name for _, name in self.scanner.targets()]
            ranks = [order.index(w.c.channel.lower()) for w in self.watchers.values()
                     if w.c.channel.lower() in order]
            if ranks:
                probe_until = max(ranks)
        top = (await self.scanner.scan(probe_until=probe_until))[:limit]
        wanted = {c.key: c for c in top}

        # Lâcher les lives sortis du top (hystérésis : on garde s'il reste une place)
        for key in list(self.watchers):
            if key not in wanted and len(wanted) >= limit:
                await self.watchers.pop(key).stop()

        for key, c in wanted.items():
            if key not in self.watchers and len(self.watchers) < limit:
                w = StreamWatcher(c, self.s, self.session, self.extractor, self.on_clip)
                self.watchers[key] = w
                if c.platform is Platform.KICK and self.scanner.kick.enabled():
                    await self.scanner.kick.subscribe_chat(c.chat_ref)
                await w.start()
