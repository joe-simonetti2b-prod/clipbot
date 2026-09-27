"""
Serveur HTTP minimal (aiohttp) exposé par l'hébergeur :

  /health            healthcheck + keep-alive
  /                  page d'état publique (aucune donnée sensible)
  /privacy, /terms   pages exigées par TikTok pour valider l'app développeur
  /tiktok/login      démarre la connexion TikTok (protégé par le code d'appairage)
  /tiktok/callback   retour OAuth TikTok
  /youtube/login     connexion YouTube (protégée par le code d'appairage)
  /youtube/callback  retour OAuth Google
  /kick/webhook      réception du chat Kick (API officielle)
"""
from __future__ import annotations

import html
import logging
import secrets
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;
color:#111;background:#fff}}@media(prefers-color-scheme:dark){{body{{color:#eee;background:#111}}}}
h1{{font-size:1.4rem}}</style></head><body><h1>{title}</h1>{body}</body></html>"""

PRIVACY = """<p>Ce service est un outil personnel de montage et de publication de courts
extraits vidéo, utilisé par son propriétaire pour son propre compte TikTok.</p>
<p><b>Données TikTok et YouTube utilisées :</b> identifiant de compte et jetons d'accès
OAuth, uniquement pour publier les vidéos que le propriétaire a choisi de publier.
Aucune donnée d'autres utilisateurs n'est collectée, vendue ou partagée.</p>
<p><b>Conservation :</b> les jetons sont stockés sur le serveur privé du service et
supprimés sur simple demande ; la connexion peut être révoquée à tout moment depuis
les paramètres TikTok.</p><p>Contact : {email}</p>"""

TERMS = """<p>Service privé, non commercialisé, réservé à son propriétaire. Les contenus
publiés respectent les conditions d'utilisation de TikTok et les droits des créateurs
concernés (crédit systématique de la chaîne d'origine). Contact : {email}</p>"""


def build_app(a: "App") -> web.Application:
    email = html.escape(a.s.server.contact_email or "voir le profil TikTok du propriétaire")

    async def health(_):
        return web.Response(text="ok")

    async def index(_):
        body = (
            "<p>Personal tool that turns the best moments of livestreams into vertical short "
            "videos (9:16, subtitles) and uploads them, after my manual approval, to my own "
            "TikTok and YouTube accounts. The original streamer is always credited.</p>"
            "<p>Outil personnel : il repère les meilleurs moments de lives, les monte en vidéos "
            "verticales sous-titrées et les envoie, après validation, sur mes propres comptes.</p>"
            f"<p>État : {'⏸️ en pause' if a.orch.paused else '🟢 actif'} · "
            f"lives suivis : {len(a.orch.watchers)}</p>"
            '<p><a href="/terms">Terms of Service</a> · <a href="/privacy">Privacy Policy</a></p>')
        return web.Response(text=PAGE.format(title="clipbot", body=body), content_type="text/html")

    async def privacy(_):
        return web.Response(text=PAGE.format(title="Politique de confidentialité",
                                             body=PRIVACY.format(email=email)),
                            content_type="text/html")

    async def terms(_):
        return web.Response(text=PAGE.format(title="Conditions d'utilisation",
                                             body=TERMS.format(email=email)),
                            content_type="text/html")

    async def tiktok_login(req: web.Request):
        code = a.s.publish.telegram_pair_code
        if not code or not secrets.compare_digest(req.query.get("k", ""), code):
            raise web.HTTPForbidden(text="lien invalide — utilise /tiktok dans Telegram")
        if not a.tiktok.configured:
            raise web.HTTPServiceUnavailable(text="TIKTOK_CLIENT_KEY / SECRET manquants")
        raise web.HTTPFound(a.tiktok.login_url())

    async def tiktok_callback(req: web.Request):
        try:
            if "error" in req.query:
                raise RuntimeError(req.query.get("error_description") or req.query["error"])
            await a.tiktok.handle_callback(req.query.get("code", ""), req.query.get("state", ""))
        except Exception as e:
            log.error("Connexion TikTok échouée : %s", e)
            await a.tg.send(f"⚠️ Connexion TikTok échouée : {e}")
            return web.Response(text=PAGE.format(title="Échec", body=html.escape(str(e))),
                                content_type="text/html", status=400)
        await a.tg.send("✅ TikTok connecté. Les clips validés partiront automatiquement.")
        return web.Response(text=PAGE.format(title="TikTok connecté ✅",
                                             body="<p>Tu peux fermer cette page.</p>"),
                            content_type="text/html")

    async def youtube_login(req: web.Request):
        code = a.s.publish.telegram_pair_code
        if not code or not secrets.compare_digest(req.query.get("k", ""), code):
            raise web.HTTPForbidden(text="lien invalide — utilise /youtube dans Telegram")
        if not a.youtube.configured:
            raise web.HTTPServiceUnavailable(text="YOUTUBE_CLIENT_ID / SECRET manquants")
        raise web.HTTPFound(a.youtube.login_url())

    async def youtube_callback(req: web.Request):
        try:
            if "error" in req.query:
                raise RuntimeError(req.query["error"])
            await a.youtube.handle_callback(req.query.get("code", ""), req.query.get("state", ""))
        except Exception as e:
            log.error("Connexion YouTube échouée : %s", e)
            await a.tg.send(f"⚠️ Connexion YouTube échouée : {e}")
            return web.Response(text=PAGE.format(title="Échec", body=html.escape(str(e))),
                                content_type="text/html", status=400)
        await a.tg.send("✅ YouTube connecté. Les clips validés partiront aussi en Shorts.")
        return web.Response(text=PAGE.format(title="YouTube connecté ✅",
                                             body="<p>Tu peux fermer cette page.</p>"),
                            content_type="text/html")

    async def kick_webhook(req: web.Request):
        if req.headers.get("Kick-Event-Type") != "chat.message.sent":
            return web.Response(text="ignored")
        try:
            data = await req.json()
            bid = str(data["broadcaster"]["user_id"])
            author = data.get("sender", {}).get("username", "")
            a.orch.kick_message(bid, author, data.get("content", ""))
        except Exception as e:
            log.debug("Webhook Kick illisible : %s", e)
        return web.Response(text="ok")

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.add_routes([
        web.get("/", index), web.get("/health", health),
        web.get("/privacy", privacy), web.get("/terms", terms),
        web.get("/tiktok/login", tiktok_login), web.get("/tiktok/callback", tiktok_callback),
        web.get("/youtube/login", youtube_login), web.get("/youtube/callback", youtube_callback),
        web.post("/kick/webhook", kick_webhook),
    ])
    return app
