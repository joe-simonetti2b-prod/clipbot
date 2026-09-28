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
  /offre             page de vente publique (à partager partout) + paiement crypto
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

TERMS = """<p>L'intégration TikTok et YouTube de ce service est réservée au compte de son
propriétaire : elle ne publie que sur ses propres comptes. Les contenus publiés respectent
les conditions d'utilisation de TikTok et les droits des créateurs concernés (crédit
systématique de la chaîne d'origine). Contact : {email}</p>"""

OFFER_CSS = """
:root{--bg:#0e0e12;--card:#17171f;--ink:#f4f4f6;--mute:#a4a4b2;--acc:#ff2d55;--acc2:#25f4ee;--line:#2a2a36}
@media (prefers-color-scheme:light){:root{--bg:#f6f6f9;--card:#fff;--ink:#131318;
--mute:#5d5d6b;--line:#e3e3ea;--acc2:#0a8f8a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:40px 16px 64px}
.tag{display:inline-block;font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;color:var(--acc2)}
h1{font-size:clamp(1.8rem,6vw,2.6rem);line-height:1.15;margin:.3em 0 .4em}
h1 em{font-style:normal;color:var(--acc)}
p.lead{color:var(--mute);font-size:1.08rem;margin:0 0 28px}
ul.feat{list-style:none;padding:0;margin:0 0 32px;display:grid;gap:10px}
ul.feat li{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.cta{display:block;text-align:center;background:var(--acc);color:#fff;text-decoration:none;
font-weight:700;padding:16px;border-radius:14px;font-size:1.05rem;margin:0 0 36px}
h2{font-size:1.2rem;margin:0 0 14px}
.packs{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.pack{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
.pack b{font-size:1.5rem;display:block}.pack .price{color:var(--mute);margin:4px 0 14px}
.pack button{width:100%;border:1px solid var(--line);background:transparent;color:var(--ink);
padding:11px;border-radius:10px;font:inherit;cursor:pointer}
.pack button:hover{border-color:var(--acc2)}
small{color:var(--mute);display:block;margin-top:28px}
"""


def offer_page(shop, bot_username: str) -> str:
    link = f"https://t.me/{bot_username}?start=offre" if bot_username else "#"
    packs = []
    for p in shop.packs:
        crypto = (f'<form method="post" action="/offre/crypto/{p.key}">'
                  f'<button type="submit">Payer {p.eur:.2f} € en crypto</button></form>'
                  if shop.crypto_enabled else "")
        packs.append(f'<div class="pack"><b>{p.credits} clips</b>'
                     f'<div class="price">{p.stars} ⭐ dans Telegram'
                     + (f" · ou {p.eur:.2f} €" if shop.crypto_enabled else "") +
                     f"</div>{crypto}</div>")
    trial = (f"{shop.cfg.free_trial} clip offert pour essayer, sans rien payer."
             if shop.cfg.free_trial else "")
    body = f"""<main>
<span class="tag">Clips Twitch &amp; Kick → TikTok</span>
<h1>Ton clip, <em>prêt à poster</em> en quelques minutes.</h1>
<p class="lead">Envoie un lien de clip Twitch ou Kick au bot Telegram : tu reçois la vidéo
verticale montée, sous-titrée, et la légende avec hashtags à copier. {trial}</p>
<ul class="feat">
<li>📐 Format 9:16 avec cadrage automatique (webcam + jeu, ou plein écran)</li>
<li>💬 Sous-titres synchronisés mot à mot, traduits si tu veux (FR, EN, ES…)</li>
<li>🪝 Accroche à l'écran, miniature, son normalisé pour TikTok / Reels / Shorts</li>
<li>🏷️ Ton @pseudo incrusté, légende + hashtags prêts à coller</li>
</ul>
<a class="cta" href="{link}">Ouvrir le bot sur Telegram</a>
<h2>Tarifs</h2>
<div class="packs">{''.join(packs)}</div>
<small>1 crédit = 1 clip livré, débité seulement à la livraison, sans expiration.
Tu dois avoir le droit de republier les clips que tu envoies (ta chaîne, campagne
de clipping officielle ou accord du streamer).</small>
</main>"""
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Clips prêts à poster</title>'
            '<meta name="description" content="Envoie un lien de clip Twitch ou Kick, reçois la '
            'vidéo TikTok montée et sous-titrée.">'
            f"<style>{OFFER_CSS}</style></head><body>{body}</body></html>")


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
                err = req.query.get("error_description") or req.query["error"]
                if a.tiktok.note_auth_error(err):
                    await a.tg.send(
                        f"⚠️ Connexion TikTok échouée ({err}) — probablement les statistiques "
                        "(user.info.stats / video.list) non activées sur l'app. J'ai coupé cette "
                        "option automatiquement : relance /tiktok pour te connecter sans elle.")
                    return web.Response(text=PAGE.format(title="Échec", body=html.escape(err)),
                                        content_type="text/html", status=400)
                raise RuntimeError(err)
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

    # ---------------------------------------------------------------- boutique
    invoice_hits: dict[str, list[float]] = {}

    async def offer(_):
        shop = getattr(a, "shop", None)
        if shop is None or not shop.enabled:
            raise web.HTTPNotFound(text="offre indisponible")
        return web.Response(text=offer_page(shop, a.shop_bot.username), content_type="text/html")

    async def offer_crypto(req: web.Request):
        import time as _t
        shop = getattr(a, "shop", None)
        if shop is None or not shop.enabled or not shop.crypto_enabled:
            raise web.HTTPNotFound(text="paiement crypto indisponible")
        # Dernière adresse de X-Forwarded-For = celle vue par le proxy de l'hébergeur
        # (les précédentes peuvent être inventées par le client).
        ip = req.headers.get("X-Forwarded-For", req.remote or "?").split(",")[-1].strip()
        now = _t.time()
        for k in list(invoice_hits):
            invoice_hits[k] = [t for t in invoice_hits[k] if now - t < 3600]
            if not invoice_hits[k]:
                del invoice_hits[k]
        if len(invoice_hits.get(ip, [])) >= 10 or sum(map(len, invoice_hits.values())) >= 40:
            raise web.HTTPTooManyRequests(text="trop de tentatives, réessaie plus tard")
        invoice_hits.setdefault(ip, []).append(now)
        try:
            url = await shop.crypto_invoice(req.match_info["pack"])
        except Exception as e:
            log.warning("Facture crypto impossible : %s", e)
            raise web.HTTPServiceUnavailable(text="paiement crypto momentanément indisponible")
        raise web.HTTPSeeOther(url)

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.add_routes([
        web.get("/offre", offer), web.post("/offre/crypto/{pack}", offer_crypto),
        web.get("/", index), web.get("/health", health),
        web.get("/privacy", privacy), web.get("/terms", terms),
        web.get("/tiktok/login", tiktok_login), web.get("/tiktok/callback", tiktok_callback),
        web.get("/youtube/login", youtube_login), web.get("/youtube/callback", youtube_callback),
        web.post("/kick/webhook", kick_webhook),
    ])
    return app
