"""
Assemblage de l'application : un seul processus asyncio qui fait tourner
  - la veille et la capture des lives (Orchestrator)
  - la post-production et la publication (Pipeline)
  - la télécommande Telegram
  - le serveur HTTP (healthcheck, OAuth TikTok, webhook Kick)
et s'arrête proprement sur SIGTERM (redéploiement, mise à jour).
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time

import aiohttp
from aiohttp import web

from .config import Settings
from .discovery import parse_spec
from .models import HypeEvent, StreamCandidate
from .pipeline import Pipeline
from .recorder import RawClip
from .storage import Store
from .telegram import TelegramBot
from .tiktok import TikTokClient
from .backup import TelegramBackup
from .youtube import YouTubeClient
from .llm import LLM
from .watcher import Orchestrator
from .web import build_app

log = logging.getLogger(__name__)


class App:
    def __init__(self, settings: Settings):
        self.s = settings
        self.store = Store(settings.capture.work_dir / "clipbot.db")

    # ------------------------------------------------------ câblage
    def _on_clip(self, c: StreamCandidate, ev: HypeEvent, raw: RawClip) -> None:
        self.store.add_clip(
            platform=c.platform.value, channel=c.channel, stream_url=c.url,
            stream_title=c.title, category=c.category, reason=ev.reason, score=ev.score,
            tokens=", ".join(ev.top_tokens), raw_path=str(raw.path),
            trim_offset=raw.offset, trim_duration=raw.duration,
        )
        self.pipeline.wake()

    def _register_commands(self) -> None:
        tg, store = self.tg, self.store

        async def status(_):
            since = time.time() - 86400
            n = store.counts_since(since)
            lives = "\n".join(f"  • {w.c.channel} ({w.c.platform.value}, {w.chat_count} msgs de chat, "
                              f"{str(w.c.viewers) + ' viewers' if w.c.viewers else 'en live'}, "
                              f"{w.clips} clips)" for w in self.orch.watchers.values()) or "  aucun"
            tt = ("non configuré" if not self.tiktok.configured else
                  f"connecté ({self.tiktok.mode})" if self.tiktok.connected else "à connecter → /tiktok")
            yt = ("non configuré" if not self.youtube.configured else
                  "connecté" if self.youtube.connected else "à connecter → /youtube")
            return (f"{'⏸️ EN PAUSE' if self.orch.paused else '🟢 ACTIF'} · "
                    f"auto : {'on' if self.pipeline.auto else 'off'}\n"
                    f"Lives suivis :\n{lives}\n\n24 h : "
                    f"{sum(n.values())} clips · {n.get('ready', 0)} à valider · "
                    f"{n.get('published', 0)} publiés · {n.get('discarded', 0)} jetés par l'IA · "
                    f"{n.get('failed', 0)} erreurs · {n.get('skipped', 0)} écartés (file pleine)\n"
                    f"TikTok : {tt}\nYouTube : {yt}")

        async def auto(args):
            # « /auto » seul bascule ; « /auto on » / « /auto off » forcent
            if args and args[0].lower() in ("on", "off"):
                store.set("auto_publish", args[0].lower() == "on")
            elif not args:
                store.set("auto_publish", not self.pipeline.auto)
            if self.pipeline.auto:
                return ("Publication automatique : ON ✅\nChaque clip bien noté par l'IA part "
                        "seul vers tes plateformes. /auto pour couper.")
            return "Publication automatique : OFF — chaque clip attend ton ✅. /auto pour activer."

        async def pause(_):
            store.set("paused", True)
            self.orch.rescan()
            return "⏸️ Surveillance en pause (/resume pour reprendre)."

        async def resume(_):
            store.set("paused", False)
            self.orch.rescan()
            return "▶️ Surveillance relancée."

        async def add(args):
            if not args:
                return ("Usage : /add <pseudo>  — cherché sur Twitch ET Kick\n"
                        "Forcer une plateforme : /add kick:pseudo ou /add twitch:pseudo")
            added = []
            chans = store.get("extra_channels", [])
            for a in args:
                forced, name = parse_spec(a)
                spec = f"{forced}:{name}" if forced else name
                if spec not in chans:
                    chans.append(spec)
                    added.append(spec)
            store.set("extra_channels", chans)
            self.orch.rescan()
            return (f"➕ Ajouté : {', '.join(added) or 'rien de nouveau'}.\n"
                    "Je vérifie que le pseudo existe et je te dis où je l'ai trouvé.")

        async def remove(args):
            if not args:
                return "Usage : /remove <pseudo>"
            names = {parse_spec(a)[1] for a in args}
            store.set("extra_channels", [c for c in store.get("extra_channels", [])
                                         if parse_spec(c)[1] not in names])
            env = [n for n in self.s.discovery.allowed_channels if parse_spec(n)[1] in names]
            self.orch.rescan()
            msg = f"➖ Retiré : {', '.join(sorted(names))}."
            if env:
                msg += ("\n(Certaines viennent de ALLOWED_CHANNELS dans Render : "
                        "dis-le moi pour que je les retire définitivement.)")
            return msg

        async def chaines(_):
            r = self.orch.scanner.resolution
            if r is None:
                if not self.orch.scanner.specs():
                    return "Aucune liste : veille par tendances (catégories / langue)."
                return "Vérification des pseudos en cours…"
            watched = ", ".join(f"{w.c.channel} ({w.c.platform.value}, {w.c.viewers} viewers)"
                                for w in self.orch.watchers.values()) or "aucun"
            focus = [parse_spec(f)[1] for f in self.s.discovery.focus_channels]
            head = (f"★ Focus (priorité ×{self.s.discovery.focus_boost:g}) : {', '.join(focus)}\n\n"
                    if focus else "")
            return (f"{head}{r.summary()}\n\nTwitch : {', '.join(r.twitch) or '—'}\n"
                    f"Kick : {', '.join(r.kick) or '—'}\n\nEn cours de suivi : {watched}")

        async def youtube(_):
            if not self.youtube.configured:
                return ("YouTube pas encore configuré : ajoute YOUTUBE_CLIENT_ID et "
                        "YOUTUBE_CLIENT_SECRET dans les variables de l'hébergeur.")
            if not self.s.server.public_url:
                return "Adresse publique inconnue : renseigne PUBLIC_URL (https://…) dans les variables."
            return (f"Ouvre ce lien, choisis ta chaîne YouTube et accepte :\n"
                    f"{self.s.server.public_url}/youtube/login?k={self.s.publish.telegram_pair_code}")

        async def tiktok(_):
            if not self.tiktok.configured:
                return ("TikTok pas encore configuré : ajoute TIKTOK_CLIENT_KEY et "
                        "TIKTOK_CLIENT_SECRET dans les variables de l'hébergeur.")
            if not self.s.server.public_url:
                return "Adresse publique inconnue : renseigne PUBLIC_URL (https://…) dans les variables."
            return (f"Ouvre ce lien et accepte :\n{self.s.server.public_url}/tiktok/login"
                    f"?k={self.s.publish.telegram_pair_code}")

        tg.commands.update({"status": status, "auto": auto, "pause": pause, "resume": resume,
                            "add": add, "remove": remove, "chaines": chaines, "tiktok": tiktok, "youtube": youtube})

    # ------------------------------------------------------ exécution
    async def run(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            self.tg = TelegramBot(self.s.publish.telegram_token, self.s.publish.telegram_pair_code,
                                  self.store, session, self.s.publish.telegram_owner_id)
            # Hébergeur sans disque : on restaure connexions et réglages depuis Telegram
            self.backup = TelegramBackup(self.tg, self.store, self.s.publish.telegram_token)
            await self.backup.restore()
            self.store.on_set = self.backup.mark_dirty
            self.tiktok = TikTokClient(self.s.publish.tiktok_client_key,
                                       self.s.publish.tiktok_client_secret,
                                       self.s.publish.tiktok_mode, self.s.server.public_url,
                                       self.store, session)
            self.youtube = YouTubeClient(self.s.publish.youtube_client_id,
                                         self.s.publish.youtube_client_secret,
                                         self.s.publish.youtube_privacy, self.s.server.public_url,
                                         self.store, session)
            self.pipeline = Pipeline(self.s, self.store, session, self.tg, self.tiktok, self.youtube)
            self.orch = Orchestrator(self.s, self.store, session, self._on_clip)
            self.orch.notify = lambda text: asyncio.create_task(self.tg.send("🔎 " + text))
            self._register_commands()

            runner = web.AppRunner(build_app(self))
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", self.s.server.port).start()
            log.info("Serveur HTTP sur le port %d", self.s.server.port)

            tasks = [asyncio.create_task(coro, name=name) for name, coro in (
                ("orchestrator", self.orch.run()),
                ("processor", self.pipeline.run_processor()),
                ("publisher", self.pipeline.run_publisher()),
                ("janitor", self.pipeline.run_janitor()),
                ("telegram", self.tg.run()),
                ("keepalive", self._keepalive(session)),
                ("backup", self.backup.run()),
            )]
            self._log_config()
            await self.tg.send("🟢 clipbot démarré. /status")
            await self._check_tiktok()
            await self._remind_logins()

            await stop.wait()
            log.info("Arrêt demandé : fermeture propre…")
            await self.tg.send("🔄 clipbot redémarre (mise à jour ou maintenance).")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.orch.stop_all()
            await runner.cleanup()

    async def _check_tiktok(self) -> None:
        if not self.tiktok.configured:
            return
        ok, why = await self.tiktok.check_credentials()
        log.info("Identifiants TikTok : %s (%s)", {True: "OK", False: "REFUSÉS", None: "?"}[ok], why)
        if ok is False:
            await self.tg.send("❌ TikTok refuse TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET : "
                               f"{why}\nRecopie-les exactement (majuscules comprises) dans Render.")
        elif ok:
            await self.tg.send("✅ Identifiants TikTok valides.")

    async def _remind_logins(self) -> None:
        """Hébergeur sans disque (Render) : les connexions sont perdues au redémarrage.
        On envoie directement les liens pour les refaire en un tap."""
        base, k = self.s.server.public_url, self.s.publish.telegram_pair_code
        if not (base and k):
            return
        links = []
        if self.tiktok.configured and not self.tiktok.connected:
            links.append(f"TikTok : {base}/tiktok/login?k={k}")
        if self.youtube.configured and not self.youtube.connected:
            links.append(f"YouTube : {base}/youtube/login?k={k}")
        if links:
            await self.tg.send("🔑 Reconnexion nécessaire (un tap chacun) :\n" + "\n".join(links))

    async def _keepalive(self, session: aiohttp.ClientSession) -> None:
        """Render gratuit endort le service après 15 min sans requête entrante :
        on s'appelle soi-même par l'URL publique toutes les 10 min."""
        url = self.s.server.public_url
        if not (self.s.server.keepalive and url):
            return
        log.info("Keep-alive actif sur %s/health", url)
        while True:
            await asyncio.sleep(600)
            try:
                async with session.get(f"{url}/health") as r:
                    await r.read()
            except Exception as e:
                log.warning("Keep-alive : %s", e)

    def _log_config(self) -> None:
        d, p = self.s.discovery, self.s.publish
        log.info("Config : %s", json.dumps({
            "twitch_api": bool(d.twitch_client_id), "youtube_api": bool(d.youtube_api_key),
            "kick_api": bool(d.kick_client_id), "telegram": bool(p.telegram_token),
            "tiktok": bool(p.tiktok_client_key), "tiktok_mode": p.tiktok_mode,
            "ia": LLM.from_settings(self.s.processing).provider or "aucune",
            "traduction": self.s.processing.target_lang or "non",
            "youtube_upload": bool(self.s.publish.youtube_client_id),
            "transcription": "groq" if self.s.processing.groq_api_key
            else f"local:{self.s.processing.whisper_model}",
            "sortie": f"{self.s.processing.output_height}p", "public_url": self.s.server.public_url,
            "chaines": d.allowed_channels + self.store.get("extra_channels", []),
        }))
