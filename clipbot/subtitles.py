"""
Sous-titres « karaoké » au format ASS (rendu par libass dans FFmpeg).

Style viral : 1 à 3 mots à l'écran, en MAJUSCULES, gros contour noir, le mot
prononcé passe en jaune avec un léger effet « pop ». Plus un bandeau
d'accroche (hook) en haut, style natif TikTok (texte noir sur fond blanc).
"""
from __future__ import annotations

import re

from .transcribe import Word

# Position verticale selon la mise en page (en px sur un canevas 1080×1920)
PLACEMENT = {
    #          sous-titres (alignement, marge)   hook (marge haut, durée max)
    "blur":  {"sub": (2, 430), "hook_mv": 300, "hook_full": True},
    "face":  {"sub": (2, 460), "hook_mv": 170, "hook_full": False},
    "split": {"sub": (8, 690), "hook_mv": 60,  "hook_full": False},
}

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,{sub_align},70,70,{sub_mv},1
Style: Credit,{font},38,&H40FFFFFF,&H40FFFFFF,&H80000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,40,40,70,1
Style: Hook,{font},66,&H00000000,&H00000000,&H00FFFFFF,&H00000000,-1,0,0,0,100,100,0,0,3,16,0,8,90,90,{hook_mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

HIGHLIGHT = r"{\c&H0000FFFF&\fscx114\fscy114\t(0,110,\fscx100\fscy100)}"


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return re.sub(r"[\"“”«»,;:]", "", text).strip()


def chunk_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6,
                max_len: float = 1.4) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap
                    or w.end - cur[0].start > max_len
                    or cur[-1].text.endswith((".", "!", "?"))):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: list[Word], hook: str, layout_kind: str, duration: float,
              font: str = "DejaVu Sans", credit: str = "") -> str:
    p = PLACEMENT.get(layout_kind, PLACEMENT["blur"])
    out = [HEADER.format(font=font, sub_align=p["sub"][0], sub_mv=p["sub"][1], hook_mv=p["hook_mv"])]

    if credit:
        out.append(f"Dialogue: 2,{_ts(0)},{_ts(duration)},Credit,,0,0,0,,{_clean(credit)}\n")

    if hook:
        end = duration if p["hook_full"] else min(duration, 3.5)
        out.append(f"Dialogue: 1,{_ts(0)},{_ts(end)},Hook,,0,0,0,,{_clean(hook)}\n")

    for chunk in chunk_words(words):
        texts = [_clean(w.text).upper() for w in chunk]
        for i, w in enumerate(chunk):
            start = chunk[0].start if i == 0 else w.start
            end = chunk[i + 1].start if i + 1 < len(chunk) else max(w.end, start + 0.25)
            line = " ".join(
                f"{HIGHLIGHT}{t}{{\\r}}" if j == i else t for j, t in enumerate(texts) if t
            )
            if line:
                out.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Sub,,0,0,0,,{line}\n")
    return "".join(out)
