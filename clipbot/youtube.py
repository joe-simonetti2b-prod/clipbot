"""
Publication YouTube Shorts via l'API officielle YouTube Data v3.

  * Connexion : OAuth Google, une seule fois via le lien envoyé par /youtube.
    Le jeton de rafraîchissement permet ensuite de publier sans intervention.
  * Un Short = vidéo verticale de 3 min max ; « #Shorts » est ajouté au titre.
  * Quota gratuit : 10 000 unités/jour, un envoi coûte ~1 600 unités
    -> 6 Shorts par jour maximum ; le bot s'arrête proprement au-delà.
  * Tant que ton projet Google Cloud n'a pas passé l'audit de conformité de
    l'API YouTube, Google force les vidéos envoyées par API en privé : il
    suffira alors de les passer en public depuis l'app YouTube Studio.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
import urllib.parse
from pathlib import Path

import aiohttp

from .storage import Store

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status")
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
DAILY_LIMIT = 6


class YouTubeError(RuntimeError):
    pass


def build_metadata(caption: str, hook: str, privacy: str) -> dict:
    """Titre (100 car. max, #Shorts), description = légende, tags = hashtags."""
    tags = [t.lstrip("#") for t in re.findall(r"#\w+", caption)][:15]
    base = (hook or caption.split("\n")[0] or "Clip").replace("<", "").replace(">", "").strip()
    title = f"{base[:90].rstrip()} #Shorts"
    return {
        "snippet": {"title": title, "description": caption[:4900], "tags": tags,
                    "categoryId": "20"},  # 20 = Gaming (le plus proche du streaming)
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }


class YouTubeClient:
    def __init__(self, client_id: str, client_secret: str, privacy: str, public_url: str,
                 store: Store, session: aiohttp.ClientSession):
        self.client_id = client_id
        self.secret = client_secret
        self.privacy = privacy if privacy in ("public", "unlisted", "private") else "public"
        self.redirect_uri = f"{public_url}/youtube/callback"
        self.store = store
        self.session = session

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.secret)

    @property
    def connected(self) -> bool:
        return bool(self.store.get("youtube_tokens"))

    # ---------------------------------------------------------------- OAuth
    def login_url(self) -> str:
        state = secrets.token_urlsafe(16)
        self.store.set("youtube_oauth_state", state)
        q = {"client_id": self.client_id, "redirect_uri": self.redirect_uri,
             "response_type": "code", "scope": SCOPE, "access_type": "offline",
             "prompt": "consent", "include_granted_scopes": "true", "state": state}
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    async def handle_callback(self, code: str, state: str) -> None:
        if not state or state != self.store.get("youtube_oauth_state"):
            raise YouTubeError("state OAuth invalide (lien expiré ? relance /youtube)")
        tok = await self._token({"code": code, "grant_type": "authorization_code",
                                 "redirect_uri": self.redirect_uri})
        if "refresh_token" not in tok:
            raise YouTubeError("Google n'a pas renvoyé de jeton de rafraîchissement")
        self._save(tok, tok["refresh_token"])

    async def _token(self, extra: dict) -> dict:
        data = {"client_id": self.client_id, "client_secret": self.secret, **extra}
        async with self.session.post(TOKEN_URL, data=data) as r:
            payload = await r.json(content_type=None)
        if "access_token" not in payload:
            raise YouTubeError(f"jeton refusé : {payload.get('error_description') or payload}")
        return payload

    def _save(self, tok: dict, refresh: str) -> None:
        self.store.set("youtube_tokens", {
            "access_token": tok["access_token"], "refresh_token": refresh,
            "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        })

    async def _access_token(self) -> str:
        tok = self.store.get("youtube_tokens")
        if not tok:
            raise YouTubeError("YouTube non connecté (envoie /youtube au bot)")
        if tok["expires_at"] - time.time() < 300:
            new = await self._token({"grant_type": "refresh_token",
                                     "refresh_token": tok["refresh_token"]})
            self._save(new, tok["refresh_token"])
            tok = self.store.get("youtube_tokens")
        return tok["access_token"]

    # --------------------------------------------------------------- envoi
    def _quota_left(self) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 3600))  # quota remis à minuit (heure du Pacifique)
        used = self.store.get("youtube_quota", {})
        return DAILY_LIMIT - (used.get("n", 0) if used.get("day") == day else 0)

    def _count_upload(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 3600))
        used = self.store.get("youtube_quota", {})
        n = used.get("n", 0) + 1 if used.get("day") == day else 1
        self.store.set("youtube_quota", {"day": day, "n": n})

    async def publish(self, path: Path, caption: str, hook: str) -> str:
        if self._quota_left() <= 0:
            raise YouTubeError("quota YouTube du jour atteint (6 Shorts) — reprise demain")
        token = await self._access_token()
        size = path.stat().st_size
        async with self.session.post(
            UPLOAD_URL, json=build_metadata(caption, hook, self.privacy),
            headers={"Authorization": f"Bearer {token}",
                     "X-Upload-Content-Type": "video/mp4",
                     "X-Upload-Content-Length": str(size)},
        ) as r:
            if r.status != 200:
                raise YouTubeError(f"init HTTP {r.status} : {(await r.text())[:300]}")
            location = r.headers.get("Location")
        if not location:
            raise YouTubeError("pas d'URL d'envoi renvoyée par YouTube")
        with path.open("rb") as f:
            async with self.session.put(
                location, data=f,
                headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
                timeout=aiohttp.ClientTimeout(total=900),
            ) as r:
                body = await r.json(content_type=None)
                if r.status not in (200, 201):
                    raise YouTubeError(f"envoi HTTP {r.status} : {str(body)[:300]}")
        self._count_upload()
        vid = body.get("id", "")
        log.info("YouTube Short publié : https://youtube.com/shorts/%s (%s)", vid, self.privacy)
        return vid
