"""
Textes de publication : accroche (hook), légende, hashtags, note de viralité.

Avec une IA (Claude, ou Groq gratuit) : le modèle lit la transcription et le contexte du live,
écrit le texte ET note le clip de 1 à 10 — les clips sous MIN_AI_SCORE sont
jetés automatiquement (moins de tri pour toi).
Sans clé : textes générés par gabarits, aucun tri automatique.

Crédit du streamer et mention « Publicité » sont ajoutés par le code, jamais
laissés au bon vouloir du modèle.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import aiohttp

from .llm import LLM, ask_json

log = logging.getLogger(__name__)

PLATFORM_LABEL = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}
REASON_HOOK = {
    "rire": "Personne ne s'attendait à ça 😂",
    "action": "Le moment où tout le chat a explosé 🔥",
    "demande_clip": "Le chat a crié « CLIP ! » 🎬",
    "burst": "Regarde jusqu'à la fin 👀",
    "clip_viewer": "Le moment que tout le chat a clippé 🎬",
    "top_clip": "Le clip le plus vu du jour 🏆",
}
REASON_COVER = {"rire": "FOU RIRE", "action": "IL EXPLOSE", "demande_clip": "CLIP !",
                "burst": "INCROYABLE", "clip_viewer": "LE MOMENT", "top_clip": "LE CLIP DU JOUR"}

PROMPT = """Tu es éditeur de clips courts viraux (TikTok/Shorts) pour du streaming, du sport et de l'entertainment.

Contexte du live :
- Chaîne : {channel} ({platform})
- Titre du live : {title}
- Catégorie : {category}
- Signal du chat au moment du pic : {reason} (mots dominants : {tokens})

Transcription du clip ({duration:.0f} s, [secondes] depuis le début) :
\"\"\"{transcript}\"\"\"
{style}
Règles :
- Le spectateur TikTok n'a PAS vu le live. Note ≤ 4 un moment qui n'a de sens que pour les
  habitués : dons, abonnements, raids, lecture du chat, blague interne, attente, discussion
  sans enjeu. Note haut ce qui se comprend en 2 secondes : action, réaction forte, clash,
  surprise, exploit, échec.
- Accroche et titre CONCRETS : qui, quoi, quel enjeu. Mots vides interdits : INSANE,
  INCROYABLE, DE FOLIE, CLASS, CHAPITRE, LÉGENDAIRE, HALLUCINANT, WOW.
- "start" : seconde où couper le début pour entrer tout de suite dans l'action (0 si le
  début est déjà bon). Coupe l'installation molle, jamais le contexte indispensable.
- "end" : seconde de fin, juste après la chute (null pour garder jusqu'au bout).

Réponds UNIQUEMENT avec un objet JSON, en {lang} :
{{
  "score": entier 1-10 (10 = viral évident, 1 = rien ne se passe),
  "hook": "accroche de 4 à 9 mots affichée en haut de la vidéo, qui donne envie de rester sans spoiler la chute",
  "cover": "titre de miniature de 2 à 4 mots, concret et intrigant (ex : IL CRAQUE EN LIVE), sans emoji",
  "keywords": ["2 à 4 mots forts prononcés dans la transcription (dans la langue des sous-titres), à surligner"],
  "caption": "légende de 1 à 2 phrases, naturelle, qui pousse au commentaire",
  "hashtags": ["5 à 7 hashtags pertinents, sans #, mélange niche + large"],
  "start": nombre,
  "end": nombre ou null
}}"""


def timed_transcript(words, every_s: float = 4.0) -> str:
    """Transcription découpée en lignes « [12s] … » pour que l'IA situe les moments."""
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None:
            t0 = w.start
        if w.start - t0 >= every_s and cur:
            lines.append(f"[{t0:.0f}s] " + " ".join(cur))
            cur, t0 = [], w.start
        cur.append(w.text)
    if cur:
        lines.append(f"[{t0:.0f}s] " + " ".join(cur))
    return "\n".join(lines)


@dataclass
class Copy:
    hook: str
    caption: str
    score: int | None
    cover: str = ""
    keywords: tuple[str, ...] = ()
    start: float = 0.0              # coupe proposée par l'IA (début mou)
    end: float | None = None        # fin proposée (après la chute)


def _finalize(body: str, hashtags: list[str], channel: str, platform: str,
              cta: str, ad: bool) -> str:
    tags = " ".join("#" + re.sub(r"[^\w]", "", t.lstrip("#")) for t in hashtags if t.strip())
    parts = []
    if ad:
        parts.append("Publicité")
    parts.append(body.strip())
    parts.append(f"🎥 {channel} en live sur {PLATFORM_LABEL.get(platform, platform)}")
    if cta:
        parts.append(cta)
    parts.append(tags)
    return "\n\n".join(p for p in parts if p)[:2000]


def fallback_copy(clip: dict, cta: str, ad: bool) -> Copy:
    title = (clip.get("stream_title") or "").strip()
    hook = REASON_HOOK.get(clip.get("reason"), REASON_HOOK["burst"])
    cat = re.sub(r"[^\w]", "", (clip.get("category") or "").lower())
    tags = [t for t in (cat, clip.get("channel", ""), "clip", "stream", "pourtoi", "fyp") if t]
    body = title[:150] if title else hook
    return Copy(hook, _finalize(body, tags, clip["channel"], clip["platform"], cta, ad), None,
                cover=REASON_COVER.get(clip.get("reason"), "INCROYABLE"))


LANG_NAMES = {"fr": "français", "en": "anglais", "es": "espagnol", "de": "allemand",
              "it": "italien", "pt": "portugais"}


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


async def write_copy(session: aiohttp.ClientSession, llm: LLM, clip: dict, transcript: str,
                     out_lang: str, cta: str, ad: bool, style: str = "", words=None) -> Copy:
    """out_lang : langue de la légende et de l'accroche (celle des sous-titres affichés).
    style : consignes propres au compte (ex : esport). words : transcription horodatée."""
    if not llm.enabled:
        return fallback_copy(clip, cta, ad)
    text = timed_transcript(words) if words else transcript
    prompt = PROMPT.format(
        channel=clip["channel"], platform=PLATFORM_LABEL.get(clip["platform"], clip["platform"]),
        title=clip.get("stream_title") or "?", category=clip.get("category") or "?",
        reason=clip.get("reason"), tokens=clip.get("tokens") or "?",
        duration=clip.get("trim_duration") or 0,
        transcript=(text or "(aucune parole détectée)")[:4000],
        lang=LANG_NAMES.get(out_lang, "français"), style=style,
    )
    obj = await ask_json(session, llm, prompt, max_tokens=600)
    if not obj:
        return fallback_copy(clip, cta, ad)
    try:
        return Copy(
            hook=str(obj.get("hook", "")).strip()[:90],
            caption=_finalize(str(obj.get("caption", "")), list(obj.get("hashtags", []))[:8],
                              clip["channel"], clip["platform"], cta, ad),
            score=max(1, min(10, int(obj.get("score", 5)))),
            cover=(re.sub(r"[^\w\s'!?-]", "", str(obj.get("cover") or "")).strip()[:32]
                   or REASON_COVER.get(clip.get("reason"), "INCROYABLE")),
            keywords=tuple(str(k)[:30] for k in (obj.get("keywords") or [])[:4]
                           if isinstance(k, str)),
            start=max(0.0, _num(obj.get("start")) or 0.0),
            end=_num(obj.get("end")),
        )
    except (TypeError, ValueError):
        return fallback_copy(clip, cta, ad)
