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
import html
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from .config import Settings
from .copywriter import write_copy
from .llm import LLM
from .translate import translate_words
from .layout import analyze
from .render import render
from .storage import Store
from .subtitles import build_ass
from .resources import Resources
from .telegram import TelegramBot, copyable
from .tiktok import TikTokClient, TikTokError
from .youtube import YouTubeClient, YouTubeError
from .transcribe import transcribe

log = logging.getLogger(__name__)

EMOJI = {"rire": "😂", "action": "🔥", "demande_clip": "🎬", "burst": "⚡",
         "clip_viewer": "📎", "top_clip": "🏆"}
OUTRO_S = 1.8
TIKTOK_PENDING_MAX = 5      # brouillons en attente acceptés par TikTok sur 24 h glissantes
FORCED_NOTE = "envoyé par toi"   # = ClipHarvester.OWNER_NOTE : jamais jeté par l'IA
DAY = 86400


def parse_hours(spec: str) -> tuple[int, int]:
    """« 11-23 » -> (11, 23) ; « 20-2 » passe minuit ; invalide -> toute la journée."""
    try:
        a, b = (int(x) for x in spec.replace("h", "").split("-", 1))
        if 0 <= a <= 24 and 0 <= b <= 24 and a != b:
            return a % 24, b % 24 if b != 24 else 24
    except ValueError:
        pass
    return 0, 24


def seconds_until_window(now: float, tz: str, spec: str) -> float:
    """0 si `now` est dans la plage horaire locale, sinon secondes jusqu'à son début."""
    start, end = parse_hours(spec)
    if (start, end) == (0, 24):
        return 0.0
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:
        zone = None
    dt = datetime.fromtimestamp(now, zone) if zone else datetime.fromtimestamp(now)
    h = dt.hour + dt.minute / 60
    inside = start <= h < end if start < end else (h >= start or h < end)
    if inside:
        return 0.0
    nxt = dt.replace(hour=start, minute=0, second=0, microsecond=0)
    if nxt <= dt:
        nxt += timedelta(days=1)
    return (nxt - dt).total_seconds()


class Pipeline:
    def __init__(self, settings: Settings, store: Store, session: aiohttp.ClientSession,
                 telegram: TelegramBot, tiktok: TikTokClient, youtube: YouTubeClient | None = None,
                 res: Resources | None = None):
        self.s = settings
        self.store = store
        self.session = session
        self.tg = telegram
        self.tiktok = tiktok
        self.youtube = youtube
        self.res = res
        # Boutique : livraison / remboursement des clips commandés par des clients
        self.on_customer_done: Callable[[dict, Path, object], Awaitable[None]] | None = None
        self.on_customer_failed: Callable[[dict, str], Awaitable[None]] | None = None
        self.final_dir = settings.capture.work_dir / "final"
        self.final_dir.mkdir(parents=True, exist_ok=True)
        self._process_wake = asyncio.Event()
        self._last_channel: str | None = None
        # TikTok refuse au-delà de 5 vidéos en attente dans la boîte de réception
        self.tiktok_full_until = 0.0
        self._full_notified = False
        self._publish_wake = asyncio.Event()
        self.force_until = 0.0     # /relance : ignore créneaux et espacement un moment
        telegram.on_approve = self.approve
        telegram.on_reject = self.reject
        telegram.on_done = self.done

    # ------------------------------------------------------------- entrées
    def wake(self) -> None:
        self.trim_backlog()
        self._process_wake.set()

    def trim_backlog(self) -> None:
        """La machine gratuite monte ~1 clip toutes les 5-10 min : si les pics arrivent
        plus vite, on ne garde en attente que les meilleurs."""
        # Jamais les commandes clients (payées) ni les clips que tu as envoyés toi-même
        rows = self.store.db.execute(
            "SELECT * FROM clips WHERE status='extracted' AND customer_id IS NULL "
            "AND COALESCE(tokens, '') NOT LIKE ? ORDER BY score DESC, created DESC",
            (FORCED_NOTE + "%",)).fetchall()
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
        if self.store.claim(clip_id, "ready", "approved") \
                or self.store.claim(clip_id, "manual", "approved"):
            self._publish_wake.set()
            targets = self.platforms()
            if not targets:
                return "Validé ✅ (aucune plateforme connectée : publie depuis la vidéo)"
            wait = self.tiktok_wait() if targets == ["tiktok"] else 0
            if wait > 60:
                return f"En file TikTok ✅ — envoi vers {self._clock(time.time() + wait)}"
            return "Envoi vers " + " + ".join(t.capitalize() for t in targets) + "… 🚀"
        return "Déjà traité"

    async def reject(self, clip_id: int) -> str:
        if self.store.claim(clip_id, "ready", "rejected") \
                or self.store.claim(clip_id, "manual", "rejected"):
            return "Jeté 🗑️"
        return "Déjà traité"

    async def done(self, clip_id: int) -> str:
        """Posté à la main depuis Telegram : compté comme publié (statistiques, Whop)."""
        if self.store.claim(clip_id, "manual", "published") \
                or self.store.claim(clip_id, "ready", "published"):
            self.store.update_clip(clip_id, publish_id="manual", published_at=time.time())
            return "Posté ✅ merci !"
        return "Déjà traité"

    # ----------------------------------------------------- rythme TikTok
    @property
    def pub(self):
        return self.s.publish

    def inbox_log(self) -> list[float]:
        """Envois dans la boîte TikTok sur les dernières 24 h (sauvegardé : survit aux
        redémarrages, pour ne pas croire à tort que les 5 places sont libres)."""
        now = time.time()
        return sorted(t for t in self.store.get("tiktok_inbox_log", []) if now - t < DAY)

    def _clock(self, t: float) -> str:
        try:
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(t, ZoneInfo(self.pub.timezone)).strftime("%Hh%M")
        except Exception:
            return datetime.fromtimestamp(t).strftime("%Hh%M")

    def tiktok_wait(self, now: float | None = None) -> float:
        """Secondes avant le prochain envoi TikTok autorisé : place libre chez TikTok,
        dans la plage horaire choisie, et assez espacé du précédent (chaque notif arrive
        à un moment où tu peux publier, au lieu de 5 brouillons qui pourrissent la nuit)."""
        now = now or time.time()
        waits = [self.tiktok_full_until - now]
        if now >= self.force_until:
            log_ = self.inbox_log()
            if log_:
                waits.append(log_[-1] + self.pub.tiktok_gap_min * 60 - now)
            waits.append(seconds_until_window(now, self.pub.timezone, self.pub.tiktok_hours))
        return max(0.0, *waits)

    def manual_log(self) -> list[float]:
        now = time.time()
        return [t for t in self.store.get("manual_log", []) if now - t < DAY]

    def _expire_stale(self) -> None:
        """Un moment de live vieux de plus de FRESH_HOURS n'est plus publié."""
        limit = time.time() - self.pub.fresh_hours * 3600
        for row in self.store.db.execute(
                "SELECT id FROM clips WHERE status='approved' AND created < ?", (limit,)).fetchall():
            if self.store.claim(row["id"], "approved", "skipped"):
                log.info("Clip %s trop ancien, retiré de la file", row["id"])

    def tiktok_status_line(self) -> str:
        if not (self.tiktok.configured and self.tiktok.connected):
            return ""
        used, now = len(self.inbox_log()), time.time()
        parts = [f"{used}/{TIKTOK_PENDING_MAX} envois sur 24 h"]
        n = self.waiting_count()
        if n:
            wait = self.tiktok_wait(now)
            parts.append(f"{n} en file · prochain envoi " +
                         ("maintenant" if wait < 60 else f"~{self._clock(now + wait)}"))
        if now < self.tiktok_full_until:
            parts.append("🟡 TikTok plein (brouillons non publiés)")
        manual = len(self.manual_log())
        if manual:
            parts.append(f"📲 {manual} envoyés sur Telegram à poster à la main")
        return "\n  " + " · ".join(parts)

    # ------------------------------------------------------- post-production
    CUSTOMER_MAX_WAIT_S = 30 * 60

    def next_job(self):
        """Notre compte d'abord ; une commande client passe devant seulement si elle
        attend depuis plus de CUSTOMER_MAX_WAIT_S (sinon un live très animé la
        bloquerait indéfiniment)."""
        oldest = self.store.db.execute(
            "SELECT * FROM clips WHERE status='extracted' AND customer_id IS NOT NULL "
            "ORDER BY created LIMIT 1").fetchone()
        if oldest and time.time() - oldest["created"] > self.CUSTOMER_MAX_WAIT_S:
            return oldest
        forced = self.store.db.execute(
            "SELECT * FROM clips WHERE status='extracted' AND customer_id IS NULL "
            "AND tokens LIKE ? ORDER BY created LIMIT 1", (FORCED_NOTE + "%",)).fetchone()
        return (forced or self.store.next_clip("extracted", avoid_channel=self._last_channel,
                                               own_only=True) or oldest)

    async def run_processor(self) -> None:
        while True:
            row = self.next_job()
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
                if row["customer_id"] and self.on_customer_failed:
                    try:
                        await self.on_customer_failed(dict(row), str(e))
                    except Exception:
                        log.exception("Remboursement du clip %s impossible", row["id"])

    async def _heavy(self, coro_fn):
        """Travail lourd (cadrage + montage) : jamais en même temps qu'un téléchargement,
        et seulement quand la mémoire le permet."""
        if self.res is None:
            return await coro_fn()
        async with self.res.heavy:
            await self.res.wait_room(200)
            return await coro_fn()

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
        customer = bool(clip.get("customer_id"))
        # Clip commandé (client) ou envoyé par toi : jamais jeté, réglages propres
        forced = customer or (clip.get("tokens") or "").startswith(FORCED_NOTE)
        if customer:
            lang = clip.get("customer_lang")
            target = "" if lang == "original" else (lang or p.target_lang)
            cta, watermark = "", clip.get("customer_tag") or ""
        else:
            target, watermark = p.target_lang, self.watermark
            # Mentions exigées par la campagne de ce streamer + appel à l'action global
            tags = p.channel_tags.get((clip["channel"] or "").lower(), "")
            whop = self.store.get("whop_campaigns", {}).get((clip["channel"] or "").lower())
            whop_rules = whop.get("rules") if whop else ""
            cta = "\n".join(x for x in (tags, whop_rules, p.cta_text) if x)
        llm = LLM.from_settings(p)
        # Note + textes d'abord : un clip mal noté n'est jamais monté (gain de CPU)
        out_lang = target or tr.language or "fr"
        copy = await write_copy(self.session, llm, clip, tr.text, out_lang, cta, p.ad_disclosure)

        if not forced and copy.score is not None and copy.score < p.min_ai_score:
            log.info("Clip %s jeté par l'IA (note %s/10)", cid, copy.score)
            self.store.update_clip(cid, status="discarded", ai_score=copy.score,
                                   transcript=tr.text, hook=copy.hook)
            src.unlink(missing_ok=True)
            self.store.update_clip(cid, raw_path=None)
            return

        words = tr.words
        translated = None
        if target:
            translated = await translate_words(self.session, llm, tr.words, tr.language, target)
        if translated:
            words = translated
            log.info("Clip %s : sous-titres traduits %s -> %s", cid, tr.language, target)

        out = self.final_dir / f"clip_{cid}.mp4"
        domain = {"twitch": "twitch.tv", "kick": "kick.com"}.get(clip["platform"], "")
        credit = f"{domain}/{clip['channel']}" if (p.video_credit and domain) else ""
        # Outro « S'abonner » : pour un client, seulement s'il a donné son pseudo
        outro_s = OUTRO_S if (self.outro and (watermark or not customer)) else 0.0

        async def montage():
            layout = await analyze(src, offset, duration)
            ass = build_ass(words, copy.hook, layout.kind, duration, p.font, credit,
                            karaoke=not translated, keywords=copy.keywords, cover=copy.cover,
                            creator=clip["channel"], watermark=watermark, outro_s=outro_s,
                            peak_at=clip.get("peak_at"))
            ok = await render(src, offset, duration, layout, ass, out,
                              height=p.output_height, preset=p.x264_preset,
                              threads=p.ffmpeg_threads, outro_s=outro_s)
            return layout, ok

        layout, ok = await self._heavy(montage)
        if not ok:
            raise RuntimeError("montage FFmpeg échoué")

        src.unlink(missing_ok=True)  # le brut ne sert plus
        if customer:
            self.store.update_clip(cid, status="delivering", raw_path=None, final_path=str(out),
                                   transcript=tr.text, hook=copy.hook, caption=copy.caption,
                                   ai_score=copy.score)
            log.info("Commande client %s montée en %.0fs", cid, time.time() - t0)
            if self.on_customer_done:
                await self.on_customer_done(dict(self.store.clip(cid)), out, copy)
            return
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
            self._expire_stale()
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
            if targets == ["tiktok"]:
                # 5 places par jour chez TikTok : seuls les meilleurs y attendent, les autres
                # bons clips partent tout de suite sur Telegram (voie manuelle, illimitée).
                await self._trim_approved()
                wait = self.tiktok_wait()
                if wait > 0:
                    self._publish_wake.clear()
                    try:
                        await asyncio.wait_for(self._publish_wake.wait(), min(60, wait))
                    except asyncio.TimeoutError:
                        pass
                    continue
                row = self.store.next_clip("approved")
                if row is None:
                    continue
                cid = row["id"]
            if not self.store.claim(cid, "approved", "publishing"):
                continue
            await self._publish_everywhere(dict(row), targets)
            # Limites TikTok/YouTube : on espace les envois
            await asyncio.sleep(30)

    MAX_WAITING = TIKTOK_PENDING_MAX   # une journée de places TikTok, pas plus

    async def _trim_approved(self) -> None:
        """File TikTok limitée aux meilleurs clips ; les autres passent sur la voie manuelle
        tant qu'ils sont frais (un moment de live perd sa valeur en quelques heures)."""
        rows = self.store.db.execute(
            "SELECT * FROM clips WHERE status='approved' "
            "ORDER BY COALESCE(ai_score,0) DESC, score DESC, created DESC").fetchall()
        # TikTok bloqué (brouillons pas encore publiés) : inutile d'y faire vieillir 5 clips
        keep = 2 if time.time() < self.tiktok_full_until else self.MAX_WAITING
        for row in rows[keep:]:
            await self._to_manual(row)

    async def _to_manual(self, row) -> None:
        cid = row["id"]
        ai = row["ai_score"] if row["ai_score"] is not None else self.pub.manual_min_score
        if ai < self.pub.manual_min_score or len(self.manual_log()) >= self.pub.manual_per_day:
            if self.store.claim(cid, "approved", "skipped"):
                log.info("Clip %s écarté (file TikTok pleine, note %s, voie manuelle %d/%d)",
                         cid, row["ai_score"], len(self.manual_log()), self.pub.manual_per_day)
            return
        if not self.store.claim(cid, "approved", "manual"):
            return
        header = (f"📲 #{cid} · {row['channel']} · IA {row['ai_score'] if row['ai_score'] is not None else '?'}/10"
                  " — à poster à la main (les 5 places TikTok du jour sont prises par "
                  "les meilleurs).\nVidéo : ⋮ → Partager → TikTok, puis colle la légende.")
        mid = await self.tg.send_clip(cid, Path(row["final_path"]), row["caption"] or "", header,
                                      buttons=[[("✅ Posté sur TikTok", f"done:{cid}"),
                                                ("🗑️ Jeter", f"rej:{cid}")]])
        if mid:
            self.store.update_clip(cid, tg_message_id=mid)
            self.store.set("manual_log", self.manual_log() + [time.time()])
        else:
            self.store.update_clip(cid, status="skipped", error="envoi Telegram impossible")

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
                    self.store.set("tiktok_inbox_log", self.inbox_log() + [time.time()])
                    report.append("TikTok : " + ("prêt dans ton app → touche la notification "
                                                 "« contenu prêt » puis Publier"
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
                    "🟡 TikTok refuse : 5 brouillons attendent déjà d'être publiés.\n"
                    "Supprimer la notification ne libère PAS la place : il faut ouvrir le "
                    "brouillon et toucher Publier. Sinon TikTok ne rend la place qu'au bout "
                    "d'environ 24 h après l'envoi.\n"
                    "En attendant, je garde les 2 meilleurs clips pour TikTok et je t'envoie "
                    "les autres bons clips ici, prêts à poster à la main.")
            return
        if ids:
            self.store.update_clip(cid, status="published", publish_id=json.dumps(ids),
                                   error=None, published_at=time.time())
            text = html.escape(f"Clip #{cid} ({row.get('channel') or '?'})\n" + "\n".join(report),
                               quote=False)
            if "tiktok" in ids and self.tiktok.mode == "inbox":
                # Le brouillon TikTok arrive SANS légende (limite de l'API) : à coller.
                text += "\n\n👇 légende à coller dans TikTok (tap pour copier)\n" + copyable(caption)
            await self.tg.send(text, html=True)
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
