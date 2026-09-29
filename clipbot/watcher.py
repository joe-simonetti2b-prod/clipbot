"""
Orchestration de la surveillance : un `StreamWatcher` par live suivi
(capture + chat + détecteur), piloté par un `Orchestrator` qui relance la
veille périodiquement, fait tourner les lives selon l'audience, et respecte
la pause / les chaînes ajoutées depuis Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import re
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

CLIP_LINK_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:"
    r"clips\.twitch\.tv/(?:embed\?clip=)?[A-Za-z0-9_-]{6,}"
    r"|twitch\.tv/\w+/clip/[A-Za-z0-9_-]{6,}"
    r"|kick\.com/[\w-]+(?:/clips/|\?clip=)clip_[A-Za-z0-9]+)", re.I)


def find_clip_links(text: str) -> list[str]:
    """Liens de clips Twitch / Kick postés dans le chat (un viewer a jugé le moment fort)."""
    return list(dict.fromkeys(m.group(0) for m in CLIP_LINK_RE.finditer(text)))


class StreamWatcher:
    def __init__(self, c: StreamCandidate, settings: Settings,
                 session: aiohttp.ClientSession, extractor: ClipExtractor, on_clip: OnClip,
                 on_link: Callable[[StreamCandidate, str, str], None] | None = None):
        self.c = c
        self.on_link = on_link
        self.recorder = StreamRecorder(c, settings.capture)
        self.detector = HypeDetector(c.key, settings.hype)
        self.min_score = settings.hype.min_score
        self.chat = make_chat_reader(c, session, settings.discovery.youtube_api_key)
        self.extractor = extractor
        self.on_clip = on_clip
        self.clips = 0
        self.chat_count = 0
        self._health_at, self._health_chat = time.time(), 0
        self.started_at = time.time()
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
            self.chat_count += 1
            self.detector.add(msg)
            if self.on_link and "clip" in msg.text.lower():
                for url in find_clip_links(msg.text):
                    self.on_link(self.c, url, msg.author)

    def health(self) -> str:
        """Bilan : débit du chat et vidéo en tampon (les deux doivent être > 0)."""
        now = time.time()
        mins = max((now - self._health_at) / 60, 0.1)
        rate = (self.chat_count - self._health_chat) / mins
        self._health_at, self._health_chat = now, self.chat_count
        segs = len(list(self.recorder.buffer_dir.glob("seg_*.ts")))
        return (f"{self.c.channel} ({self.c.platform.value}, {self.c.viewers} viewers) : "
                f"chat {rate:.0f} msg/min · vidéo {segs * self.recorder.cfg.segment_s}s en tampon "
                f"({self.recorder.mode}) · {self.clips} clips")

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


def plan_rotation(watched: dict[str, tuple[StreamCandidate, float]],
                  live: list[StreamCandidate], limit: int, ratio: float,
                  min_watch_s: float, now: float,
                  pinned: set[str] | frozenset = frozenset()) -> tuple[list[str], list[StreamCandidate]]:
    """Décide quels lives arrêter / démarrer.

    watched : {clé: (candidat avec viewers à jour, heure de début de suivi)}
    pinned  : créateurs choisis à la main depuis Telegram (/lives) : prioritaires,
              jamais remplacés par la rotation ; les places restantes suivent l'algorithme.
    Règles :
      * un live terminé est lâché ;
      * les places libres vont aux lives les plus regardés ;
      * rotation progressive : au plus UN remplacement par cycle, seulement si le
        nouveau live a `ratio`× plus de poids (viewers × priorité) que le plus faible,
        et si ce dernier est suivi depuis au moins `min_watch_s` ;
      * jamais deux fois le même créateur (Twitch + Kick en simultané).
    """
    pinned = {p.lower() for p in pinned}
    live_by_key = {c.key: c for c in live}
    stop = [k for k in watched if k not in live_by_key]
    kept = {k: (live_by_key[k], t) for k, (_, t) in watched.items() if k in live_by_key}
    creators = {c.channel.lower() for c, _ in kept.values()}
    start: list[StreamCandidate] = []

    # 1) Choix manuels en live mais pas encore suivis : ils prennent la place des plus faibles
    forced: list[StreamCandidate] = []
    for c in live:                                  # trié par poids : meilleure plateforme d'abord
        name = c.channel.lower()
        if name in pinned and name not in creators and name not in {x.channel.lower() for x in forced}:
            forced.append(c)
    forced = forced[:limit]
    free = [k for k, (c, _) in kept.items() if c.channel.lower() not in pinned]
    while len(kept) + len(forced) > limit and free:
        weakest = min(free, key=lambda k: kept[k][0].weight)
        free.remove(weakest)
        creators.discard(kept[weakest][0].channel.lower())
        del kept[weakest]
        stop.append(weakest)
    # Moins de places qu'avant (mémoire sous pression) : même un choix manuel cède
    while len(kept) + len(forced) > limit and kept:
        weakest = min(kept, key=lambda k: kept[k][0].weight)
        creators.discard(kept[weakest][0].channel.lower())
        del kept[weakest]
        stop.append(weakest)
    start += forced
    taken = creators | {c.channel.lower() for c in forced}

    # 2) Places libres -> lives les plus regardés
    pool = [c for c in live if c.key not in kept and c.channel.lower() not in taken]
    for c in pool:
        if len(kept) + len(start) >= limit:
            break
        if c.channel.lower() in {x.channel.lower() for x in start}:
            continue
        start.append(c)

    # 3) Rotation progressive, uniquement sur les places non choisies à la main
    rotatable = {k: v for k, v in kept.items() if v[0].channel.lower() not in pinned}
    if len(kept) + len(start) >= limit and rotatable and not forced:
        used = {x.channel.lower() for x in start}
        rest = [c for c in pool if c not in start and c.channel.lower() not in used]
        if rest:
            best = rest[0]
            weakest_key, (weakest, since) = min(rotatable.items(), key=lambda kv: kv[1][0].weight)
            if now - since >= min_watch_s and best.weight >= max(1, weakest.weight) * ratio:
                stop.append(weakest_key)
                start.append(best)
    return stop, start


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, session: aiohttp.ClientSession,
                 on_clip: OnClip):
        self.s = settings
        self.store = store
        self.session = session
        self.on_clip = on_clip
        self.watchers: dict[str, StreamWatcher] = {}
        # Les choix manuels sont aussi ajoutés à la veille (même hors de la liste)
        self.scanner = TrendScanner(settings.discovery, session,
                                    extra_channels=lambda: store.get("extra_channels", [])
                                    + store.get("pinned_channels", []),
                                    on_resolved=lambda r: self.notify(r.summary()),
                                    perf=lambda: store.get("channel_perf_boost", {}),
                                    esport=lambda: bool(store.get("esport_mode", False)),
                                    pinned=lambda: store.get("pinned_channels", []))
        self.notify = lambda text: None   # branché par l'application (message Telegram)
        self.extractor = ClipExtractor(settings.capture, settings.hype)
        self._rescan = asyncio.Event()
        self.last_live: list[StreamCandidate] = []   # dernier résultat de la veille (/lives)
        self.on_link: Callable[[StreamCandidate, str, str], None] = lambda c, url, who: None
        self.capacity: Callable[[], int] = lambda: settings.discovery.max_concurrent_streams

    def pinned(self) -> set[str]:
        from .discovery import parse_spec
        return {parse_spec(p)[1] for p in self.store.get("pinned_channels", [])}

    @property
    def paused(self) -> bool:
        return self.store.get("paused", False)

    def rescan(self) -> None:
        self._rescan.set()

    def kick_message(self, broadcaster_user_id: str, author: str, text: str) -> None:
        """Appelé par le webhook Kick : route le message vers le bon live."""
        for w in self.watchers.values():
            if w.c.platform is Platform.KICK and w.c.chat_ref == f"webhook:{broadcaster_user_id}" \
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

        for w in self.watchers.values():
            log.info("♥ %s", w.health())
        live = await self.scanner.scan()
        if not live and self.watchers and self.scanner.last_scan_failed:
            return  # veille en panne : on ne coupe pas les lives en cours
        self.last_live = live
        watched = {k: (w.c, w.started_at) for k, w in self.watchers.items()}
        d = self.s.discovery
        # Mémoire sous pression (voir resources.py) : un live de moins temporairement
        slots = max(1, min(d.max_concurrent_streams, self.capacity()))
        stop, start = plan_rotation(watched, live, slots,
                                    d.switch_ratio, d.min_watch_s, time.time(), self.pinned())
        live_by_key = {c.key: c for c in live}
        for k, w in self.watchers.items():
            if k in live_by_key:
                w.c.viewers = live_by_key[k].viewers   # affichage /status à jour
                w.c.boost = live_by_key[k].boost
        for key in stop:
            w = self.watchers.pop(key, None)
            if w:
                log.info("↔ Arrêt du suivi %s (%s)", w.c.channel,
                         "live terminé" if key not in live_by_key else "remplacé")
                await w.stop()
        for c in start:
            w = StreamWatcher(c, self.s, self.session, self.extractor, self.on_clip,
                              on_link=lambda cand, url, who: self.on_link(cand, url, who))
            self.watchers[c.key] = w
            if c.platform is Platform.KICK and self.scanner.kick.enabled():
                await self.scanner.kick.subscribe_chat(c.chat_ref)
            await w.start()
