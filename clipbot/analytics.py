"""
Statistiques TikTok (missions/Creator Rewards) et retrouvailles automatiques des
vidéos publiées (utile pour soumettre le lien à une campagne Whop sans avoir à
le chercher toi-même dans l'app).

Nécessite les scopes user.info.stats + video.list (facultatifs : voir tiktok.py).
Sans eux, ce module ne fait rien — le reste du bot n'en dépend pas.

Éligibilité TikTok Creator Rewards (payée par TikTok directement, indépendamment
de Whop) : 10 000 abonnés ET 100 000 vues sur les 30 derniers jours, vidéos
publiées ≥ 60 s. On calcule les deux à partir des vraies données du compte.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .storage import Store
from .tiktok import TikTokClient

log = logging.getLogger(__name__)

EVERY_S = 3 * 3600
FIRST_RUN_DELAY_S = 300
MATCH_WINDOW_S = (300, 3600 * 3)     # une vidéo publiée entre 5 min et 3h après l'appel
REWARDS_FOLLOWERS = 10_000
REWARDS_VIEWS_30D = 100_000


class TikTokAnalytics:
    def __init__(self, store: Store, tiktok: TikTokClient, notify):
        self.store = store
        self.tiktok = tiktok
        self.notify = notify   # async fn(str) -> None (Telegram)

    async def run(self) -> None:
        await asyncio.sleep(FIRST_RUN_DELAY_S)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Analytics TikTok : cycle en échec")
            await asyncio.sleep(EVERY_S)

    async def tick(self) -> None:
        if not (self.tiktok.connected and self.tiktok.analytics):
            return
        stats = await self.tiktok.creator_stats()
        videos = await self.tiktok.video_list(max_count=30)
        if stats:
            self.store.set("tiktok_stats", {
                "followers": stats.get("follower_count", 0), "likes": stats.get("likes_count", 0),
                "videos": stats.get("video_count", 0), "at": time.time(),
            })
        if videos:
            now = time.time()
            views_30d = sum(v.get("view_count", 0) for v in videos
                            if now - float(v.get("create_time", now)) < 30 * 86400)
            self.store.set("tiktok_views_30d", {"views": views_30d, "at": now})
            self._match(videos)
        await self._check_rewards()

    def _match(self, videos: list[dict]) -> None:
        rows = self.store.db.execute(
            "SELECT id, published_at, caption FROM clips WHERE status='published' "
            "AND tiktok_video_id IS NULL AND published_at IS NOT NULL"
        ).fetchall()
        if not rows:
            return
        claimed = {r[0] for r in self.store.db.execute(
            "SELECT tiktok_video_id FROM clips WHERE tiktok_video_id IS NOT NULL")}
        free = [v for v in videos if str(v.get("id")) not in claimed]
        for row in rows:
            cid, pub_at, caption = row["id"], row["published_at"], row["caption"] or ""
            lo, hi = pub_at + MATCH_WINDOW_S[0], pub_at + MATCH_WINDOW_S[1]
            candidates = [v for v in free if lo <= float(v.get("create_time", 0)) <= hi]
            if not candidates:
                continue
            first_words = " ".join(caption.split()[:4]).lower()
            best = min(candidates, key=lambda v: (
                0 if first_words and first_words in (v.get("video_description") or "").lower() else 1,
                abs(float(v.get("create_time", 0)) - pub_at),
            ))
            free.remove(best)
            self.store.update_clip(cid, tiktok_video_id=str(best.get("id")),
                                   tiktok_url=best.get("share_url") or "",
                                   tiktok_views=int(best.get("view_count") or 0))
            log.info("Clip %s relié à la vidéo TikTok %s (%s)", cid, best.get("id"),
                     best.get("share_url"))
            asyncio.ensure_future(self._on_matched(cid))

    async def _on_matched(self, cid: int) -> None:
        row = self.store.clip(cid)
        if not row or not row["tiktok_url"]:
            return
        whop = self.store.get("whop_campaigns", {}).get((row["channel"] or "").lower())
        if whop:
            await self.notify(
                f"🎯 Clip #{cid} ({row['channel']}) publié sur TikTok, campagne Whop active "
                f"({whop.get('rate', '?')}) :\n{row['tiktok_url']}\n"
                "Colle ce lien sur Whop pour être payé.")

    async def _check_rewards(self) -> None:
        stats, views = self.store.get("tiktok_stats"), self.store.get("tiktok_views_30d")
        if not (stats and views):
            return
        followers, v30 = stats["followers"], views["views"]
        pct = min(followers / REWARDS_FOLLOWERS, v30 / REWARDS_VIEWS_30D)
        last = self.store.get("rewards_last_pct", 0.0)
        milestone = next((m for m in (1.0, 0.75, 0.5, 0.25) if pct >= m > last), None)
        self.store.set("rewards_last_pct", max(last, min(pct, 1.0)))
        if milestone == 1.0:
            await self.notify(
                "🏆 Seuils des missions TikTok (Creator Rewards) atteints : "
                f"{followers} abonnés, {v30} vues/30j. Tu peux l'activer dans l'app TikTok "
                "(Paramètres → Outils créateur → Programme de récompenses).")
        elif milestone:
            await self.notify(f"📈 Missions TikTok : {pct:.0%} du seuil "
                              f"({followers}/{REWARDS_FOLLOWERS} abonnés, "
                              f"{v30}/{REWARDS_VIEWS_30D} vues/30j).")

    def status_line(self) -> str:
        if not self.tiktok.analytics:
            return ""
        stats, views = self.store.get("tiktok_stats"), self.store.get("tiktok_views_30d")
        if not stats:
            return "\n  (statistiques pas encore récupérées)"
        pct = min(stats["followers"] / REWARDS_FOLLOWERS,
                  (views or {}).get("views", 0) / REWARDS_VIEWS_30D)
        return (f"\n  {stats['followers']} abonnés · {(views or {}).get('views', 0)} vues/30j · "
                f"missions TikTok : {pct:.0%} du seuil")
