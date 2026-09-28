"""
Télécommande Telegram : ton tableau de bord depuis le téléphone.

  /start <code>   appairer ton compte (code = TELEGRAM_PAIR_CODE)
  /status         lives suivis, clips du jour, modes actifs
  /auto on|off    publication sans validation
  /pause /resume  arrêter / reprendre la surveillance des lives
  /add <chaîne>   suivre une chaîne (ex : /add kamet0  ou  /add kick:xxx)
  /remove <chaîne>, /chaines
  /lives          choisir à la main les lives suivis (boutons) ; /algo pour revenir
  /tag @pseudo    tag incrusté sur les vidéos ; /outro on|off
  /tiktok         lien de connexion TikTok (une seule fois)
  /youtube        lien de connexion YouTube Shorts (une seule fois)

Chaque clip prêt arrive en vidéo, légende copiable d'un tap, avec ses boutons
(✅ Publier / ❌ Jeter / 📲 Déjà posté à la main).
API Bot HTTP brute (long polling) : aucune dépendance, aucun webhook à régler.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from .storage import Store

log = logging.getLogger(__name__)

Handler = Callable[[list[str]], Awaitable[str]]


def _keyboard(rows: list[list[tuple[str, str]]]) -> list[list[dict]]:
    return [[{"text": t[:60], "callback_data": d[:64]} for t, d in row] for row in rows if row]


def copyable(text: str, limit: int = 3500) -> str:
    """Texte en police à chasse fixe : sur Telegram mobile, un tap le copie en entier
    (légende + hashtags prêts à coller dans TikTok)."""
    return f"<code>{html.escape(text[:limit], quote=False)}</code>"


def clip_caption(header: str, caption: str) -> str:
    # Légende vidéo Telegram : 1024 caractères max (balises non comptées)
    head = header[:300]
    room = 1000 - len(head) - 30
    return (f"{html.escape(head, quote=False)}\n\n👇 légende (tap pour copier)\n"
            f"{copyable(caption, max(room, 100))}")


OWNER_MENU = [
    ("status", "État du bot"), ("auto", "on/off publication auto"),
    ("pause", "Mettre en pause"), ("resume", "Reprendre"),
    ("add", "Suivre une chaîne"), ("remove", "Ne plus suivre"),
    ("chaines", "Chaînes suivies"), ("tiktok", "Connecter TikTok"),
    ("youtube", "Connecter YouTube Shorts"),
    ("relance", "Renvoyer la file vers TikTok"),
    ("lives", "Choisir les lives suivis"), ("algo", "Lives choisis par l'algorithme"),
    ("tag", "Tag incrusté sur les vidéos"), ("outro", "on/off fin avec S'abonner"),
    ("whop", "Campagnes Whop suivies"), ("boutique", "Ventes aux clients"),
]


class TelegramBot:
    def __init__(self, token: str, pair_code: str, store: Store, session: aiohttp.ClientSession,
                 owner_id: str = "", public_only: bool = False, offset_key: str = "telegram_offset"):
        self.api = f"https://api.telegram.org/bot{token}"
        self.token = token
        self.enabled = bool(token)
        self.pair_code = pair_code
        self.owner_id = int(owner_id) if owner_id.strip().lstrip("-").isdigit() else None
        self.store = store
        self.session = session
        # Bot « boutique » dédié : aucune commande de pilotage, tout va aux clients
        self.public_only = public_only
        self.offset_key = offset_key
        # Messages / boutons / paiements de quelqu'un d'autre que toi -> boutique
        self.public: Callable[[dict], Awaitable[None]] | None = None
        self.on_owner_link: Callable[[str], Awaitable[str]] | None = None
        self.menu: list[tuple[str, str]] = OWNER_MENU
        self.public_menu: list[tuple[str, str]] = []
        self.username = ""
        self.commands: dict[str, Handler] = {}
        self.on_approve: Callable[[int], Awaitable[str]] | None = None
        self.on_reject: Callable[[int], Awaitable[str]] | None = None
        self.on_done: Callable[[int], Awaitable[str]] | None = None   # posté à la main
        # Autres boutons : {"préfixe": handler(argument) -> (texte, nouveau clavier | None)}
        self.callbacks: dict[str, Callable[[str], Awaitable[tuple[str, list | None]]]] = {}

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

    async def send(self, text: str, chat_id: int | None = None, html: bool = False) -> None:
        chat_id = chat_id or self.owner
        if not (self.enabled and chat_id):
            return
        body = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if html:
            body["parse_mode"] = "HTML"
        try:
            await self._call("sendMessage", json=body)
        except Exception as e:
            log.warning("Envoi Telegram impossible : %s", e)

    async def send_menu(self, text: str, rows: list[list[tuple[str, str]]]) -> None:
        """Message avec boutons : rows = [[(libellé, données), …], …]."""
        if not (self.enabled and self.owner):
            return
        try:
            await self._call("sendMessage", json={
                "chat_id": self.owner, "text": text[:4000], "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": _keyboard(rows)}})
        except Exception as e:
            log.warning("Envoi Telegram impossible : %s", e)

    async def reply(self, chat_id: int, text: str, rows: list | None = None,
                    html: bool = False) -> int | None:
        """Message à n'importe quelle conversation (clients, groupes), boutons facultatifs."""
        body = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if rows:
            body["reply_markup"] = {"inline_keyboard": _keyboard(rows)}
        if html:
            body["parse_mode"] = "HTML"
        try:
            return (await self._call("sendMessage", json=body))["message_id"]
        except Exception as e:
            log.warning("Envoi Telegram vers %s impossible : %s", chat_id, e)
            return None

    async def send_video(self, chat_id: int, path: Path, caption_html: str,
                         rows: list | None = None) -> int | None:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption_html)
        form.add_field("parse_mode", "HTML")
        form.add_field("supports_streaming", "true")
        if rows:
            form.add_field("reply_markup", json.dumps({"inline_keyboard": _keyboard(rows)}))
        try:
            with path.open("rb") as f:
                form.add_field("video", f, filename=path.name, content_type="video/mp4")
                msg = await self._call("sendVideo", data=form,
                                       timeout=aiohttp.ClientTimeout(total=300))
            return msg["message_id"]
        except Exception as e:
            log.error("Envoi de la vidéo %s vers %s impossible : %s", path.name, chat_id, e)
            return None

    async def send_clip(self, clip_id: int, path: Path, caption: str, header: str,
                        buttons: list[list[tuple[str, str]]] | None = None) -> int | None:
        if not (self.enabled and self.owner):
            return None
        if buttons is None:
            buttons = [[("✅ Publier", f"pub:{clip_id}"), ("❌ Jeter", f"rej:{clip_id}")],
                       [("📲 Déjà posté à la main", f"done:{clip_id}")]]
        keyboard = {"inline_keyboard": _keyboard(buttons)}
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self.owner))
        form.add_field("caption", clip_caption(header, caption))
        form.add_field("parse_mode", "HTML")
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
        try:
            self.username = (await self._call("getMe"))["username"]
        except Exception as e:
            log.warning("Telegram getMe : %s", e)
        cmds = lambda menu: [{"command": c, "description": d} for c, d in menu]
        try:   # un souci réseau au démarrage ne doit jamais couper la télécommande
            if self.public_menu and self.owner and not self.public_only:
                # Même bot pour toi et les clients : chacun voit son propre menu
                await self._call("setMyCommands", json={"commands": cmds(self.public_menu)})
                await self._call("setMyCommands", json={
                    "commands": cmds(self.menu), "scope": {"type": "chat", "chat_id": self.owner}})
            else:
                await self._call("setMyCommands", json={"commands": cmds(self.menu)})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Telegram setMyCommands : %s", e)
        offset = self.store.get(self.offset_key, 0)
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
            self.store.set(self.offset_key, offset)

    async def _public(self, u: dict) -> None:
        if self.public:
            await self.public(u)

    async def _handle(self, u: dict) -> None:
        if self.public_only or "pre_checkout_query" in u:
            return await self._public(u)
        if "callback_query" in u:
            cq = u["callback_query"]
            if cq["from"]["id"] != self.owner or cq.get("data", "").startswith("shop"):
                return await self._public(u)
            action, _, cid = cq.get("data", "").partition(":")
            if action in self.callbacks:
                text, rows = await self.callbacks[action](cid)
                await self._call("answerCallbackQuery", json={"callback_query_id": cq["id"],
                                                             "text": text[:190]})
                try:
                    await self._call("editMessageText", json={
                        "chat_id": cq["message"]["chat"]["id"],
                        "message_id": cq["message"]["message_id"], "text": text[:4000],
                        "reply_markup": {"inline_keyboard": _keyboard(rows or [])}})
                except Exception:
                    pass   # message identique : Telegram refuse la modification, sans gravité
                return
            handlers = {"pub": self.on_approve, "rej": self.on_reject, "done": self.on_done}
            if action not in handlers or not cid.isdigit():
                await self._call("answerCallbackQuery", json={"callback_query_id": cq["id"]})
                return
            # Base effacée à chaque redémarrage (Render gratuit) : les numéros de clips
            # repartent de 1. Un vieux bouton ne doit jamais agir sur un autre clip.
            row = self.store.clip(int(cid))
            if not row or row["tg_message_id"] != cq["message"]["message_id"]:
                await self._call("answerCallbackQuery", json={
                    "callback_query_id": cq["id"], "show_alert": True,
                    "text": "⌛ Bouton expiré (le bot a redémarré depuis). La vidéo reste "
                            "utilisable : partage-la vers TikTok à la main."})
                return
            handler = handlers[action]
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
        if not chat_id:
            return
        if msg.get("successful_payment"):
            return await self._public(u)
        if chat_id != self.owner and self.owner is not None:
            parts = text.split()
            if parts and parts[0].split("@")[0].lower() == "/start" and len(parts) > 1 \
                    and self.pair_code and parts[1] == self.pair_code:
                log.warning("Tentative d'appairage refusée (chat %s) : bot déjà appairé", chat_id)
                await self.send(f"⚠️ Quelqu'un a tenté d'appairer ton bot (id {chat_id}). Refusé.")
                return
            return await self._public(u)   # clients (boutique) ; ignoré si elle est fermée
        if not text.startswith("/"):
            # Lien de clip envoyé par toi : monté pour ton compte
            if chat_id == self.owner and self.on_owner_link and ("twitch.tv" in text or "kick.com" in text):
                reply = await self.on_owner_link(text)
                if reply:
                    await self.send(reply, chat_id)
            return
        cmd, *args = text.split()
        cmd = cmd[1:].split("@")[0].lower()

        if cmd == "start":
            if self.owner == chat_id:
                await self.send("Déjà appairé ✅ — /status pour voir l'état.", chat_id)
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
        reply = await handler(args) if handler else "Commande inconnue. /status"
        if reply:
            await self.send(reply, chat_id)
