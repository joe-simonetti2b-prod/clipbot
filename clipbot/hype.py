"""
Brique 1c — Détection des moments viraux à partir du chat.

Principe : un live « normal » a un débit de chat bruité mais stable. Un
moment viral provoque un pic brutal ET homogène (tout le monde tape la même
chose : KEKW, BUT !!!, CLIP IT). On combine donc :

  1. Débit (messages/s) comparé à une ligne de base adaptative (EWMA)
     -> z-score, robuste aux chats petits comme géants.
  2. Signaux sémantiques légers (pas de LLM ici : latence nulle, coût nul) :
       - lexique « rire »   (KEKW, LUL, mdr, ptdr, 😂…)
       - lexique « action » (POG, BUT, GOAL, WTF, 🔥…)
       - demandes explicites de clip (« clip », « clip it »)
       - taux de répétition (spam d'un même token = réaction collective)
       - diversité des auteurs (évite qu'un seul spammeur déclenche)
  3. Machine à états : on « arme » au dépassement de seuil, on suit la montée
     et on n'émet l'événement qu'au sommet du pic -> horodatage précis.

Le détecteur est piloté par `tick(now)` : aucune dépendance au temps réel,
donc entièrement testable avec des horloges simulées.
"""
from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass

from .config import HypeConfig
from .models import ChatMessage, HypeEvent

LAUGH = {
    "kekw", "lul", "lol", "lmao", "lmfao", "omegalul", "icant", "mdr", "ptdr", "xd",
    "haha", "hahaha", "jaja", "kappa", "😂", "🤣", "💀",
}
ACTION = {
    "pog", "poggers", "pogchamp", "pogu", "goal", "but", "golazo", "gg", "wtf", "omg",
    "insane", "letsgo", "sheesh", "clutch", "ez", "incroyable", "🔥", "😱", "🤯", "!!!",
}
CLIP_REQUEST = re.compile(r"\bclip+(\s*(it|ça|ca|that))?\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"[\w']+|[^\w\s]", re.UNICODE)


def _tokens(text: str) -> list[str]:
    toks = [t.lower() for t in TOKEN_RE.findall(text)]
    # « BUUUUUT », « LOOOOL » -> « but », « lol »
    return [re.sub(r"(.)\1{2,}", r"\1", t) for t in toks]


@dataclass
class WindowStats:
    rate: float = 0.0
    laugh: float = 0.0
    action: float = 0.0
    clip_req: float = 0.0
    repetition: float = 0.0
    author_diversity: float = 1.0
    top_tokens: list[str] | None = None


class HypeDetector:
    MAX_PER_AUTHOR = 3
    def __init__(self, stream_key: str, cfg: HypeConfig):
        self.stream_key = stream_key
        self.cfg = cfg
        self._msgs: deque[tuple[float, str, list[str]]] = deque()
        self._mean = 0.0
        self._var = 1.0
        self._started: float | None = None
        self._last_emit = -math.inf
        # état « armé » pendant la montée du pic
        self._armed = False
        self._peak_score = 0.0
        self._peak_ts = 0.0
        self._peak_stats: WindowStats | None = None
        self._armed_at = 0.0

    # ------------------------------------------------------------------ API
    def add(self, msg: ChatMessage) -> None:
        if self._started is None:
            self._started = msg.ts
        self._msgs.append((msg.ts, msg.author, _tokens(msg.text)))

    def tick(self, now: float) -> HypeEvent | None:
        """À appeler ~1×/s. Renvoie un HypeEvent au sommet d'un pic, sinon None."""
        if self._started is None:
            self._started = now
        self._evict(now)
        st = self._stats()
        z = (st.rate - self._mean) / math.sqrt(max(self._var, 0.25))
        score = self._score(z, st)

        warm = (now - self._started) >= self.cfg.warmup_s
        cooling = (now - self._last_emit) < self.cfg.cooldown_s
        above = warm and not cooling and score >= self.cfg.z_threshold \
            and st.rate >= self.cfg.min_msgs_per_s

        event = None
        if above:
            if not self._armed:
                self._armed, self._armed_at, self._peak_score = True, now, 0.0
            if score > self._peak_score:
                self._peak_score, self._peak_ts, self._peak_stats = score, now, st
        elif self._armed:
            event = self._emit()

        # Pic qui dure : on émet quand même après 12 s pour ne pas le rater.
        if self._armed and now - self._armed_at > 12 and \
                score < 0.7 * self._peak_score:
            event = self._emit()

        # La ligne de base n'apprend que hors pic, sinon elle « avale » la hype.
        if not self._armed:
            self._update_baseline(st.rate)
        return event

    # ------------------------------------------------------------ internes
    def _evict(self, now: float) -> None:
        horizon = now - self.cfg.window_s
        while self._msgs and self._msgs[0][0] < horizon:
            self._msgs.popleft()

    def _stats(self) -> WindowStats:
        n = len(self._msgs)
        if n == 0:
            return WindowStats(top_tokens=[])
        laugh = action = clip = 0
        firsts: Counter[str] = Counter()
        per_author: Counter[str] = Counter()
        for _, author, toks in self._msgs:
            per_author[author] += 1
            ts = set(toks)
            laugh += bool(ts & LAUGH)
            action += bool(ts & ACTION)
            clip += bool(CLIP_REQUEST.search(" ".join(toks)))
            if toks:
                firsts[toks[0]] += 1
        top = firsts.most_common(3)
        # Débit « effectif » : chaque auteur compte au plus MAX_PER_AUTHOR messages
        # par fenêtre -> un spammeur ou un bot ne peut pas fabriquer un pic seul.
        effective = sum(min(k, self.MAX_PER_AUTHOR) for k in per_author.values())
        return WindowStats(
            rate=effective / self.cfg.window_s,
            laugh=laugh / n,
            action=action / n,
            clip_req=clip / n,
            repetition=(top[0][1] / n) if top else 0.0,
            author_diversity=len(per_author) / n,
            top_tokens=[t for t, _ in top],
        )

    @staticmethod
    def _score(z: float, st: WindowStats) -> float:
        if z <= 0:
            return z
        boost = 1.0 + 0.6 * st.laugh + 0.6 * st.action + 1.5 * st.clip_req + 0.4 * st.repetition
        # Un seul auteur qui spamme : diversité faible -> score écrasé
        diversity_penalty = min(1.0, st.author_diversity * 3)
        return z * boost * diversity_penalty

    def _update_baseline(self, rate: float) -> None:
        a = self.cfg.baseline_alpha
        diff = rate - self._mean
        self._mean += a * diff
        self._var = (1 - a) * (self._var + a * diff * diff)

    def _emit(self) -> HypeEvent | None:
        st = self._peak_stats
        self._armed = False
        if st is None:
            return None
        self._last_emit = self._peak_ts
        if st.laugh >= max(st.action, 0.2):
            reason = "rire"
        elif st.action >= 0.2:
            reason = "action"
        elif st.clip_req >= 0.1:
            reason = "demande_clip"
        else:
            reason = "burst"
        ev = HypeEvent(
            stream_key=self.stream_key,
            peak_ts=self._peak_ts,
            score=round(self._peak_score, 2),
            msgs_per_s=round(st.rate, 2),
            reason=reason,
            top_tokens=st.top_tokens or [],
        )
        self._peak_stats = None
        return ev
