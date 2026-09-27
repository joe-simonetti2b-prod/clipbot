"""
Traduction des sous-titres (ex. streamer US -> sous-titres français).

C'est un levier fort pour un compte français : le public FR ne suit pas un
live anglais, mais regarde volontiers un moment fort sous-titré — et un clip
traduit est nettement plus « transformé » qu'une simple rediffusion.

Méthode : on regroupe les mots en phrases, on traduit toutes les phrases en un
seul appel IA, puis on répartit les mots traduits sur la durée de chaque phrase
(au prorata de leur longueur). Le rendu karaoké fonctionne ensuite à l'identique.
"""
from __future__ import annotations

import json
import logging

import aiohttp

from .llm import LLM, ask_json
from .transcribe import Word

log = logging.getLogger(__name__)

NAMES = {"fr": "français", "en": "anglais", "es": "espagnol", "de": "allemand",
         "it": "italien", "pt": "portugais"}


def sentences(words: list[Word], max_gap: float = 0.8, max_words: int = 14) -> list[list[Word]]:
    out: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (w.start - cur[-1].end > max_gap or len(cur) >= max_words
                    or cur[-1].text.endswith((".", "!", "?"))):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def spread(text: str, start: float, end: float, limit: float | None = None) -> list[Word]:
    """Répartit les mots d'une phrase traduite sur [start, end] selon leur longueur.
    Une traduction plus longue peut déborder un peu (0,3 s/mot minimum pour rester
    lisible), sans jamais empiéter sur la phrase suivante (limit)."""
    toks = text.split()
    if not toks:
        return []
    total = sum(len(t) + 1 for t in toks)
    span = max(end - start, 0.3 * len(toks))
    if limit is not None:
        span = max(min(span, limit - start), end - start)
    words, t = [], start
    for tok in toks:
        d = span * (len(tok) + 1) / total
        words.append(Word(round(t, 3), round(t + d, 3), tok))
        t += d
    return words


async def translate_words(session: aiohttp.ClientSession, llm: LLM, words: list[Word],
                          source: str, target: str) -> list[Word] | None:
    """Renvoie les mots traduits horodatés, ou None (on garde alors la version originale)."""
    if not (llm.enabled and words and target and source and source != target):
        return None
    sents = sentences(words)
    lines = [" ".join(w.text for w in s) for s in sents]
    numbered = {str(i + 1): line for i, line in enumerate(lines)}
    prompt = (
        f"Traduis ces répliques d'un live ({NAMES.get(source, source)}) en {NAMES.get(target, target)} "
        "naturel et parlé, comme des sous-titres de clip viral : court, punchy, garde l'argot "
        "et les jurons au même niveau, ne traduis pas les pseudos ni les noms propres.\n"
        f"Entrée (objet JSON, une clé par réplique) :\n{json.dumps(numbered, ensure_ascii=False)}\n\n"
        'Réponds {"t": {"1": "...", "2": "...", ...}} avec EXACTEMENT les mêmes clés, '
        "une traduction par clé, sans fusionner ni découper les répliques."
    )
    obj = await ask_json(session, llm, prompt, max_tokens=1500) or {}
    got: dict[int, str] = {}
    if isinstance(obj.get("t"), dict):
        for k, v in obj["t"].items():
            if str(k).isdigit() and str(v).strip():
                got[int(k) - 1] = str(v).strip()
    elif isinstance(obj.get("lines"), list) and len(obj["lines"]) == len(lines):
        got = {i: str(v).strip() for i, v in enumerate(obj["lines"]) if str(v).strip()}
    # Au moins 60 % des répliques traduites, sinon on garde tout l'original
    if len([i for i in got if 0 <= i < len(lines)]) < 0.6 * len(lines):
        log.warning("Traduction inutilisable (%d/%d répliques) -> sous-titres d'origine",
                    len(got), len(lines))
        return None
    result: list[Word] = []
    for i, s in enumerate(sents):
        text = got.get(i)
        if not text:                 # réplique manquante : version originale, timing d'origine
            result += s
            continue
        nxt = sents[i + 1][0].start if i + 1 < len(sents) else None
        result += spread(text, s[0].start, s[-1].end, nxt)
    return result
