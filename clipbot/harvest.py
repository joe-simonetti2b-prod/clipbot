"""
Clips « déjà validés par le public » : deux sources en plus de la détection de hype.

  1. Liens de clips postés dans le chat d'un live suivi (clips.twitch.tv/…,
     twitch.tv/<chaîne>/clip/…, kick.com/<chaîne>?clip=…) : un viewer a jugé le
     moment assez fort pour le clipper — et plusieurs viewers qui postent le même
     lien, c'est encore mieux. Le clip est téléchargé puis monté comme les autres.
  2. Meilleurs clips Twitch des dernières 24 h des créateurs suivis et « focus »
     (API publique GQL) : contenu dont l'audience est déjà prouvée.

Les deux passent ensuite par la même chaîne : transcription, note IA (un clip
faible reste jeté), montage 9:16, sous-titres, publication.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import aiohttp

from .models import StreamCandidate
from .public import TWITCH_GQL, TWITCH_WEB_CLIENT_ID
from .resources import Resources, niced
from .storage import Store

log = logging.getLogger(__name__)

MAX_CLIP_S = 90
# Défense en profondeur : même si le regex d'appel est déjà strict, on revérifie l'hôte
# juste avant de lancer yt-dlp (aucune URL arbitraire postée dans un chat n'est téléchargée).
ALLOWED_HOSTS = {"clips.twitch.tv", "twitch.tv", "www.twitch.tv", "m.twitch.tv",
                 "kick.com", "www.kick.com", "m.kick.com"}
PER_CREATOR_GAP_S = 10 * 60      # au plus un clip de viewer par créateur toutes les 10 min
MAX_PER_HOUR = 6
TOP_EVERY_S = 30 * 60
TOP_MIN_VIEWS = 300


def clip_id(url: str) -> str:
    """Identifiant stable d'un clip, quelle que soit la forme du lien."""
    m = re.search(r"(clip_[A-Za-z0-9]+)", url) or re.search(r"/clip/([A-Za-z0-9_-]+)", url) \
        or re.search(r"clips\.twitch\.tv/(?:embed\?clip=)?([A-Za-z0-9_-]+)", url)
    return (m.group(1) if m else url).lower()


def accept_owner(info: dict, allowed: set[str]) -> bool:
    """Le clip vient-il d'une chaîne qu'on suit ? (sans info : accepté)"""
    owners = {str(info.get(k) or "").lower().replace(" ", "")
              for k in ("uploader", "uploader_id", "channel", "channel_id", "creator")} - {""}
    return not owners or bool(owners & allowed)


class ClipHarvester:
    def __init__(self, store: Store, session: aiohttp.ClientSession, work_dir: Path,
                 on_new: Callable[[], None], allowed: Callable[[], set[str]],
                 top_channels: Callable[[], list[str]], enabled_top: bool = True,
                 res: Resources | None = None, esport: Callable[[], bool] = lambda: False):
        self.store = store
        self.session = session
        self.res = res
        self.esport = esport
        self.dir = work_dir / "clips"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.on_new = on_new
        self.allowed = allowed
        self.top_channels = top_channels
        self.enabled_top = enabled_top
        self.queue: asyncio.Queue[tuple[StreamCandidate, str, str, float]] = asyncio.Queue(50)
        self._seen: dict[str, float] = {}
        self._mentions: dict[str, set[str]] = {}
        self._last_by_creator: dict[str, float] = {}
        self._recent: list[float] = []
        self._top_disabled = False
        self._basic_query = False

    # ------------------------------------------------------------ entrée chat
    def offer(self, c: StreamCandidate, url: str, author: str) -> None:
        cid = clip_id(url)
        self._mentions.setdefault(cid, set()).add(author.lower())
        if cid in self._seen or self._known(cid):
            return
        now = time.time()
        creator = c.channel.lower()
        self._recent = [t for t in self._recent if now - t < 3600]
        if now - self._last_by_creator.get(creator, 0) < PER_CREATOR_GAP_S \
                or len(self._recent) >= MAX_PER_HOUR:
            return
        self._seen[cid] = now
        self._last_by_creator[creator] = now
        self._recent.append(now)
        try:
            self.queue.put_nowait((c, url, f"posté dans le chat par {author}", 0.0))
            log.info("📎 Clip posté dans le chat de %s par %s : %s", c.channel, author, url)
        except asyncio.QueueFull:
            pass

    OWNER_NOTE = "envoyé par toi"

    def trusted(self, note: str) -> bool:
        return note == self.OWNER_NOTE

    def submit(self, url: str) -> str:
        """Lien de clip envoyé par le propriétaire au bot : monté pour NOTRE compte,
        quelle que soit la chaîne, en priorité maximale."""
        url = clip_url(url)
        if not url:
            return "Lien non pris en charge : envoie le lien d'un CLIP Twitch ou Kick."
        cid = clip_id(url)
        if self._known(cid):
            return "Ce clip est déjà passé par le bot."
        from .models import Platform
        plat = Platform.KICK if "kick.com" in url else Platform.TWITCH
        c = StreamCandidate(platform=plat, channel="?", stream_id=cid, url=url,
                            title="", category="", viewers=0)
        self._seen[cid] = time.time()
        try:
            self.queue.put_nowait((c, url, self.OWNER_NOTE, 0.0))
        except asyncio.QueueFull:
            return "File de téléchargement pleine, renvoie-le dans quelques minutes."
        return "📥 Reçu : je le télécharge et je le monte pour ton compte (priorité max)."

    def _known(self, cid: str) -> bool:
        row = self.store.db.execute(
            "SELECT 1 FROM clips WHERE stream_url LIKE ? LIMIT 1", (f"%{cid}%",)).fetchone()
        return row is not None

    # ------------------------------------------------------------ téléchargement
    async def run(self) -> None:
        tasks = [asyncio.create_task(self._worker())]
        if self.enabled_top:
            tasks.append(asyncio.create_task(self._top_loop()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    async def _worker(self) -> None:
        while True:
            c, url, note, views = await self.queue.get()
            try:
                await self._fetch(c, url, note, views)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Clip %s non récupéré : %s", url, e)

    async def _fetch(self, c: StreamCandidate, url: str, note: str, views: float) -> None:
        cid = clip_id(url)
        path, info, duration = await download_clip(url, self.dir, self.res)
        trusted = self.trusted(note)
        if not (trusted or accept_owner(info, self.allowed() | {c.channel.lower()})):
            path.unlink(missing_ok=True)
            log.info("Clip %s ignoré : chaîne non suivie (%s)", url, info.get("uploader"))
            return
        views = views or float(info.get("view_count") or 0)
        mentions = len(self._mentions.get(cid, ()))
        if trusted:
            reason, score = "clip_viewer", 10.0
            c.channel = broadcaster(info) or "clip"
        elif note.startswith("posté"):
            reason, score = "clip_viewer", 9.0 + min(mentions - 1, 3)
        else:
            reason, score = "top_clip", min(10.0, 5.0 + math.log10(max(views, 1)))
        self.store.add_clip(
            platform=c.platform.value, channel=c.channel, stream_url=url,
            stream_title=str(info.get("title") or c.title)[:200], category=c.category,
            reason=reason, score=score,
            tokens=note + (f" · {int(views)} vues" if views else ""),
            raw_path=str(path), trim_offset=0.0, trim_duration=min(duration, MAX_CLIP_S),
        )
        log.info("Clip %s (%s) ajouté à la file : %.0fs, score %.1f", cid, reason, duration, score)
        self.on_new()

    # ------------------------------------------------------------ top clips du jour
    async def _top_loop(self) -> None:
        await asyncio.sleep(120)            # laisse la veille démarrer
        while not self._top_disabled:
            try:
                await self._harvest_top()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Top clips Twitch indisponibles : %s", e)
            # Esport : les clips d'un match explosent en quelques minutes, on passe plus souvent
            await asyncio.sleep(TOP_EVERY_S * (2 / 3 if self.esport() else 1))

    async def top_clips(self, logins: list[str]) -> list[dict]:
        # Champs en plus (date, jeu) pour classer par vitesse et filtrer l'esport ; si Twitch
        # les refuse un jour, on retombe sur la requête simple (déjà éprouvée).
        extra = "" if self._basic_query else " createdAt game { name }"
        query = ("query { users(logins: %s) { login clips(first: 5, criteria: {filter: LAST_DAY}) "
                 "{ edges { node { slug title viewCount durationSeconds url%s } } } } }"
                 % (json.dumps(logins[:30]), extra))
        async with self.session.post(TWITCH_GQL, json={"query": query},
                                     headers={"Client-ID": TWITCH_WEB_CLIENT_ID},
                                     timeout=aiohttp.ClientTimeout(total=20)) as r:
            payload = await r.json(content_type=None)
        if payload.get("errors") and not (payload.get("data") or {}).get("users"):
            if not self._basic_query:
                log.warning("Top clips : champs étendus refusés (%s) -> requête simple",
                            payload["errors"][0].get("message"))
                self._basic_query = True
                return await self.top_clips(logins)
            raise RuntimeError(payload["errors"][0].get("message"))
        out = []
        for u in (payload.get("data") or {}).get("users") or []:
            for e in (((u or {}).get("clips") or {}).get("edges") or []):
                n = e.get("node") or {}
                if n.get("slug"):
                    out.append({"login": u["login"].lower(), **n})
        return out

    def pick_top(self, clips: list[dict], n: int, now: float | None = None) -> list[dict]:
        """Les clips qui MONTENT le plus vite (vues / heure depuis leur création), pas les
        plus vus en absolu : un clip de 30 min déjà à 2 000 vues vaut mieux qu'un clip de
        20 h à 5 000 — les autres comptes de clips l'ont déjà tous posté."""
        now = now or time.time()
        esport = self.esport()
        from . import esport as es

        def age_h(c):
            try:
                ts = datetime.fromisoformat(str(c["createdAt"]).replace("Z", "+00:00")).timestamp()
                return max(0.25, (now - ts) / 3600)
            except (KeyError, ValueError):
                return 12.0                        # date inconnue : âge moyen supposé
        out = []
        for c in sorted(clips, key=lambda c: (c.get("viewCount") or 0) / age_h(c), reverse=True):
            if (c.get("viewCount") or 0) < TOP_MIN_VIEWS:
                continue
            game = ((c.get("game") or {}).get("name") or "").lower()
            if esport and game and game not in es.GAMES:
                continue                           # ex : Kameto en Just Chatting
            cid = str(c["slug"]).lower()
            if cid in self._seen or self._known(cid):
                continue
            out.append(c)
            if len(out) >= n:
                break
        return out

    async def _harvest_top(self) -> None:
        logins = list(dict.fromkeys(self.top_channels()))
        if not logins:
            return
        clips = await self.top_clips(logins)
        best = self.pick_top(clips, 2 if self.esport() else 1)
        if not best:
            log.info("Top clips : rien de nouveau (%d clips vus)", len(clips))
            return
        from .models import Platform
        for b in best:
            cid = str(b["slug"]).lower()
            self._seen[cid] = time.time()
            url = b.get("url") or f"https://clips.twitch.tv/{b['slug']}"
            c = StreamCandidate(platform=Platform.TWITCH, channel=b["login"], stream_id=cid,
                                url=f"https://www.twitch.tv/{b['login']}", title=b.get("title") or "",
                                category=(b.get("game") or {}).get("name") or "", viewers=0)
            log.info("🏆 Top clip : %s (%s, %s vues)", b.get("title"), b["login"], b.get("viewCount"))
            await self.queue.put((c, url, "top clip du jour", float(b.get("viewCount") or 0)))


def clip_url(url: str) -> str:
    """Lien de clip Twitch/Kick normalisé, ou "" si ce n'en est pas un (ex : une chaîne)."""
    from .watcher import CLIP_LINK_RE
    m = CLIP_LINK_RE.match(url.strip())
    return m.group(0) if m else ""


def broadcaster(info: dict) -> str:
    """Chaîne d'origine d'un clip (et non la personne qui l'a clippé)."""
    for k in ("channel", "uploader_id", "uploader"):
        v = str(info.get(k) or "").strip()
        if v:
            return v.lower().replace(" ", "")
    return ""


async def download_clip(url: str, dest: Path, res=None) -> tuple[Path, dict, float]:
    """Télécharge un clip Twitch/Kick (lien vérifié). Un seul travail lourd à la fois
    (mémoire limitée) et en priorité basse. Renvoie (fichier, infos, durée)."""
    if urlparse(url).hostname not in ALLOWED_HOSTS:
        raise RuntimeError(f"hôte non autorisé : {url}")
    # Uniquement un vrai lien de CLIP (jamais une chaîne en direct : yt-dlp enregistrerait
    # le live sans fin). On ne passe à yt-dlp que la partie reconnue du lien.
    url = clip_url(url)
    if not url:
        raise RuntimeError("ce n'est pas un lien de clip Twitch/Kick")
    dest.mkdir(parents=True, exist_ok=True)
    base = dest / f"clip_{re.sub(r'[^a-z0-9_-]', '', clip_id(url))[:60]}_{int(time.time())}"
    cmd = niced(["yt-dlp", "--quiet", "--no-warnings", "--no-playlist", "--no-part",
                 "--match-filter", "!is_live", "-f", "best[height<=720]/best",
                 "--max-filesize", "120M", "--write-info-json", "-o", f"{base}.%(ext)s", url])

    async def run():
        # Groupe de processus à part : en cas de dépassement on tue AUSSI les ffmpeg
        # lancés par yt-dlp (sinon ils continueraient à tourner en arrière-plan).
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 180)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            for p in dest.glob(base.name + "*"):
                p.unlink(missing_ok=True)
            raise RuntimeError("téléchargement trop long")
        return proc.returncode, err

    if res is not None:
        async with res.heavy:
            await res.wait_room(120, max_wait_s=120)
            code, err = await run()
    else:
        code, err = await run()
    info_path = base.with_suffix(".info.json")
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    info_path.unlink(missing_ok=True)
    files = [p for p in dest.glob(base.name + ".*") if p.suffix not in (".json", ".part")]
    if code != 0 or not files:
        for p in files:
            p.unlink(missing_ok=True)
        raise RuntimeError(err.decode(errors="ignore").strip()[-200:] or "yt-dlp a échoué")
    path = files[0]
    duration = float(info.get("duration") or 0) or await _probe_duration(path)
    if not duration or duration < 3:
        path.unlink(missing_ok=True)
        raise RuntimeError("clip vide ou trop court")
    return path, info, duration


async def _probe_duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0
