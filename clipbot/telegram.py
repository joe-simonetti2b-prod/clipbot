"""
Télécommande Telegram : ton tableau de bord depuis le téléphone.

  /start <code>   appairer ton compte (code = TELEGRAM_PAIR_CODE)
  /status         lives suivis, clips du jour, modes actifs
  /auto on|off    publication sans validation
  /pause /resume  arrêter / reprendre la surveillance des lives
  /add <chaîne>   suivre une chaîne (ex : /add kamet0  ou  /add kick:xxx)
  /remove <chaîne>, /chaines
  /tiktok         lien de connexion TikTok (une seule fois)
  /youtube        lien de connexion YouTube Shorts (une seule fois)

Chaque clip prêt arrive en vidéo avec deux boutons : ✅ Publier / ❌ Jeter.
API Bot HTTP brute (long polling) : aucune dépendance, aucun webhook à régler.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from .storage import Store

log = logging.getLogger(__name__)

Handler = Callable[[list[str]], Awaitable[str]]


class TelegramBot:
    def __init__(self, token: str, pair_code: str, store: Store, session: aiohttp.ClientSession,
                 owner_id: str = ""):
        self.api = f"https://api.telegram.org/bot{token}"
        self.enabled = bool(token)
        self.pair_code = pair_code
        self.owner_id = int(owner_id) if owner_id.strip().lstrip("-").isdigit() else None
        self.store = store
        self.session = session
        self.commands: dict[str, Handler] = {}
        self.on_approve: Callable[[int], Awaitable[str]] | None = None
        self.on_reject: Callable[[int], Awaitable[str]] | None = None

    @property
    def owner(self) -> int | None:
        # TELEGRAM_OWNER_ID (variable d'environnement) est prioritaire et verrouille le bot
        return self.owner_id or self.store.get("telegram_owner")

    # ------------------------------------------------------------ envoi
    async def _call(self, method: str, **kwargs) -> dict:
        async with self.session.post(f"{self.api}/{method}", **kwargs) as r:
            data = await r.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} : {data.get('description')}")
        return data["result"]

    async def send(self, text: str, chat_id: int | None = None) -> None:
        chat_id = chat_id or self.owner
        if not (self.enabled and chat_id):
            return
        try:
            await self._call("sendMessage", json={
                "chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True,
            })
        except Exception as e:
            log.warning("Envoi Telegram impossible : %s", e)

    async def send_clip(self, clip_id: int, path: Path, caption: str, header: str) -> int | None:
        if not (self.enabled and self.owner):
            return None
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Publier", "callback_data": f"pub:{clip_id}"},
            {"text": "❌ Jeter", "callback_data": f"rej:{clip_id}"},
        ]]}
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self.owner))
        form.add_field("caption", f"{header}\n\n{caption}"[:1024])
        form.add_field("supports_streaming", "true")
        form.add_field("reply_markup", json.dumps(keyboard))
        try:
            with path.open("rb") as f:
                form.add_field("video", f, filename=path.name, content_type="video/mp4")
                msg = await self._call("sendVideo", data=form,
                                       timeout=aiohttp.ClientTimeout(total=300))
            return msg["message_id"]
        except Exception as e:
            log.error("Envoi du clip %s sur Telegram impossible : %s", clip_id, e)
            return None

    # ----------------------------------------------------------- réception
    async def run(self) -> None:
        if not self.enabled:
            log.warning("TELEGRAM_BOT_TOKEN absent : pas de télécommande.")
            return
        await self._call("setMyCommands", json={"commands": [
            {"command": c, "description": d} for c, d in [
                ("status", "État du bot"), ("auto", "on/off publication auto"),
                ("pause", "Mettre en pause"), ("resume", "Reprendre"),
                ("add", "Suivre une chaîne"), ("remove", "Ne plus suivre"),
                ("chaines", "Chaînes suivies"), ("tiktok", "Connecter TikTok"),
                ("youtube", "Connecter YouTube Shorts"),
            ]]})
        offset = self.store.get("telegram_offset", 0)
        while True:
            try:
                updates = await self._call(
                    "getUpdates", json={"offset": offset, "timeout": 50},
                    timeout=aiohttp.ClientTimeout(total=70),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Telegram getUpdates : %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    await self._handle(u)
                except Exception as e:
                    log.exception("Erreur de traitement Telegram : %s", e)
            self.store.set("telegram_offset", offset)

    async def _handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            if cq["from"]["id"] != self.owner:
                return
            action, _, cid = cq.get("data", "").partition(":")
            if action not in ("pub", "rej"):
                await self._call("answerCallbackQuery", json={"callback_query_id": cq["id"]})
                return
            handler = self.on_approve if action == "pub" else self.on_reject
            reply = await handler(int(cid)) if handler else "?"
            await self._call("answerCallbackQuery", json={"callback_query_id": cq["id"],
                                                         "text": reply[:190]})
            await self._call("editMessageReplyMarkup", json={
                "chat_id": cq["message"]["chat"]["id"], "message_id": cq["message"]["message_id"],
                "reply_markup": {"inline_keyboard": [[{"text": reply[:60], "callback_data": "noop:0"}]]},
            })
            return

        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = msg.get("chat", {}).get("id")
        if not text.startswith("/") or not chat_id:
            return
        cmd, *args = text.split()
        cmd = cmd[1:].split("@")[0].lower()

        if cmd == "start":
            if self.owner == chat_id:
                await self.send("Déjà appairé ✅ — /status pour voir l'état.", chat_id)
            elif self.owner is not None:
                log.warning("Tentative d'appairage refusée (chat %s) : bot déjà appairé", chat_id)
                await self.send(f"⚠️ Quelqu'un a tenté d'appairer ton bot (id {chat_id}). Refusé.")
            elif self.pair_code and args and args[0] == self.pair_code:
                self.store.set("telegram_owner", chat_id)
                await self.send("Appairage réussi ✅\nTu recevras ici chaque clip à valider.\n"
                                f"Hébergeur sans disque (Render) : ajoute TELEGRAM_OWNER_ID={chat_id} "
                                "dans ses variables pour ne jamais refaire cette étape.\n"
                                "/status pour commencer.", chat_id)
            else:
                await self.send("Code d'appairage requis : /start <code>", chat_id)
            return

        if chat_id != self.owner:
            return  # le bot n'obéit qu'à toi
        handler = self.commands.get(cmd)
        await self.send(await handler(args) if handler else "Commande inconnue. /status", chat_id)
