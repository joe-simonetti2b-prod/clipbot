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
from .public import KickPublic, Resolution, TwitchPublic, kick_to_candidate, resolve

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
                        chat_ref=f"webhook:{s['broadcaster_user_id']}",
                    )
                )
        return out


    async def subscribe_chat(self, broadcaster_user_id: str) -> None:
        """Demande à Kick d'envoyer le chat de cette chaîne sur notre webhook."""
        broadcaster_user_id = broadcaster_user_id.removeprefix("webhook:")
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
def parse_spec(spec: str) -> tuple[str | None, str]:
    """ « kamet0 » -> (None, kamet0) : cherché sur Twitch ET Kick ;
    « kick:xxx », « twitch:xxx » ou une URL forcent la plateforme. """
    spec = spec.strip().lower().rstrip("/")
    for p in ("twitch", "kick", "youtube"):
        if spec.startswith(p + ":"):
            return p, spec.split(":", 1)[1].lstrip("@")
    if "kick.com/" in spec:
        return "kick", spec.rsplit("/", 1)[1]
    if "twitch.tv/" in spec:
        return "twitch", spec.rsplit("/", 1)[1]
    return None, spec.lstrip("@")


class TrendScanner:
    RESOLVE_EVERY_S = 6 * 3600

    def __init__(self, cfg: DiscoveryConfig, session: aiohttp.ClientSession,
                 extra_channels: Callable[[], list[str]] = lambda: [],
                 on_resolved: Callable[[Resolution], None] | None = None):
        self.cfg = cfg
        self.extra_channels = extra_channels
        self.on_resolved = on_resolved
        self.twitch = TwitchDiscovery(cfg, session)
        self.youtube = YouTubeDiscovery(cfg, session)
        self.kick = KickDiscovery(cfg, session)
        self.prober = ProbeDiscovery()
        self.tw_public = TwitchPublic(session)
        self.kick_public = KickPublic()
        self.resolution: Resolution | None = None
        self._resolved_for: tuple = ()
        self.last_scan_failed = False

    def specs(self) -> list[tuple[str | None, str]]:
        raw = self.cfg.allowed_channels + self.extra_channels()
        return list(dict.fromkeys(parse_spec(s) for s in raw if s.strip()))

    async def ensure_resolved(self) -> None:
        specs = tuple(self.specs())
        fresh = self.resolution and time.time() - self.resolution.at < self.RESOLVE_EVERY_S
        if specs == self._resolved_for and fresh:
            return
        self.resolution = await resolve(list(specs), self.tw_public, self.kick_public)
        self._resolved_for = specs
        log.info("%s", self.resolution.summary().replace("\n", " | "))
        if self.on_resolved:
            self.on_resolved(self.resolution)

    def targets(self) -> list[tuple[Platform, str]]:
        if self.resolution:
            return self.resolution.targets
        out = []
        for forced, name in self.specs():
            out.append((Platform(forced) if forced else Platform.TWITCH, name))
        return out

    async def _twitch_live(self, names: list[str], probe_until: int | None) -> list[StreamCandidate]:
        if self.twitch.enabled():
            self.twitch.targets = names
            return await self.twitch.fetch()
        try:
            users = await self.tw_public.users(names)
            return [c for u in users.values() if u and (c := TwitchPublic.to_candidate(u))]
        except Exception as e:
            # Secours : sonde yt-dlp (plus lente, sans nombre de viewers)
            log.warning("Twitch public indisponible (%s) -> sonde yt-dlp", e)
            limit = probe_until if probe_until is not None else 8
            res = await asyncio.gather(*(self.prober.probe(n) for n in names[:limit]),
                                       return_exceptions=True)
            return [r for r in res if isinstance(r, StreamCandidate)]

    async def _kick_live(self, names: list[str]) -> list[StreamCandidate]:
        if self.kick.enabled():
            self.kick.targets = names
            return await self.kick.fetch()

        async def one(slug):
            try:
                data = await self.kick_public.channel(slug)
                return kick_to_candidate(data) if data else None
            except Exception as e:
                log.debug("Kick %s : %s", slug, e)
                return None
        res = await asyncio.gather(*(one(n) for n in names))
        return [c for c in res if c]

    async def scan(self, probe_until: int | None = None) -> list[StreamCandidate]:
        """Lives en cours, triés par viewers. Tolère la panne d'une source."""
        if not self.specs():
            return await self._scan_trends()
        await self.ensure_resolved()
        targets = self.targets()
        tw = [n for p, n in targets if p is Platform.TWITCH]
        kk = [n for p, n in targets if p is Platform.KICK]
        jobs = []
        if tw:
            jobs.append(self._twitch_live(tw, probe_until))
        if kk:
            jobs.append(self._kick_live(kk))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        live: list[StreamCandidate] = []
        self.last_scan_failed = all(isinstance(r, Exception) for r in results)
        for r in results:
            if isinstance(r, Exception):
                log.error("Veille en échec : %s", r)
            else:
                live += r
        wanted = set(targets)
        live = [c for c in live if (c.platform, c.channel.lower()) in wanted]
        live.sort(key=lambda c: c.viewers, reverse=True)
        top = ", ".join(f"{c.channel}({c.platform.value[0]}) {c.viewers}" for c in live[:5])
        log.info("Veille : %d lives en cours%s", len(live), f" — {top}" if top else "")
        return live

    async def _scan_trends(self) -> list[StreamCandidate]:
        """Sans liste de chaînes : tendances via les API officielles (clés requises)."""
        jobs = [src.fetch() for src in (self.twitch, self.youtube, self.kick) if src.enabled()]
        if not jobs:
            log.warning("Rien à surveiller : ajoute une chaîne (/add) ou des clés API.")
            return []
        results = await asyncio.gather(*jobs, return_exceptions=True)
        candidates = [c for r in results if not isinstance(r, Exception) for c in r]
        filtered = [c for c in candidates if c.viewers >= self.cfg.min_viewers]
        filtered.sort(key=lambda c: c.viewers, reverse=True)
        return filtered
