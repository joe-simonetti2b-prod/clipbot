"""
Sauvegarde de l'état dans Telegram, pour les hébergeurs sans disque (Render gratuit).

Render efface le disque à chaque redémarrage : sans ça, la connexion TikTok/YouTube
et les réglages (/auto, /pause, chaînes ajoutées) seraient perdus à chaque mise à jour.

Les clés importantes sont chiffrées puis écrites dans UN message épinglé de ta
conversation avec le bot. Au démarrage, le bot relit ce message et restaure tout.
Chiffrement : flux SHA-256 dérivé du jeton du bot + HMAC (aucune dépendance) — le
message est illisible sans le jeton du bot, qui ne quitte jamais Render.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os

log = logging.getLogger(__name__)

PERSISTED = ("tiktok_tokens", "youtube_tokens", "auto_publish", "paused",
             "extra_channels", "telegram_owner", "youtube_quota",
             "pinned_channels", "watermark", "outro", "whop_campaigns",
             "tiktok_inbox_log", "manual_log", "channel_perf_boost", "rewards_last_pct",
             "tiktok_analytics_denied")
HEADER = "💾 Sauvegarde clipbot — ne pas supprimer ni désépingler\n"


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, counter = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def seal(data: dict, secret: str) -> str:
    key = hashlib.sha256(("clipbot-backup:" + secret).encode()).digest()
    raw = json.dumps(data, separators=(",", ":")).encode()
    nonce = os.urandom(12)
    ct = bytes(a ^ b for a, b in zip(raw, _keystream(key, nonce, len(raw))))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + tag + ct).decode()


def unseal(token: str, secret: str) -> dict | None:
    try:
        key = hashlib.sha256(("clipbot-backup:" + secret).encode()).digest()
        blob = base64.urlsafe_b64decode(token.strip().encode())
        nonce, tag, ct = blob[:12], blob[12:28], blob[28:]
        if not hmac.compare_digest(tag, hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]):
            return None
        raw = bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))
        return json.loads(raw)
    except Exception:
        return None


class TelegramBackup:
    def __init__(self, tg, store, secret: str):
        self.tg = tg
        self.store = store
        self.secret = secret
        self._dirty = asyncio.Event()
        self._last = ""

    def snapshot(self) -> dict:
        return {k: v for k in PERSISTED if (v := self.store.get(k)) is not None}

    async def restore(self) -> int:
        """Restaure les clés absentes du disque. Renvoie le nombre de clés restaurées."""
        if not (self.tg.enabled and self.tg.owner):
            return 0
        try:
            chat = await self.tg._call("getChat", json={"chat_id": self.tg.owner})
        except Exception as e:
            log.warning("Sauvegarde Telegram illisible : %s", e)
            return 0
        text = (chat.get("pinned_message") or {}).get("text", "")
        if not text.startswith(HEADER.strip()[:20]):
            return 0
        data = unseal(text.split("\n", 1)[-1], self.secret)
        if not data:
            log.warning("Sauvegarde Telegram invalide (jeton du bot changé ?)")
            return 0
        n = 0
        for k, v in data.items():
            if k in PERSISTED and self.store.get(k) is None:
                self.store.set(k, v)
                n += 1
        self.store.set("backup_message_id", chat["pinned_message"]["message_id"])
        self._last = json.dumps(self.snapshot(), sort_keys=True)
        log.info("État restauré depuis Telegram (%d éléments)", n)
        return n

    def mark_dirty(self, key: str) -> None:
        if key in PERSISTED:
            self._dirty.set()

    async def run(self) -> None:
        """Écrit la sauvegarde quand un réglage change (regroupé toutes les 5 s)."""
        while True:
            await self._dirty.wait()
            await asyncio.sleep(5)
            self._dirty.clear()
            try:
                await self._write()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Sauvegarde Telegram impossible : %s", e)

    async def _write(self) -> None:
        if not (self.tg.enabled and self.tg.owner):
            return
        snap = self.snapshot()
        state = json.dumps(snap, sort_keys=True)
        if state == self._last:
            return
        text = HEADER + seal(snap, self.secret)
        mid = self.store.get("backup_message_id")
        if mid:
            try:
                await self.tg._call("editMessageText", json={
                    "chat_id": self.tg.owner, "message_id": mid, "text": text})
                self._last = state
                return
            except Exception:
                pass  # message supprimé : on en recrée un
        msg = await self.tg._call("sendMessage", json={
            "chat_id": self.tg.owner, "text": text, "disable_notification": True})
        await self.tg._call("pinChatMessage", json={
            "chat_id": self.tg.owner, "message_id": msg["message_id"],
            "disable_notification": True})
        self.store.set("backup_message_id", msg["message_id"])
        self._last = state
