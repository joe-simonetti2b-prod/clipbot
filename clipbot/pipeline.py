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

EMOJI = {"rire": "😂", "action": "🔥", "demande_clip": "🎬", "burst": "⚡",
         "clip_viewer": "📎", "top_clip": "🏆"}
OUTRO_S = 1.8


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
        self._last_channel: str | None = None
        # TikTok refuse au-delà de 5 vidéos en attente dans la boîte de réception
        self.tiktok_full_until = 0.0
        self._full_notified = False
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
        rows = self.store.db.execute(
            "SELECT * FROM clips WHERE status='extracted' ORDER BY score DESC, created DESC"
        ).fetchall()
        # Le meilleur clip de chaque créateur est toujours gardé, puis les meilleurs scores
        keep, seen = [], set()
        for r in rows:
            if r["channel"] not in seen:
                keep.append(r["id"])
                seen.add(r["channel"])
        for r in rows:
            if len(keep) >= max(self.s.processing.max_backlog, len(seen)):
                break
            if r["id"] not in keep:
                keep.append(r["id"])
        for row in (r for r in rows if r["id"] not in keep):
            if self.store.claim(row["id"], "extracted", "skipped"):
                if row["raw_path"]:
                    Path(row["raw_path"]).unlink(missing_ok=True)
                self.store.update_clip(row["id"], raw_path=None)
                log.info("Clip %s abandonné (file pleine, score %.1f)", row["id"], row["score"] or 0)

    @property
    def watermark(self) -> str:
        w = self.store.get("watermark")        # /tag depuis Telegram prime sur WATERMARK
        return self.s.processing.watermark if w is None else w

    @property
    def outro(self) -> bool:
        return self.store.get("outro", self.s.processing.outro)

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
            row = self.store.next_clip("extracted", avoid_channel=self._last_channel)
            if row is None:
                self._process_wake.clear()
                try:
                    await asyncio.wait_for(self._process_wake.wait(), 30)
                except asyncio.TimeoutError:
                    pass
                continue
            if not self.store.claim(row["id"], "extracted", "processing"):
                continue
            self._last_channel = row["channel"]
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
        whop = self.store.get("whop_campaigns", {}).get((clip["channel"] or "").lower())
        whop_rules = whop.get("rules") if whop else ""
        cta = "\n".join(x for x in (tags, whop_rules, p.cta_text) if x)
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
        outro_s = OUTRO_S if self.outro else 0.0
        ass = build_ass(words, copy.hook, layout.kind, duration, p.font, credit,
                        karaoke=not translated, keywords=copy.keywords, cover=copy.cover,
                        creator=clip["channel"], watermark=self.watermark, outro_s=outro_s,
                        peak_at=clip.get("peak_at"))
        if not await render(src, offset, duration, layout, ass, out,
                            height=p.output_height, preset=p.x264_preset,
                            threads=p.ffmpeg_threads, outro_s=outro_s):
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
                  + (f" · IA {copy.score}/10" if copy.score is not None else "")
                  + (f"\n{clip['tokens']}" if clip["reason"] in ("clip_viewer", "top_clip") else ""))
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
            if targets == ["tiktok"] and time.time() < self.tiktok_full_until:
                # File TikTok pleine : les clips attendent (les meilleurs d'abord)
                await self._trim_approved()
                self._publish_wake.clear()
                try:
                    await asyncio.wait_for(self._publish_wake.wait(),
                                           min(60, self.tiktok_full_until - time.time()))
                except asyncio.TimeoutError:
                    pass
                continue
            if not self.store.claim(cid, "approved", "publishing"):
                continue
            await self._publish_everywhere(dict(row), targets)
            # Limites TikTok/YouTube : on espace les envois
            await asyncio.sleep(30)

    MAX_WAITING = 8

    async def _trim_approved(self) -> None:
        """File d'attente TikTok limitée aux 8 meilleurs clips ; les autres reviennent
        dans Telegram (boutons ✅/❌) pour une publication à la main."""
        rows = self.store.db.execute(
            "SELECT * FROM clips WHERE status='approved' "
            "ORDER BY COALESCE(ai_score,0) DESC, score DESC").fetchall()
        for row in rows[self.MAX_WAITING:]:
            if self.store.claim(row["id"], "approved", "ready"):
                mid = await self.tg.send_clip(row["id"], Path(row["final_path"]), row["caption"] or "",
                                              f"#{row['id']} · file TikTok pleine, à publier à la main")
                if mid:
                    self.store.update_clip(row["id"], tg_message_id=mid)

    def waiting_count(self) -> int:
        return self.store.db.execute(
            "SELECT COUNT(*) FROM clips WHERE status='approved'").fetchone()[0]

    async def _publish_everywhere(self, row: dict, targets: list[str]) -> None:
        cid, path = row["id"], Path(row["final_path"])
        caption, hook = row["caption"] or "", row["hook"] or ""
        ids, report = {}, []
        tiktok_full = False
        for t in targets:
            try:
                if t == "tiktok":
                    ids[t] = await self.tiktok.publish(path, caption,
                                                       branded=self.s.processing.ad_disclosure)
                    self._full_notified = False
                    report.append("TikTok : " + ("prêt dans ton app → Notifications système, "
                                                 "touche la notif puis Publier"
                                                 if self.tiktok.mode == "inbox" else "publié ✅"))
                elif t == "youtube":
                    ids[t] = await self.youtube.publish(path, caption, hook)
                    report.append(f"YouTube Shorts ✅ youtube.com/shorts/{ids[t]}")
            except (TikTokError, YouTubeError) as e:
                if t == "tiktok" and "too_many_pending" in str(e):
                    tiktok_full = True
                else:
                    report.append(f"{t.capitalize()} ⚠️ {e}")
            except Exception as e:  # une plateforme en panne ne bloque pas les autres
                log.exception("Publication %s échouée", t)
                report.append(f"{t.capitalize()} ⚠️ {e}")
        if tiktok_full and not ids:
            # Pas une erreur : 5 vidéos attendent déjà dans TikTok. Le clip reste en file,
            # nouvel essai toutes les 15 min (ou dès qu'un autre clip est validé).
            self.tiktok_full_until = time.time() + 15 * 60
            self.store.update_clip(cid, status="approved", error=None)
            log.info("File TikTok pleine : clip %s mis en attente", cid)
            if not self._full_notified:
                self._full_notified = True
                await self.tg.send(
                    "🟡 5 vidéos attendent déjà dans TikTok (limite de TikTok).\n"
                    "Ouvre TikTok → Messages → Notifications système → « Ton contenu de "
                    "Joe-clipbot est prêt » → Publier (ou supprimer).\n"
                    "Je garde les meilleurs clips en file et je les envoie dès qu'il y a de la place.")
            return
        if ids:
            self.store.update_clip(cid, status="published", publish_id=json.dumps(ids),
                                   error=None, published_at=time.time())
            await self.tg.send(f"Clip #{cid}\n" + "\n".join(report))
            return
        # Échec partout (ex. limite de 5 brouillons TikTok atteinte) : le clip n'est pas
        # perdu, il revient dans Telegram avec ses boutons pour réessayer plus tard.
        self.store.update_clip(cid, status="ready", error="; ".join(report)[:500])
        header = f"⚠️ #{cid} non publié — " + " / ".join(report)
        mid = await self.tg.send_clip(cid, path, caption, header[:300])
        if mid:
            self.store.update_clip(cid, tg_message_id=mid)
        else:
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
