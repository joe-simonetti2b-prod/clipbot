"""
Boutique : le bot comme outil payant pour d'autres clippeurs.

Un client envoie au bot un lien de clip Twitch ou Kick (en privé, ou « /clip <lien> »
dans un groupe où le bot est ajouté) et reçoit la vidéo montée : 9:16, sous-titres
calés, accroche, miniature, son normalisé, + la légende et les hashtags prêts à
copier. Son propre @pseudo peut être incrusté (/tag), la langue des sous-titres
choisie (/langue).

Paiement :
  * dans Telegram : Étoiles (⭐, obligatoire pour un service numérique vendu dans un
    bot — règle Telegram). Rien à configurer : ça marche avec le bot tel quel.
    Les étoiles se retirent en TON via Fragment (~0,013 $ l'étoile, 21 jours de délai).
  * hors Telegram : page web /offre, paiement crypto via Crypto Pay (@CryptoBot) ;
    après paiement, un lien t.me/<bot>?start=r_<code> crédite le compte (vérifié
    auprès de Crypto Pay, utilisable une seule fois).

Crédits : 1 crédit = 1 clip LIVRÉ (débité seulement à la livraison ; un échec ne coûte
rien). Notre propre compte TikTok reste prioritaire dans la file de montage.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import aiohttp

from .config import Settings
from .harvest import broadcaster, clip_url, download_clip
from .resources import Resources
from .storage import Store
from .telegram import TelegramBot, copyable
from .watcher import find_clip_links

log = logging.getLogger(__name__)

CRYPTOPAY_API = "https://pay.crypt.bot/api"
LANGS = {"fr": "français", "en": "anglais", "es": "espagnol", "de": "allemand",
         "it": "italien", "pt": "portugais", "original": "langue d'origine (pas de traduction)"}
MAX_CLIP_S = 90
AVG_MIN_PER_CLIP = 8        # estimation affichée au client (machine gratuite, lente)
CUSTOMER_MENU = [
    ("start", "Présentation et tarifs"), ("acheter", "Acheter des clips"),
    ("solde", "Mes crédits"), ("clip", "Monter un clip : /clip <lien>"),
    ("tag", "Mon @pseudo sur les vidéos"), ("langue", "Langue des sous-titres"),
    ("conditions", "Conditions"), ("paysupport", "Aide paiement"),
]


@dataclass
class Pack:
    key: str
    credits: int
    stars: int
    eur: float


def parse_packs(spec: str) -> list[Pack]:
    """Clé stable dérivée du contenu (« 10c450s ») : modifier ou réordonner SHOP_PACKS
    ne peut jamais faire créditer un autre pack que celui payé."""
    out = []
    for part in (x for x in spec.split(",") if x.strip()):
        try:
            c, s, e = part.strip().split(":")
            if int(c) > 0 and int(s) > 0:
                out.append(Pack(f"{int(c)}c{int(s)}s", int(c), int(s), float(e)))
        except ValueError:
            log.warning("Pack ignoré (format crédits:étoiles:euros) : %r", part)
    return out or [Pack("3c150s", 3, 150, 2.0)]


def pack_credits(key: str) -> int:
    try:
        return int(key.split("c", 1)[0])
    except ValueError:
        return 0


class Shop:
    def __init__(self, settings: Settings, store: Store, bot: TelegramBot, owner_bot: TelegramBot,
                 session: aiohttp.ClientSession, res: Resources | None = None,
                 wake: Callable[[], None] = lambda: None):
        self.s = settings
        self.cfg = settings.shop
        self.store = store
        self.bot = bot                  # parle aux clients (bot dédié ou le même)
        self.owner_bot = owner_bot      # te prévient (ventes, support)
        self.session = session
        self.res = res
        self.wake = wake
        self.packs = parse_packs(self.cfg.packs)
        self.dir = settings.capture.work_dir / "orders"
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ état
    @property
    def enabled(self) -> bool:
        return self.store.get("shop_enabled", self.cfg.enabled)

    def pack(self, key: str) -> Pack | None:
        return next((p for p in self.packs if p.key == key), None)

    def customers(self) -> dict:
        return self.store.get("shop_customers", {})

    def customer(self, uid: int, name: str = "") -> dict:
        all_ = self.customers()
        c = all_.get(str(uid))
        if c is None:
            c = {"credits": self.cfg.free_trial, "tag": "", "lang": "", "name": name,
                 "clips": 0, "stars": 0, "eur": 0.0, "since": time.time(), "trial": self.cfg.free_trial}
            all_[str(uid)] = c
            self.store.set("shop_customers", all_)
        elif name and c.get("name") != name:
            c["name"] = name
            all_[str(uid)] = c
            self.store.set("shop_customers", all_)
        return c

    def _update(self, uid: int, **changes) -> dict:
        all_ = self.customers()
        c = all_.get(str(uid)) or self.customer(uid)
        for k, v in changes.items():
            c[k] = v
        all_[str(uid)] = c
        self.store.set("shop_customers", all_)
        return c

    def jobs(self) -> list[dict]:
        return self.store.get("shop_jobs", [])

    def _set_jobs(self, jobs: list[dict]) -> None:
        self.store.set("shop_jobs", jobs)

    def pending(self, uid: int | None = None) -> int:
        return sum(1 for j in self.jobs() if uid is None or j["uid"] == uid)

    def available(self, uid: int) -> int:
        """Crédits utilisables = solde − commandes déjà en cours (débitées à la livraison)."""
        return self.customer(uid).get("credits", 0) - self.pending(uid)

    # ------------------------------------------------------------ Telegram
    async def handle(self, u: dict) -> None:
        try:
            if "pre_checkout_query" in u:
                return await self._pre_checkout(u["pre_checkout_query"])
            if "callback_query" in u:
                return await self._button(u["callback_query"])
            msg = u.get("message") or {}
            if msg.get("successful_payment"):
                return await self._paid(msg)     # toujours traité : de l'argent a été reçu
            await self._message(msg)
        except Exception:
            log.exception("Boutique : mise à jour non traitée")

    async def _message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        text = (msg.get("text") or "").strip()
        if not (chat.get("id") and sender.get("id")) or sender.get("is_bot"):
            return
        private = chat.get("type") == "private"
        uid, cid = sender["id"], chat["id"]
        name = sender.get("username") or sender.get("first_name") or str(uid)
        if not self.enabled:
            if private:
                await self.bot.reply(cid, "Ce bot est privé pour le moment.")
            return
        cmd, _, rest = text.partition(" ")
        cmd = cmd[1:].split("@")[0].lower() if cmd.startswith("/") else ""
        if cmd == "" and private:
            links = [l for l in find_clip_links(text)] or \
                    [w for w in text.split() if w.startswith("http")]
            if links:
                return await self.order(uid, cid, links[0], name)
            return await self.bot.reply(cid, "Envoie-moi un lien de clip Twitch ou Kick "
                                             "(ex : https://clips.twitch.tv/…) ou /aide.")
        if not cmd:
            return   # groupe : seules les commandes comptent
        args = rest.split()
        if cmd == "start":
            if args and args[0].startswith("r_"):
                return await self.redeem(uid, cid, args[0][2:], name)
            return await self.welcome(uid, cid, name)
        if cmd in ("aide", "help"):
            return await self.welcome(uid, cid, name)
        if cmd in ("acheter", "buy"):
            return await self.bot.reply(cid, self.prices_text(), self.buy_rows())
        if cmd in ("solde", "balance"):
            c = self.customer(uid, name)
            return await self.bot.reply(cid, (
                f"💳 {self.available(uid)} clip(s) disponible(s)"
                + (f" ({self.pending(uid)} en cours de montage)" if self.pending(uid) else "")
                + f"\nPseudo incrusté : {c.get('tag') or 'aucun'} · sous-titres : "
                f"{LANGS.get(c.get('lang') or 'fr', 'français')}"), self.buy_rows())
        if cmd == "clip":
            if not args:
                return await self.bot.reply(cid, "Usage : /clip <lien du clip Twitch ou Kick>")
            return await self.order(uid, cid, args[0], name)
        if cmd == "tag":
            if not args:
                return await self.bot.reply(cid, "Usage : /tag @toncompte (ou /tag off)")
            tag = "" if args[0].lower() in ("off", "aucun", "non") else args[0][:30]
            if tag and not tag.startswith("@"):
                tag = "@" + tag
            self.customer(uid, name)
            self._update(uid, tag=tag)
            return await self.bot.reply(cid, f"✅ Pseudo incrusté : {tag or 'aucun'}")
        if cmd == "langue":
            if not args or args[0].lower() not in LANGS:
                return await self.bot.reply(cid, "Usage : /langue " + " | ".join(LANGS))
            self.customer(uid, name)
            self._update(uid, lang=args[0].lower())
            return await self.bot.reply(cid, f"✅ Sous-titres : {LANGS[args[0].lower()]}")
        if cmd in ("conditions", "terms"):
            return await self.bot.reply(cid, self.terms_text())
        if cmd in ("paysupport", "support"):
            return await self.support(uid, cid, name, rest)

    async def welcome(self, uid: int, cid: int, name: str) -> None:
        c = self.customer(uid, name)
        gift = (f"🎁 {c['credits']} clip(s) offert(s) pour essayer.\n\n"
                if c.get("clips", 0) == 0 and c.get("credits", 0) > 0 else "")
        await self.bot.reply(cid, (
            "🎬 Je transforme un clip Twitch ou Kick en vidéo TikTok / Reels / Shorts prête "
            "à poster : format vertical, sous-titres synchronisés, accroche, son propre, "
            "légende + hashtags à copier.\n\n"
            f"{gift}"
            "➡️ Envoie-moi simplement le lien du clip (dans un groupe : /clip <lien>).\n"
            "Options : /tag @toncompte pour incruster ton pseudo · /langue pour les sous-titres.\n\n"
            + self.prices_text()), self.buy_rows())

    def prices_text(self) -> str:
        lines = [f"  • {p.credits} clips — {p.stars} ⭐" for p in self.packs]
        return "Tarifs (1 crédit = 1 clip livré, débité seulement à la livraison) :\n" + "\n".join(lines)

    def buy_rows(self) -> list:
        return [[(f"⭐ {p.credits} clips — {p.stars}", f"shop:buy:{p.key}")] for p in self.packs]

    def terms_text(self) -> str:
        return (
            "Conditions\n"
            "• Service : montage automatique de clips Twitch/Kick que tu fournis.\n"
            "• Tu dois avoir le droit de republier ce contenu (ta chaîne, une campagne de "
            "clipping officielle comme Whop, ou l'accord du streamer). Tu es responsable de "
            "ce que tu publies.\n"
            "• 1 crédit = 1 clip livré. Débité uniquement à la livraison ; sans expiration.\n"
            "• Délais indicatifs (file partagée). Clips de 90 s maximum.\n"
            "• Problème de paiement ou remboursement : /paysupport <ton message>.")

    async def support(self, uid: int, cid: int, name: str, text: str) -> None:
        if not text.strip():
            return await self.bot.reply(cid, "Décris ton problème après la commande, ex :\n"
                                             "/paysupport j'ai payé mais pas reçu mes crédits")
        pays = [p for p in self.store.get("shop_payments", []) if p.get("uid") == uid][-3:]
        await self.owner_bot.send(
            f"🆘 Support boutique — {name} (id {uid}) :\n{text.strip()[:1500]}\n"
            f"Solde : {self.customer(uid).get('credits', 0)} · paiements récents : "
            + (", ".join(f"{p['amount']}{'⭐' if p['kind'] == 'stars' else '€'} "
                         f"[{p.get('charge', '')}]" for p in pays) or "aucun")
            + f"\nRépondre : /repondre {uid} <message> · Rembourser : /rembourser {uid} <id paiement>")
        await self.bot.reply(cid, "📨 Message transmis, réponse ici sous 24 h.")

    # ------------------------------------------------------------ commandes
    async def order(self, uid: int, cid: int, url: str, name: str) -> None:
        url = clip_url(url.strip().strip("<>"))
        if not url:
            return await self.bot.reply(cid, "❌ Lien non pris en charge : envoie le lien d'un "
                                             "CLIP Twitch ou Kick (clips.twitch.tv/…, "
                                             "twitch.tv/…/clip/…, kick.com/…?clip=…), pas d'une "
                                             "chaîne ou d'un live.")
        self.customer(uid, name)
        if self.available(uid) < 1:
            return await self.bot.reply(cid, "Plus de crédit. " + self.prices_text(), self.buy_rows())
        if self.pending(uid) >= self.cfg.per_user:
            return await self.bot.reply(cid, f"⏳ Tu as déjà {self.pending(uid)} clips en cours, "
                                             "attends qu'ils arrivent.")
        if self.pending() >= self.cfg.max_queue:
            return await self.bot.reply(cid, "🚦 File pleine pour le moment, réessaie dans 30 min.")
        c = self.customer(uid)
        job = {"id": secrets.token_hex(6), "uid": uid, "chat": cid, "url": url, "at": time.time(),
               "tag": c.get("tag", ""), "lang": c.get("lang", ""), "name": name}
        self._set_jobs(self.jobs() + [job])
        pos = self.pending()
        await self.bot.reply(cid, f"⏳ Commande reçue (position {pos}). Livraison estimée : "
                                  f"~{pos * AVG_MIN_PER_CLIP} min. Je t'envoie la vidéo ici.")
        self._spawn(self._fetch(job))

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _fetch(self, job: dict) -> None:
        try:
            path, info, duration = await download_clip(job["url"], self.dir, self.res)
        except Exception as e:
            log.info("Commande %s : téléchargement impossible (%s)", job["id"], e)
            self._drop_job(job["id"])
            await self.bot.reply(job["chat"], "❌ Impossible de récupérer ce clip (lien privé, "
                                              "supprimé ou invalide ?). Aucun crédit utilisé.")
            return
        from .models import Platform
        plat = Platform.KICK.value if "kick.com" in job["url"] else Platform.TWITCH.value
        self.store.add_clip(
            platform=plat, channel=broadcaster(info) or "clip", stream_url=job["url"],
            stream_title=str(info.get("title") or "")[:200], category="", reason="commande",
            score=0.0, tokens="commande client", raw_path=str(path), trim_offset=0.0,
            trim_duration=min(duration, MAX_CLIP_S), customer_id=job["uid"],
            customer_chat=job["chat"], customer_tag=job.get("tag", ""),
            customer_lang=job.get("lang", ""), customer_job=job["id"],
            created=job["at"])
        self.wake()

    def _drop_job(self, job_id: str) -> None:
        self._set_jobs([j for j in self.jobs() if j["id"] != job_id])

    async def resume(self) -> None:
        """Après un redémarrage (disque effacé), les commandes en cours sont relancées."""
        jobs = self.jobs()
        if jobs:
            log.info("Boutique : %d commande(s) relancée(s) après redémarrage", len(jobs))
        for job in jobs:
            known = self.store.db.execute("SELECT 1 FROM clips WHERE customer_job=?",
                                          (job["id"],)).fetchone()
            if not known:           # disque conservé (autre hébergeur) : déjà en file
                self._spawn(self._fetch(job))

    async def deliver(self, row: dict, path: Path, copy) -> None:
        uid, chat = row["customer_id"], row["customer_chat"]
        left = max(0, self.customer(uid).get("credits", 0) - 1)     # estimation pour le message
        head = (f"✅ Ton clip est prêt ! 1 crédit utilisé, il t'en reste {left}."
                + (f"\nAccroche : {copy.hook}" if getattr(copy, "hook", "") else ""))
        caption = (html.escape(head, quote=False) + "\n\n👇 légende + hashtags (tap pour copier)\n"
                   + copyable(row.get("caption") or "", 700))
        rows = self.buy_rows() if left <= 0 else None
        mid = await self.bot.send_video(chat, path, caption, rows)
        self._drop_job(row.get("customer_job") or "")
        if not mid:
            self.store.update_clip(row["id"], status="failed", error="envoi Telegram impossible")
            await self.bot.reply(chat, "❌ La vidéo n'a pas pu être envoyée. Aucun crédit utilisé, "
                                       "renvoie le lien.")
            return
        # Solde relu APRÈS l'envoi (qui peut durer) : un achat fait pendant ce temps est gardé
        c = self.customer(uid)
        self._update(uid, credits=max(0, c.get("credits", 0) - 1), clips=c.get("clips", 0) + 1)
        self.store.update_clip(row["id"], status="delivered", final_path=None)
        path.unlink(missing_ok=True)
        log.info("Boutique : clip %s livré à %s (reste %d)", row["id"], uid, left)

    async def failed(self, row: dict, err: str) -> None:
        self._drop_job(row.get("customer_job") or "")
        if row.get("raw_path"):
            Path(row["raw_path"]).unlink(missing_ok=True)
        await self.bot.reply(row["customer_chat"], "❌ Le montage de ton clip a échoué. "
                                                   "Aucun crédit utilisé, tu peux renvoyer le lien.")

    # ------------------------------------------------------------ Étoiles
    async def _button(self, cq: dict) -> None:
        parts = (cq.get("data") or "").split(":")
        await self.bot._call("answerCallbackQuery", json={"callback_query_id": cq["id"]})
        if len(parts) == 3 and parts[1] == "buy" and self.enabled:
            chat = (cq.get("message") or {}).get("chat", {}).get("id") or cq["from"]["id"]
            await self.invoice(chat, parts[2])

    async def invoice(self, chat_id: int, key: str) -> None:
        p = self.pack(key)
        if not p:
            return
        await self.bot._call("sendInvoice", json={
            "chat_id": chat_id, "title": f"{p.credits} clips montés",
            "description": f"{p.credits} clips Twitch/Kick montés en vidéo verticale sous-titrée, "
                           "prêts à poster. Crédits sans expiration.",
            "payload": f"pack:{p.key}", "currency": "XTR",
            "prices": [{"label": f"{p.credits} clips", "amount": p.stars}]})

    async def _pre_checkout(self, q: dict) -> None:
        key = (q.get("invoice_payload") or "").partition(":")[2]
        p = self.pack(key)
        ok = bool(self.enabled and p and q.get("currency") == "XTR"
                  and q.get("total_amount") == p.stars)
        body = {"pre_checkout_query_id": q["id"], "ok": ok}
        if not ok:
            body["error_message"] = "Offre expirée : relance /acheter."
        await self.bot._call("answerPreCheckoutQuery", json=body)

    async def _paid(self, msg: dict) -> None:
        sp = msg["successful_payment"]
        uid = msg["from"]["id"]
        charge = sp.get("telegram_payment_charge_id", "")
        pays = self.store.get("shop_payments", [])
        if any(x.get("charge") == charge for x in pays):
            return                                  # déjà crédité
        # Crédits lus dans la clé du pack payé (fixée par nous dans la facture, validée au
        # pre_checkout) : même si SHOP_PACKS change entre-temps, le client reçoit son dû.
        credits = pack_credits((sp.get("invoice_payload") or "").partition(":")[2]) \
            or max(1, sp.get("total_amount", 0) // 50)
        name = msg["from"].get("username") or msg["from"].get("first_name") or str(uid)
        c = self.customer(uid, name)
        self._update(uid, credits=c.get("credits", 0) + credits,
                     stars=c.get("stars", 0) + sp.get("total_amount", 0))
        pays.append({"uid": uid, "kind": "stars", "amount": sp.get("total_amount", 0),
                     "credits": credits, "charge": charge, "at": time.time()})
        self.store.set("shop_payments", pays[-500:])
        await self.bot.reply(msg["chat"]["id"], f"🎉 Merci ! +{credits} clips. Solde : "
                                                f"{self.available(uid)}. Envoie-moi un lien de clip.")
        await self.owner_bot.send(f"💰 Vente : +{sp.get('total_amount')} ⭐ de {name} "
                                  f"(id {uid}, {credits} clips).")

    async def refund(self, uid: int, charge: str) -> str:
        pays = self.store.get("shop_payments", [])
        pay = next((x for x in pays if x.get("charge") == charge and x.get("uid") == uid), None)
        if not pay:
            return "Paiement introuvable (vérifie l'id client et l'id du paiement)."
        if pay.get("refunded"):
            return "Déjà remboursé."
        if pay["kind"] != "stars":
            return "Paiement crypto : rembourse-le depuis @CryptoBot, puis /offrir pour ajuster."
        await self.bot._call("refundStarPayment", json={"user_id": uid,
                                                        "telegram_payment_charge_id": charge})
        pays = self.store.get("shop_payments", [])          # relu après l'appel réseau
        for x in pays:
            if x.get("charge") == charge:
                x["refunded"] = True
        self.store.set("shop_payments", pays)
        c = self.customer(uid)
        self._update(uid, credits=max(0, c.get("credits", 0) - pay["credits"]))
        await self.bot.reply(uid, f"↩️ Remboursement de {pay['amount']} ⭐ effectué.")
        return f"↩️ {pay['amount']} ⭐ remboursées à {uid}."

    # ------------------------------------------------------------ crypto (page web)
    @property
    def crypto_enabled(self) -> bool:
        return bool(self.cfg.cryptopay_token)

    async def _cryptopay(self, method: str, **params) -> dict:
        async with self.session.post(f"{CRYPTOPAY_API}/{method}", json=params,
                                     headers={"Crypto-Pay-API-Token": self.cfg.cryptopay_token},
                                     timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(f"Crypto Pay {method} : {data.get('error')}")
        return data["result"]

    async def crypto_invoice(self, key: str) -> str:
        """Crée une facture crypto pour un pack ; renvoie le lien de paiement."""
        p = self.pack(key)
        if not (p and self.crypto_enabled and self.bot.username):
            raise RuntimeError("paiement crypto indisponible")
        code = secrets.token_urlsafe(9)
        inv = await self._cryptopay(
            "createInvoice", currency_type="fiat", fiat="EUR", amount=f"{p.eur:.2f}",
            accepted_assets=self.cfg.cryptopay_assets,
            description=f"{p.credits} clips montés (clipbot)", payload=code, expires_in=3600,
            paid_btn_name="openBot", paid_btn_url=f"https://t.me/{self.bot.username}?start=r_{code}",
            hidden_message=f"Ouvre le bot avec ce lien pour recevoir tes crédits : "
                           f"https://t.me/{self.bot.username}?start=r_{code}")
        await self._prune_codes()
        codes = self.store.get("shop_codes", {})            # relu après les appels réseau
        codes[code] = {"invoice": inv["invoice_id"], "credits": p.credits, "eur": p.eur,
                       "at": time.time()}
        self.store.set("shop_codes", codes)
        return inv.get("web_app_invoice_url") or inv.get("bot_invoice_url")

    async def _prune_codes(self, keep: int = 150) -> None:
        """Codes non utilisés de plus de 2 h (facture expirée au bout d'1 h) : on ne jette
        que ceux que Crypto Pay confirme NON payés. Un code payé n'est jamais perdu."""
        codes = self.store.get("shop_codes", {})
        old = {k: v for k, v in codes.items()
               if not v.get("used") and time.time() - v["at"] > 7200}
        if len(codes) < keep or not old:
            return
        try:
            res = await self._cryptopay("getInvoices",
                                        invoice_ids=",".join(str(v["invoice"]) for v in old.values()),
                                        count=1000)
            paid = {i["invoice_id"] for i in res.get("items") or [] if i.get("status") == "paid"}
        except Exception as e:
            log.warning("Tri des codes crypto impossible : %s", e)
            return
        codes = self.store.get("shop_codes", {})
        for k, v in old.items():
            if v["invoice"] not in paid:
                codes.pop(k, None)
        # Codes utilisés : l'historique des paiements suffit au-delà de 60 jours
        codes = {k: v for k, v in codes.items()
                 if not (v.get("used") and time.time() - v["at"] > 60 * 86400)}
        self.store.set("shop_codes", codes)

    async def redeem(self, uid: int, cid: int, code: str, name: str) -> None:
        entry = self.store.get("shop_codes", {}).get(code)
        if not entry:
            return await self.bot.reply(cid, "Code inconnu ou expiré.")
        if entry.get("used"):
            return await self.bot.reply(cid, "Ce code a déjà été utilisé.")
        try:
            res = await self._cryptopay("getInvoices", invoice_ids=str(entry["invoice"]))
            inv = (res.get("items") or [{}])[0]
        except Exception as e:
            log.warning("Crypto Pay injoignable : %s", e)
            return await self.bot.reply(cid, "Vérification du paiement impossible pour l'instant, "
                                             "réessaie dans quelques minutes avec le même lien.")
        if inv.get("status") != "paid":
            return await self.bot.reply(cid, "Paiement pas encore reçu. Réessaie ce lien une fois "
                                             "le paiement confirmé.")
        codes = self.store.get("shop_codes", {})            # relu après l'appel réseau
        entry = codes.get(code)
        if not entry or entry.get("used"):
            return await self.bot.reply(cid, "Ce code a déjà été utilisé.")
        credits, eur = int(entry["credits"]), float(entry["eur"])
        c = self.customer(uid, name)
        self._update(uid, credits=c.get("credits", 0) + credits, eur=c.get("eur", 0) + eur)
        entry.update(used=True, uid=uid)
        codes[code] = entry
        self.store.set("shop_codes", codes)
        pays = self.store.get("shop_payments", [])
        pays.append({"uid": uid, "kind": "crypto", "amount": eur, "credits": credits,
                     "charge": f"cp{entry['invoice']}", "at": time.time()})
        self.store.set("shop_payments", pays[-500:])
        await self.bot.reply(cid, f"🎉 Paiement reçu : +{credits} clips. Envoie-moi un lien de clip !")
        await self.owner_bot.send(f"💰 Vente crypto : {eur:.2f} € de {name} (id {uid}).")

    # ------------------------------------------------------------ propriétaire
    def summary(self) -> str:
        cust = self.customers()
        pays = self.store.get("shop_payments", [])
        now = time.time()
        stars = sum(p["amount"] for p in pays if p["kind"] == "stars" and not p.get("refunded"))
        eur = sum(p["amount"] for p in pays if p["kind"] == "crypto")
        week = [p for p in pays if now - p["at"] < 7 * 86400 and not p.get("refunded")]
        buyers = sum(1 for c in cust.values() if c.get("stars") or c.get("eur"))
        link = f"https://t.me/{self.bot.username}" if self.bot.username else "(bot en démarrage)"
        return (f"🛒 Boutique : {'OUVERTE' if self.enabled else 'fermée'} — {link}\n"
                f"{len(cust)} clients · {buyers} acheteurs · "
                f"{sum(c.get('clips', 0) for c in cust.values())} clips livrés\n"
                f"Encaissé : {stars} ⭐ (≈ {stars * 0.013:.2f} $ au retrait) + {eur:.2f} € crypto · "
                f"7 derniers jours : {len(week)} ventes\n"
                f"En cours : {self.pending()} commande(s) · crypto : "
                f"{'active' if self.crypto_enabled else 'non configurée (CRYPTOPAY_TOKEN)'}")
