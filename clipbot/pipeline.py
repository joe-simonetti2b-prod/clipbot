"""
Pipeline de post-production et de publication.

  Processor  : extracted -> processing -> ready | discarded | failed
               transcription -> textes IA (+ note) -> cadrage 9:16 -> montage
               -> envoi Telegram (ou publication directe si /auto on)
  Publisher  : approved -> publishing -> published | failed
  Janitor    : supprime les fichiers des clips terminés après RETENTION_DAYS

Un seul montage à la fois (CPU limité, coût maîtrisé) ; la détection et la
capture continuent en parallèle, les clips attendent en file dans SQLite.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp

from .config import Settings
from .copywriter import write_copy
from .llm import LLM
from .translate import translate_words
from .layout import analyze
from .render import render
from .storage import Store
from .subtitles import build_ass
from .telegram import TelegramBot
from .tiktok import TikTokClient, TikTokError
from .youtube import YouTubeClient, YouTubeError
from .transcribe import transcribe

log = logging.getLogger(__name__)

EMOJI = {"rire": "😂", "action": "🔥", "demande_clip": "🎬", "burst": "⚡"}


class Pipeline:
    def __init__(self, settings: Settings, store: Store, session: aiohttp.ClientSession,
                 telegram: TelegramBot, tiktok: TikTokClient, youtube: YouTubeClient | None = None):
        self.s = settings
        self.store = store
        self.session = session
        self.tg = telegram
        self.tiktok = tiktok
        self.youtube = youtube
        self.final_dir = settings.capture.work_dir / "final"
        self.final_dir.mkdir(parents=True, exist_ok=True)
        self._process_wake = asyncio.Event()
        self._publish_wake = asyncio.Event()
        telegram.on_approve = self.approve
        telegram.on_reject = self.reject

    # ------------------------------------------------------------- entrées
    def wake(self) -> None:
        self.trim_backlog()
        self._process_wake.set()

    def trim_backlog(self) -> None:
        """La machine gratuite monte ~1 clip toutes les 5-10 min : si les pics arrivent
        plus vite, on ne garde en attente que les meilleurs."""
        for row in self.store.overflow("extracted", self.s.processing.max_backlog):
            if self.store.claim(row["id"], "extracted", "skipped"):
                if row["raw_path"]:
                    Path(row["raw_path"]).unlink(missing_ok=True)
                self.store.update_clip(row["id"], raw_path=None)
                log.info("Clip %s abandonné (file pleine, score %.1f)", row["id"], row["score"] or 0)

    @property
    def auto(self) -> bool:
        return self.store.get("auto_publish", self.s.publish.auto_publish)

    def platforms(self) -> list[str]:
        """Plateformes configurées ET connectées, prêtes à publier."""
        out = []
        if self.tiktok.configured and self.tiktok.connected:
            out.append("tiktok")
        if self.youtube and self.youtube.configured and self.youtube.connected:
            out.append("youtube")
        return out

    async def approve(self, clip_id: int) -> str:
        if self.store.claim(clip_id, "ready", "approved"):
            self._publish_wake.set()
            targets = self.platforms()
            if not targets:
                return "Validé ✅ (aucune plateforme connectée : publie depuis la vidéo)"
            return "Envoi vers " + " + ".join(t.capitalize() for t in targets) + "… 🚀"
        return "Déjà traité"

    async def reject(self, clip_id: int) -> str:
        if self.store.claim(clip_id, "ready", "rejected"):
            return "Jeté 🗑️"
        return "Déjà traité"

    # ------------------------------------------------------- post-production
    async def run_processor(self) -> None:
        while True:
            row = self.store.next_clip("extracted")
            if row is None:
                self._process_wake.clear()
                try:
                    await asyncio.wait_for(self._process_wake.wait(), 30)
                except asyncio.TimeoutError:
                    pass
                continue
            if not self.store.claim(row["id"], "extracted", "processing"):
                continue
            try:
                await self._process(dict(row))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Clip %s en échec", row["id"])
                self.store.update_clip(row["id"], status="failed", error=str(e)[:500])

    async def _process(self, clip: dict) -> None:
        cid = clip["id"]
        src = Path(clip["raw_path"])
        if not src.exists():
            raise FileNotFoundError(src)
        offset, duration = clip["trim_offset"] or 0.0, clip["trim_duration"]
        p = self.s.processing
        t0 = time.time()

        tr = await transcribe(src, offset, duration, p.whisper_model, p.whisper_threads,
                              groq_key=p.groq_api_key, session=self.session,
                              keep_loaded=p.whisper_keep_loaded)
        # Mentions exigées par la campagne de ce streamer + appel à l'action global
        tags = p.channel_tags.get((clip["channel"] or "").lower(), "")
        cta = "\n".join(x for x in (tags, p.cta_text) if x)
        llm = LLM.from_settings(p)
        # Note + textes d'abord : un clip mal noté n'est jamais monté (gain de CPU)
        out_lang = p.target_lang or tr.language or "fr"
        copy = await write_copy(self.session, llm, clip, tr.text, out_lang, cta, p.ad_disclosure)

        if copy.score is not None and copy.score < p.min_ai_score:
            log.info("Clip %s jeté par l'IA (note %s/10)", cid, copy.score)
            self.store.update_clip(cid, status="discarded", ai_score=copy.score,
                                   transcript=tr.text, hook=copy.hook)
            src.unlink(missing_ok=True)
            self.store.update_clip(cid, raw_path=None)
            return

        words = tr.words
        translated = await translate_words(self.session, llm, tr.words, tr.language, p.target_lang)
        if translated:
            words = translated
            log.info("Clip %s : sous-titres traduits %s -> %s", cid, tr.language, p.target_lang)

        layout = await analyze(src, offset, duration)
        out = self.final_dir / f"clip_{cid}.mp4"
        domain = {"twitch": "twitch.tv", "kick": "kick.com"}.get(clip["platform"], "")
        credit = f"{domain}/{clip['channel']}" if (p.video_credit and domain) else ""
        ass = build_ass(words, copy.hook, layout.kind, duration, p.font, credit)
        if not await render(src, offset, duration, layout, ass, out,
                            height=p.output_height, preset=p.x264_preset,
                            threads=p.ffmpeg_threads):
            raise RuntimeError("montage FFmpeg échoué")

        src.unlink(missing_ok=True)  # le brut ne sert plus
        self.store.update_clip(cid, status="ready", raw_path=None, final_path=str(out),
                               transcript=tr.text, hook=copy.hook, caption=copy.caption,
                               ai_score=copy.score)
        log.info("Clip %s prêt en %.0fs (%s, note %s)", cid, time.time() - t0,
                 layout.kind, copy.score)

        if self.auto and self.store.claim(cid, "ready", "approved"):
            self._publish_wake.set()
            return
        header = (f"{EMOJI.get(clip['reason'], '⚡')} #{cid} · {clip['channel']} "
                  f"({clip['platform']}) · {duration:.0f}s"
                  + (f" · IA {copy.score}/10" if copy.score is not None else ""))
        mid = await self.tg.send_clip(cid, out, copy.caption, header)
        if mid:
            self.store.update_clip(cid, tg_message_id=mid)

    # ------------------------------------------------------------ publication
    async def run_publisher(self) -> None:
        while True:
            row = self.store.next_clip("approved")
            if row is None:
                self._publish_wake.clear()
                try:
                    await asyncio.wait_for(self._publish_wake.wait(), 60)
                except asyncio.TimeoutError:
                    pass
                continue
            cid = row["id"]
            targets = self.platforms()
            if not targets:
                # Aucune plateforme branchée : la vidéo est dans Telegram, publication manuelle.
                self.store.update_clip(cid, status="published", publish_id="manual")
                if self.auto:
                    await self.tg.send_clip(cid, Path(row["final_path"]), row["caption"] or "",
                                            f"#{cid} prêt (auto) — aucune plateforme connectée")
                continue
            if not self.store.claim(cid, "approved", "publishing"):
                continue
            await self._publish_everywhere(dict(row), targets)
            # Limites TikTok/YouTube : on espace les envois
            await asyncio.sleep(30)

    async def _publish_everywhere(self, row: dict, targets: list[str]) -> None:
        cid, path = row["id"], Path(row["final_path"])
        caption, hook = row["caption"] or "", row["hook"] or ""
        ids, report = {}, []
        for t in targets:
            try:
                if t == "tiktok":
                    ids[t] = await self.tiktok.publish(path, caption,
                                                       branded=self.s.processing.ad_disclosure)
                    report.append("TikTok : " + ("brouillon dans ton app, touche Publier"
                                                 if self.tiktok.mode == "inbox" else "publié ✅"))
                elif t == "youtube":
                    ids[t] = await self.youtube.publish(path, caption, hook)
                    report.append(f"YouTube Shorts ✅ youtube.com/shorts/{ids[t]}")
            except (TikTokError, YouTubeError) as e:
                report.append(f"{t.capitalize()} ⚠️ {e}")
            except Exception as e:  # une plateforme en panne ne bloque pas les autres
                log.exception("Publication %s échouée", t)
                report.append(f"{t.capitalize()} ⚠️ {e}")
        status = "published" if ids else "failed"
        self.store.update_clip(cid, status=status, publish_id=json.dumps(ids) if ids else None,
                               error=None if ids else "; ".join(report)[:500])
        await self.tg.send(f"Clip #{cid}\n" + "\n".join(report))

    # ---------------------------------------------------------------- ménage
    async def run_janitor(self) -> None:
        while True:
            limit = time.time() - self.s.processing.retention_days * 86400
            for row in self.store.expired(limit):
                for key in ("raw_path", "final_path"):
                    if row[key]:
                        Path(row[key]).unlink(missing_ok=True)
                self.store.update_clip(row["id"], raw_path=None, final_path=None)
            await asyncio.sleep(3600)
