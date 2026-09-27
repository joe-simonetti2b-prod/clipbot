"""
Brique 1a — Veille des lives à fort potentiel.

Uniquement des API officielles :
  * Twitch Helix  (/helix/streams)            — token app (client_credentials)
  * YouTube Data API v3 (search + videos)      — clé API
  * Kick Public API (/public/v1/livestreams)   — token app (client_credentials)

Chaque source renvoie des `StreamCandidate` normalisés ; l'agrégateur les
classe par audience et applique la liste blanche.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable
from abc import ABC, abstractmethod

import aiohttp

from .config import DiscoveryConfig
from .models import Platform, StreamCandidate

log = logging.getLogger(__name__)


class AppTokenCache:
    """Token OAuth 'client_credentials' mis en cache jusqu'à son expiration."""

    def __init__(self, token_url: str, client_id: str, client_secret: str):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._expires_at = 0.0

    async def get(self, session: aiohttp.ClientSession) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        async with session.post(self.token_url, data=data) as r:
            r.raise_for_status()
            payload = await r.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token


class DiscoverySource(ABC):
    platform: Platform

    def __init__(self, cfg: DiscoveryConfig, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        self.targets: list[str] = []   # chaînes ciblées sur cette plateforme (liste blanche)

    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    async def fetch(self) -> list[StreamCandidate]: ...


# --------------------------------------------------------------------------- #
# Twitch
# --------------------------------------------------------------------------- #
class TwitchDiscovery(DiscoverySource):
    platform = Platform.TWITCH
    API = "https://api.twitch.tv/helix/streams"

    def __init__(self, cfg, session):
        super().__init__(cfg, session)
        self.tokens = AppTokenCache(
            "https://id.twitch.tv/oauth2/token", cfg.twitch_client_id, cfg.twitch_client_secret
        )

    def enabled(self) -> bool:
        return bool(self.cfg.twitch_client_id and self.cfg.twitch_client_secret)

    async def fetch(self) -> list[StreamCandidate]:
        token = await self.tokens.get(self.session)
        headers = {"Client-Id": self.cfg.twitch_client_id, "Authorization": f"Bearer {token}"}
        params: list[tuple[str, str]] = [("first", "100")]
        if self.targets:
            # Liste blanche : on interroge directement ces chaînes (filtres = ET logique,
            # donc pas de filtre langue/catégorie en plus).
            params += [("user_login", c) for c in self.targets[:100]]
        else:
            params += [("language", lang) for lang in self.cfg.languages]
            params += [("game_id", gid) for gid in self.cfg.twitch_game_ids]

        async with self.session.get(self.API, headers=headers, params=params) as r:
            r.raise_for_status()
            data = (await r.json()).get("data", [])

        return [
            StreamCandidate(
                platform=self.platform,
                channel=s["user_login"],
                stream_id=s["id"],
                url=f"https://www.twitch.tv/{s['user_login']}",
                title=s.get("title", ""),
                category=s.get("game_name", ""),
                viewers=int(s.get("viewer_count", 0)),
                language=s.get("language", ""),
                chat_ref=s["user_login"],  # le canal IRC = login de la chaîne
            )
            for s in data
        ]


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
class YouTubeDiscovery(DiscoverySource):
    """
    Attention au quota (10 000 unités/jour par défaut) :
    search.list = 100 unités, videos.list = 1 unité. Avec 3 catégories et un
    passage toutes les 5 min on consomme ~86 000 unités/jour -> il faut soit
    augmenter DISCOVERY_INTERVAL_S, soit demander une extension de quota.
    """
    platform = Platform.YOUTUBE
    SEARCH = "https://www.googleapis.com/youtube/v3/search"
    VIDEOS = "https://www.googleapis.com/youtube/v3/videos"

    def enabled(self) -> bool:
        return bool(self.cfg.youtube_api_key)

    async def _search_ids(self, category_id: str) -> list[str]:
        params = {
            "part": "id",
            "eventType": "live",
            "type": "video",
            "order": "viewCount",
            "maxResults": "25",
            "videoCategoryId": category_id,
            "key": self.cfg.youtube_api_key,
        }
        if self.cfg.languages:
            params["relevanceLanguage"] = self.cfg.languages[0]
        async with self.session.get(self.SEARCH, params=params) as r:
            r.raise_for_status()
            items = (await r.json()).get("items", [])
        return [i["id"]["videoId"] for i in items]

    async def fetch(self) -> list[StreamCandidate]:
        ids: list[str] = []
        for cat in self.cfg.youtube_category_ids:
            ids += await self._search_ids(cat)
        ids = list(dict.fromkeys(ids))[:50]  # dédoublonnage, videos.list accepte 50 ids max
        if not ids:
            return []

        params = {
            "part": "snippet,liveStreamingDetails",
            "id": ",".join(ids),
            "key": self.cfg.youtube_api_key,
        }
        async with self.session.get(self.VIDEOS, params=params) as r:
            r.raise_for_status()
            items = (await r.json()).get("items", [])

        out = []
        for v in items:
            live = v.get("liveStreamingDetails", {})
            chat_id = live.get("activeLiveChatId")
            if not chat_id:  # chat désactivé -> impossible de détecter la hype
                continue
            sn = v["snippet"]
            out.append(
                StreamCandidate(
                    platform=self.platform,
                    channel=sn.get("channelTitle", ""),
                    stream_id=v["id"],
                    url=f"https://www.youtube.com/watch?v={v['id']}",
                    title=sn.get("title", ""),
                    category=sn.get("categoryId", ""),
                    viewers=int(live.get("concurrentViewers", 0)),
                    language=sn.get("defaultAudioLanguage", ""),
                    chat_ref=chat_id,
                )
            )
        return out


# --------------------------------------------------------------------------- #
# Kick (API publique officielle, pas de scraping)
# --------------------------------------------------------------------------- #
class KickDiscovery(DiscoverySource):
    platform = Platform.KICK
    API = "https://api.kick.com/public/v1/livestreams"

    def __init__(self, cfg, session):
        super().__init__(cfg, session)
        self.tokens = AppTokenCache(
            "https://id.kick.com/oauth/token", cfg.kick_client_id, cfg.kick_client_secret
        )

    def enabled(self) -> bool:
        return bool(self.cfg.kick_client_id and self.cfg.kick_client_secret)

    async def fetch(self) -> list[StreamCandidate]:
        token = await self.tokens.get(self.session)
        headers = {"Authorization": f"Bearer {token}"}
        out: list[StreamCandidate] = []
        for lang in self.cfg.languages or [""]:
            params = {"limit": "100", "sort": "viewer_count"}
            if lang:
                params["language"] = lang
            async with self.session.get(self.API, headers=headers, params=params) as r:
                r.raise_for_status()
                data = (await r.json()).get("data", [])
            for s in data:
                out.append(
                    StreamCandidate(
                        platform=self.platform,
                        channel=s["slug"],
                        stream_id=str(s["channel_id"]),
                        url=f"https://kick.com/{s['slug']}",
                        title=s.get("stream_title", ""),
                        category=(s.get("category") or {}).get("name", ""),
                        viewers=int(s.get("viewer_count", 0)),
                        language=s.get("language", ""),
                        chat_ref=str(s["broadcaster_user_id"]),
                    )
                )
        return out


    async def subscribe_chat(self, broadcaster_user_id: str) -> None:
        """Demande à Kick d'envoyer le chat de cette chaîne sur notre webhook."""
        token = await self.tokens.get(self.session)
        body = {"broadcaster_user_id": int(broadcaster_user_id), "method": "webhook",
                "events": [{"name": "chat.message.sent", "version": 1}]}
        async with self.session.post(
            "https://api.kick.com/public/v1/events/subscriptions",
            headers={"Authorization": f"Bearer {token}"}, json=body,
        ) as r:
            if r.status >= 300:
                log.error("Abonnement chat Kick refusé (%s) : %s", r.status, (await r.text())[:200])


# --------------------------------------------------------------------------- #
# Sonde yt-dlp : suivre des chaînes précises SANS clé API
# --------------------------------------------------------------------------- #
class ProbeDiscovery:
    """
    Pour les chaînes de la liste blanche dont la plateforme n'a pas de clé API
    configurée : yt-dlp vérifie si elles sont en live (≈1 s par chaîne).
    Twitch uniquement : son chat se lit sans compte, donc la détection marche.
    """

    def __init__(self, max_parallel: int = 2):
        self.sem = asyncio.Semaphore(max_parallel)

    async def probe(self, name: str) -> StreamCandidate | None:
        url = f"https://www.twitch.tv/{name}"
        async with self.sem:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "-J", "--no-warnings", "--skip-download", url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), 45)
            except asyncio.TimeoutError:
                proc.kill()
                return None
        if proc.returncode != 0 or not out:
            return None  # hors ligne
        info = json.loads(out)
        if not info.get("is_live", True):
            return None
        return StreamCandidate(
            platform=Platform.TWITCH, channel=name, stream_id=str(info.get("id", name)),
            url=url, title=info.get("description") or info.get("title", ""),
            category="", viewers=int(info.get("concurrent_view_count") or info.get("view_count") or 0),
            chat_ref=name,
        )


def parse_channel(spec: str) -> tuple[Platform, str]:
    """ « kamet0 » -> Twitch ; « kick:xxx », « youtube:xxx », URL complète acceptées. """
    spec = spec.strip().lower().rstrip("/")
    for p in Platform:
        if spec.startswith(p.value + ":"):
            return p, spec.split(":", 1)[1]
    if "kick.com/" in spec:
        return Platform.KICK, spec.rsplit("/", 1)[1]
    if "twitch.tv/" in spec:
        return Platform.TWITCH, spec.rsplit("/", 1)[1]
    return Platform.TWITCH, spec.lstrip("@")


# --------------------------------------------------------------------------- #
# Agrégateur
# --------------------------------------------------------------------------- #
class TrendScanner:
    def __init__(self, cfg: DiscoveryConfig, session: aiohttp.ClientSession,
                 extra_channels: Callable[[], list[str]] = lambda: []):
        self.cfg = cfg
        self.extra_channels = extra_channels
        self.twitch = TwitchDiscovery(cfg, session)
        self.youtube = YouTubeDiscovery(cfg, session)
        self.kick = KickDiscovery(cfg, session)
        self.prober = ProbeDiscovery()

    def targets(self) -> list[tuple[Platform, str]]:
        specs = self.cfg.allowed_channels + self.extra_channels()
        return list(dict.fromkeys(parse_channel(s) for s in specs))  # sans doublons, ordre gardé

    async def scan(self, probe_until: int | None = None) -> list[StreamCandidate]:
        """Interroge toutes les sources en parallèle, tolère la panne de l'une d'elles.
        probe_until : ne sonder (sans clé API) que les chaînes de rang < probe_until."""
        targets = self.targets()
        by_platform = {p: [n for q, n in targets if q is p] for p in Platform}
        jobs = []
        for src in (self.twitch, self.youtube, self.kick):
            if not src.enabled():
                continue
            if targets and not by_platform[src.platform]:
                continue  # liste blanche sans chaîne sur cette plateforme
            src.targets = by_platform[src.platform]
            jobs.append(src.fetch())
        # Twitch sans clé API : sonde yt-dlp sur les chaînes listées
        if not self.twitch.enabled():
            to_probe = [n for i, (p, n) in enumerate(targets) if p is Platform.TWITCH
                        and (probe_until is None or i < probe_until)]
            jobs += [self.prober.probe(n) for n in to_probe]
        if not jobs:
            if probe_until is None:
                log.warning("Rien à surveiller : ajoute une chaîne (/add) ou des clés API.")
            return []

        results = await asyncio.gather(*jobs, return_exceptions=True)
        candidates: list[StreamCandidate] = []
        for res in results:
            if isinstance(res, Exception):
                log.error("Veille en échec : %s", res)
            elif isinstance(res, list):
                candidates += res
            elif res is not None:
                candidates.append(res)

        if targets:
            wanted = {(p, n) for p, n in targets}
            filtered = [c for c in candidates if (c.platform, c.channel.lower()) in wanted]
        else:
            filtered = [c for c in candidates if c.viewers >= self.cfg.min_viewers]
        filtered.sort(key=lambda c: c.viewers, reverse=True)
        log.info("Veille : %d lives retenus sur %d", len(filtered), len(candidates))
        return filtered
