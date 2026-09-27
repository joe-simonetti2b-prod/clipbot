"""
Sources publiques, sans clé API : état des lives, nombre de viewers, vérification
des pseudos. Ce sont les mêmes appels que ceux des sites twitch.tv et kick.com.

  * Twitch : API GraphQL publique (gql.twitch.tv) — UNE requête pour 50 chaînes,
    avec le nombre de viewers exact (la sonde yt-dlp renvoyait 0).
  * Kick   : https://kick.com/api/v2/channels/<pseudo>, protégé par Cloudflare ;
    curl_cffi imite l'empreinte TLS d'un navigateur. Renvoie aussi l'URL vidéo
    HLS (lue directement par FFmpeg) et l'id du salon de chat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import aiohttp

from .models import Platform, StreamCandidate

log = logging.getLogger(__name__)

TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # identifiant public du site twitch.tv
KICK_CHANNEL = "https://kick.com/api/v2/channels/{slug}"
LOGIN_RE = re.compile(r"^[a-z0-9_]{2,25}$")


# --------------------------------------------------------------------------- #
# Twitch
# --------------------------------------------------------------------------- #
class TwitchPublic:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def users(self, logins: list[str]) -> dict[str, dict | None]:
        """{login: infos | None si le compte n'existe pas}."""
        out: dict[str, dict | None] = {}
        valid = [l for l in dict.fromkeys(x.lower() for x in logins) if LOGIN_RE.match(l)]
        out.update({l.lower(): None for l in logins if l.lower() not in valid})
        for i in range(0, len(valid), 50):
            batch = valid[i:i + 50]
            query = ("query { users(logins: %s) { login displayName followers { totalCount } "
                     "stream { id title viewersCount type game { name } } } }" % json.dumps(batch))
            async with self.session.post(
                TWITCH_GQL, json={"query": query},
                headers={"Client-ID": TWITCH_WEB_CLIENT_ID},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"Twitch GQL HTTP {r.status}")
                payload = await r.json(content_type=None)
            if payload.get("errors") and not payload.get("data"):
                raise RuntimeError(f"Twitch GQL : {payload['errors'][0].get('message')}")
            users = (payload.get("data") or {}).get("users") or []
            found = {u["login"].lower(): u for u in users if u}
            for login in batch:
                out[login] = found.get(login)
        return out

    @staticmethod
    def to_candidate(u: dict) -> StreamCandidate | None:
        s = u.get("stream")
        if not s or (s.get("type") or "live") != "live":
            return None
        login = u["login"].lower()
        return StreamCandidate(
            platform=Platform.TWITCH, channel=login, stream_id=str(s["id"]),
            url=f"https://www.twitch.tv/{login}", title=s.get("title") or "",
            category=(s.get("game") or {}).get("name", ""),
            viewers=int(s.get("viewersCount") or 0), chat_ref=login,
        )


# --------------------------------------------------------------------------- #
# Kick
# --------------------------------------------------------------------------- #
class KickPublic:
    def __init__(self, max_parallel: int = 4):
        self.sem = asyncio.Semaphore(max_parallel)
        self._session = None

    async def _get(self, url: str):
        from curl_cffi.requests import AsyncSession  # import tardif (lourd)
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome", timeout=20)
        async with self.sem:
            return await self._session.get(url, headers={"Accept": "application/json"})

    async def channel(self, slug: str) -> dict | None:
        """JSON de la chaîne, ou None si elle n'existe pas. Lève une erreur si Kick bloque."""
        slug = slug.lower().strip()
        r = await self._get(KICK_CHANNEL.format(slug=slug))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"Kick HTTP {r.status_code} pour {slug}")
        try:
            return r.json()
        except Exception:
            raise RuntimeError(f"Kick : réponse illisible pour {slug} (Cloudflare ?)")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def kick_to_candidate(data: dict) -> StreamCandidate | None:
    live = data.get("livestream")
    if not live or not live.get("is_live", True):
        return None
    slug = (data.get("slug") or "").lower()
    cats = live.get("categories") or []
    return StreamCandidate(
        platform=Platform.KICK, channel=slug, stream_id=str(live.get("id") or slug),
        url=f"https://kick.com/{slug}", title=live.get("session_title") or "",
        category=(cats[0] or {}).get("name", "") if cats else "",
        viewers=int(live.get("viewer_count") or 0), language=live.get("language") or "",
        chat_ref=str((data.get("chatroom") or {}).get("id") or ""),
        hls_url=data.get("playback_url") or "",
    )


def pick_variant(master: str, base_url: str, max_height: int = 720) -> str | None:
    """Choisit dans une playlist HLS maîtresse la meilleure qualité ≤ max_height."""
    best, best_key = None, None
    lines = master.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        res = re.search(r"RESOLUTION=\d+x(\d+)", line)
        bw = re.search(r"BANDWIDTH=(\d+)", line)
        h = int(res.group(1)) if res else 0
        uri = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), None)
        if not uri or h > max_height:
            continue
        key = (h, int(bw.group(1)) if bw else 0)
        if best_key is None or key > best_key:
            best, best_key = urljoin(base_url, uri), key
    return best


# --------------------------------------------------------------------------- #
# Vérification des pseudos
# --------------------------------------------------------------------------- #
@dataclass
class Resolution:
    targets: list[tuple[Platform, str]] = field(default_factory=list)
    twitch: list[str] = field(default_factory=list)
    kick: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)   # vérification impossible (réseau)
    at: float = field(default_factory=time.time)

    def summary(self) -> str:
        both = sorted(set(self.twitch) & set(self.kick))
        parts = [f"{len(self.twitch)} Twitch", f"{len(self.kick)} Kick"]
        text = "Chaînes vérifiées : " + " · ".join(parts)
        if both:
            text += f"\nSur les deux plateformes : {', '.join(both)}"
        if self.missing:
            text += f"\n⚠️ Introuvables (ignorées) : {', '.join(self.missing)}"
        if self.unchecked:
            text += f"\n❔ Non vérifiées (réseau) : {', '.join(self.unchecked)}"
        return text


async def resolve(specs: list[tuple[str | None, str]], twitch: TwitchPublic,
                  kick: KickPublic) -> Resolution:
    """specs : (plateforme forcée ou None, pseudo). Un pseudo sans plateforme est
    cherché sur Twitch ET Kick, et gardé partout où le compte existe."""
    res = Resolution()
    tw_names = [n for p, n in specs if p in (None, "twitch")]
    kk_names = [n for p, n in specs if p in (None, "kick")]

    tw_found: dict[str, dict | None] = {}
    tw_ok = True
    if tw_names:
        try:
            tw_found = await twitch.users(tw_names)
        except Exception as e:
            log.warning("Vérification Twitch impossible : %s", e)
            tw_ok = False

    async def one(slug: str):
        try:
            return slug, await kick.channel(slug), True
        except Exception as e:
            log.debug("Kick %s : %s", slug, e)
            return slug, None, False
    kk = {s: (d, ok) for s, d, ok in await asyncio.gather(*(one(s) for s in dict.fromkeys(kk_names)))}

    for forced, name in specs:
        on_tw = forced in (None, "twitch") and tw_ok and tw_found.get(name) is not None
        kd, k_ok = kk.get(name, (None, True))
        on_kk = forced in (None, "kick") and kd is not None
        if on_tw:
            res.twitch.append(name)
            res.targets.append((Platform.TWITCH, name))
        if on_kk:
            res.kick.append(name)
            res.targets.append((Platform.KICK, name))
        if not on_tw and not on_kk:
            unsure = (forced in (None, "twitch") and not tw_ok) or \
                     (forced in (None, "kick") and not k_ok)
            if unsure:
                # Vérification impossible : on garde le pseudo tel quel plutôt que de le perdre
                res.unchecked.append(name)
                res.targets.append((Platform.KICK if forced == "kick" else Platform.TWITCH, name))
            else:
                res.missing.append(name)
    res.targets = list(dict.fromkeys(res.targets))
    return res
