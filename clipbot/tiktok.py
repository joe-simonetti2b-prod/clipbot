"""
Publication TikTok via l'API officielle Content Posting.

  * Connexion : OAuth 2 (Login Kit). Tu cliques UNE fois sur le lien envoyé
    par le bot Telegram ; les jetons sont ensuite rafraîchis automatiquement
    (jeton d'accès 24 h, jeton de rafraîchissement ~1 an).
  * TIKTOK_MODE=inbox  : la vidéo arrive en brouillon dans ton app TikTok
    (notification) -> tu touches « Publier ». Marche avant l'audit TikTok.
    Limite TikTok : 5 brouillons en attente par 24 h.
  * TIKTOK_MODE=direct : publication directe. Tant que l'app n'est pas
    auditée par TikTok, les vidéos sont forcées en privé ; après audit elles
    sortent en public sans aucune action de ta part.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import aiohttp

from .storage import Store

log = logging.getLogger(__name__)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
# Autorisations demandées : uniquement celles du mode choisi. TikTok refuse toute la
# connexion (« scope ») si on demande une autorisation non cochée dans l'app.
SCOPES_BY_MODE = {
    "inbox": "user.info.basic,video.upload",               # brouillons (Sandbox OK)
    "direct": "user.info.basic,video.upload,video.publish",  # publication directe (app validée)
}
MAX_SINGLE_CHUNK = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


def _fields(names: list[str]) -> str:
    return ",".join(names)


class TikTokClient:
    def __init__(self, client_key: str, client_secret: str, mode: str, public_url: str,
                 store: Store, session: aiohttp.ClientSession, analytics: bool = True):
        self.key = client_key
        self.secret = client_secret
        self.mode = mode
        self.analytics_wanted = analytics
        self.redirect_uri = f"{public_url}/tiktok/callback"
        self.store = store
        self.session = session

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    @property
    def connected(self) -> bool:
        tok = self.store.get("tiktok_tokens")
        return bool(tok and tok.get("refresh_expires_at", 0) > time.time())

    # ---------------------------------------------------------------- OAuth
    def login_url(self) -> str:
        state = secrets.token_urlsafe(16)
        self.store.set("tiktok_oauth_state", state)
        scopes = os.getenv("TIKTOK_SCOPES") or SCOPES_BY_MODE.get(self.mode, SCOPES_BY_MODE["inbox"])
        q = {"client_key": self.key, "scope": scopes, "response_type": "code",
             "redirect_uri": self.redirect_uri, "state": state}
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    async def check_credentials(self) -> tuple[bool | None, str]:
        """Vérifie la paire client key / secret auprès de TikTok (jeton « application »).
        Renvoie (True, …) si acceptée, (False, raison) si refusée, (None, raison) si indéterminé."""
        try:
            async with self.session.post(
                TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"client_key": self.key, "client_secret": self.secret,
                      "grant_type": "client_credentials"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                payload = await r.json(content_type=None)
        except Exception as e:
            return None, f"TikTok injoignable ({e})"
        if payload.get("access_token"):
            return True, "identifiants acceptés"
        err, desc = payload.get("error", ""), payload.get("error_description", "")
        if err in ("invalid_client", "invalid_client_key", "invalid_client_secret"):
            return False, desc or err
        return None, f"{err} {desc}".strip() or str(payload)[:200]

    def note_auth_error(self, err: str) -> bool:
        """TikTok a refusé la connexion avant même l'échange du code (souvent : un scope
        demandé n'est pas activé sur l'app dans le portail développeur). Si c'est le cas et
        qu'on avait demandé les scopes d'analytics, on les retire et on dit de relancer."""
        if self.analytics_wanted and any(w in err.lower() for w in ("scope", "permission")):
            self.store.set("tiktok_analytics_denied", True)
            return True
        return False

    async def handle_callback(self, code: str, state: str) -> None:
        if not state or state != self.store.get("tiktok_oauth_state"):
            raise TikTokError("state OAuth invalide (lien expiré ? relance /tiktok)")
        await self._token_request({
            "code": code, "grant_type": "authorization_code", "redirect_uri": self.redirect_uri,
        })

    async def _token_request(self, extra: dict) -> None:
        data = {"client_key": self.key, "client_secret": self.secret, **extra}
        async with self.session.post(
            TOKEN_URL, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            payload = await r.json(content_type=None)
        if "access_token" not in payload:
            raise TikTokError(f"échange de jeton refusé : {payload}")
        now = time.time()
        self.store.set("tiktok_tokens", {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "open_id": payload.get("open_id"),
            "expires_at": now + int(payload.get("expires_in", 86400)),
            "refresh_expires_at": now + int(payload.get("refresh_expires_in", 31_536_000)),
        })

    async def _access_token(self) -> str:
        tok = self.store.get("tiktok_tokens")
        if not tok:
            raise TikTokError("TikTok non connecté (envoie /tiktok au bot)")
        if tok["expires_at"] - time.time() < 300:
            await self._token_request({"grant_type": "refresh_token",
                                       "refresh_token": tok["refresh_token"]})
            tok = self.store.get("tiktok_tokens")
        return tok["access_token"]

    # ------------------------------------------------------------ requêtes
    async def _post(self, path: str, body: dict) -> dict:
        token = await self._access_token()
        async with self.session.post(
            f"{API}{path}", json=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=UTF-8"},
        ) as r:
            payload = await r.json(content_type=None)
        err = payload.get("error", {})
        if err.get("code") not in (None, "ok"):
            raise TikTokError(f"{path} : {err.get('code')} — {err.get('message')}")
        return payload.get("data", {})

    async def _upload(self, upload_url: str, path: Path, size: int, chunk: int) -> None:
        with path.open("rb") as f:
            first = 0
            while first < size:
                # Le dernier morceau absorbe le reste (règle de l'API TikTok)
                last = size - 1 if size - first < 2 * chunk else first + chunk - 1
                f.seek(first)
                data = f.read(last - first + 1)
                async with self.session.put(upload_url, data=data, headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {first}-{last}/{size}",
                }) as r:
                    if r.status not in (200, 201, 206):
                        raise TikTokError(f"upload HTTP {r.status} : {(await r.text())[:200]}")
                first = last + 1

    def _source(self, size: int) -> tuple[dict, int]:
        chunk = size if size <= MAX_SINGLE_CHUNK else CHUNK
        return ({"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                 "total_chunk_count": max(1, size // chunk)}, chunk)

    async def publish(self, path: Path, caption: str, branded: bool = False) -> str:
        size = path.stat().st_size
        source, chunk = self._source(size)
        if self.mode == "direct":
            info = await self._post("/post/publish/creator_info/query/", {})
            options = info.get("privacy_level_options") or ["SELF_ONLY"]
            privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else options[0]
            data = await self._post("/post/publish/video/init/", {
                "post_info": {
                    "title": caption, "privacy_level": privacy,
                    "disable_duet": False, "disable_stitch": False, "disable_comment": False,
                    "brand_content_toggle": branded, "brand_organic_toggle": False,
                    "video_cover_timestamp_ms": 1000,
                },
                "source_info": source,
            })
        else:
            data = await self._post("/post/publish/inbox/video/init/", {"source_info": source})

        await self._upload(data["upload_url"], path, size, chunk)
        publish_id = data["publish_id"]
        status = await self._wait_status(publish_id)
        log.info("TikTok %s : %s (%s)", self.mode, publish_id, status)
        return publish_id

    # ------------------------------------------------------------ analytics
    async def creator_stats(self) -> dict | None:
        """Abonnés, mentions J'aime totales, nb de vidéos (scope user.info.stats)."""
        if not self.connected:
            return None
        try:
            token = await self._access_token()
            fields = _fields(["display_name", "follower_count", "likes_count", "video_count"])
            async with self.session.get(
                f"{API}/user/info/", params={"fields": fields},
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                payload = await r.json(content_type=None)
        except Exception as e:
            log.warning("Stats TikTok indisponibles : %s", e)
            return None
        err = (payload.get("error") or {}).get("code")
        if err not in (None, "ok"):
            if err in ("scope_not_authorized", "access_token_invalid"):
                self.store.set("tiktok_analytics_denied", True)
                log.warning("Scope stats TikTok non autorisé -> analytics coupées (%s)", err)
            return None
        return (payload.get("data") or {}).get("user")

    async def video_list(self, max_count: int = 20) -> list[dict]:
        """Vidéos publiées récemment, avec leurs vues (scope video.list)."""
        if not self.connected:
            return []
        try:
            token = await self._access_token()
            fields = _fields(["id", "create_time", "share_url", "video_description",
                              "view_count", "like_count", "comment_count", "share_count"])
            async with self.session.post(
                f"{API}/video/list/", params={"fields": fields},
                json={"max_count": max_count},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=UTF-8"},
            ) as r:
                payload = await r.json(content_type=None)
        except Exception as e:
            log.warning("Liste des vidéos TikTok indisponible : %s", e)
            return []
        err = (payload.get("error") or {}).get("code")
        if err not in (None, "ok"):
            if err in ("scope_not_authorized", "access_token_invalid"):
                self.store.set("tiktok_analytics_denied", True)
                log.warning("Scope video.list TikTok non autorisé -> analytics coupées (%s)", err)
            return []
        return (payload.get("data") or {}).get("videos") or []

    async def _wait_status(self, publish_id: str, tries: int = 20) -> str:
        status = "PROCESSING_UPLOAD"
        for _ in range(tries):
            await asyncio.sleep(6)
            data = await self._post("/post/publish/status/fetch/", {"publish_id": publish_id})
            status = data.get("status", status)
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return status
            if status == "FAILED":
                raise TikTokError(f"TikTok a rejeté la vidéo : {data.get('fail_reason')}")
        return status
