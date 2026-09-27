"""
Habillage du clip au format ASS (rendu par libass dans FFmpeg, en une passe, ~0 CPU).

  * Sous-titres « karaoké » : 1 à 3 mots, MAJUSCULES, gros contour, le mot prononcé
    passe en jaune. Calés sur l'horodatage au mot de Whisper, légèrement en avance
    (0,05 s) pour paraître synchrones avec les lèvres, et tenus à l'écran entre deux
    mots (pas de clignotement). Les mots forts choisis par l'IA ressortent en vert.
  * Sous-titres traduits : la traduction ne suit pas le mouvement des lèvres mot à
    mot ; on affiche donc des bouts de phrase entiers, calés sur la phrase d'origine,
    avec une durée minimale de lecture.
  * Miniature : les 1,4 premières secondes affichent un titre géant + le nom du
    créateur — c'est la 1re image, que TikTok prend comme couverture.
  * Accroche (hook) en haut, style natif TikTok, qui apparaît avec un léger rebond.
  * Barre de progression en haut (retient le spectateur), flash au moment fort.
  * Tag du compte en petit, et fin de vidéo avec boutons « S'abonner » / « Partager ».
"""
from __future__ import annotations

import re
import unicodedata

from .transcribe import Word

W, H = 1080, 1920
# Position verticale selon la mise en page (en px sur un canevas 1080×1920)
PLACEMENT = {
    #          sous-titres (alignement, marge)  hook (marge haut, tout le long ?)  titre miniature (y)
    "blur":  {"sub": (2, 430), "hook_mv": 300, "hook_full": True, "cover_y": 780},
    "face":  {"sub": (2, 460), "hook_mv": 170, "hook_full": False, "cover_y": 820},
    "split": {"sub": (8, 690), "hook_mv": 60,  "hook_full": False, "cover_y": 1200},
}
HOOK_S = 3.5          # durée du hook quand il ne reste pas tout le long
COVER_S = 1.4         # durée du titre « miniature »
SUB_LEAD = 0.05       # sous-titres très légèrement en avance = perçus comme synchrones
HOLD_S = 0.5          # un bout de phrase reste affiché jusqu'à 0,5 s après la fin du mot
MIN_CHUNK_S = 0.45    # durée minimale d'affichage d'un bout de phrase
READ_CPS = 17         # vitesse de lecture confortable (caractères / seconde)
BRAND = "&H003C14E6&"   # rouge #E6143C (format ASS : BBGGRR)

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
Style: Tag,{font},40,&H50FFFFFF,&H50FFFFFF,&H90000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,9,40,40,190,1
Style: Cover,{font},124,&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,10,5,5,60,60,0,1
Style: Badge,{font},50,&H00FFFFFF,&H00FFFFFF,{brand},&H00000000,-1,0,0,0,100,100,2,0,3,14,0,5,60,60,0,1
Style: Shape,{font},20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Btn,{font},66,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,5,0,0,0,1
Style: Outro,{font},60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

HIGHLIGHT = r"{\c&H0000FFFF&\fscx114\fscy114\t(0,110,\fscx100\fscy100)}"
EMPHASIS = r"{\c&H0066FF33&}"          # mot fort : vert vif
POP = r"{\fscx90\fscy90\t(0,90,\fscx100\fscy100)}"


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _clean(text: str) -> str:
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return re.sub(r"[\"“”«»,;:]", "", text).strip()


def _norm(word: str) -> str:
    w = unicodedata.normalize("NFD", word.lower())
    return re.sub(r"[^a-z0-9]", "", "".join(ch for ch in w if unicodedata.category(ch) != "Mn"))


def _event(layer: int, start: float, end: float, style: str, text: str) -> str:
    return f"Dialogue: {layer},{_ts(start)},{_ts(end)},{style},,0,0,0,,{text}\n"


def _rect(w: float, h: float, r: float = 0) -> str:
    """Tracé ASS d'un rectangle (coins arrondis si r > 0)."""
    if r <= 0:
        return f"m 0 0 l {w:.0f} 0 {w:.0f} {h:.0f} 0 {h:.0f}"
    return (f"m {r:.0f} 0 l {w - r:.0f} 0 b {w:.0f} 0 {w:.0f} 0 {w:.0f} {r:.0f} "
            f"l {w:.0f} {h - r:.0f} b {w:.0f} {h:.0f} {w:.0f} {h:.0f} {w - r:.0f} {h:.0f} "
            f"l {r:.0f} {h:.0f} b 0 {h:.0f} 0 {h:.0f} 0 {h - r:.0f} l 0 {r:.0f} b 0 0 0 0 {r:.0f} 0")


# ------------------------------------------------------------------ découpage
def chunk_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6,
                max_len: float = 1.5, max_chars: int = 18) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap
                    or w.end - cur[0].start > max_len or chars > max_chars
                    or cur[-1].text.endswith((".", "!", "?"))):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def chunk_times(chunks: list[list[Word]], total: float, min_s: float = MIN_CHUNK_S,
                reading: bool = False) -> list[tuple[float, float]]:
    """(début, fin) d'affichage de chaque bout : un peu en avance sur la voix, tenu
    jusqu'au bout suivant s'il arrive vite, jamais deux bouts à l'écran en même temps."""
    starts = [max(0.0, c[0].start - SUB_LEAD) for c in chunks]
    out = []
    for i, c in enumerate(chunks):
        nxt = starts[i + 1] if i + 1 < len(chunks) else total
        want = max(c[-1].end - SUB_LEAD + HOLD_S, starts[i] + min_s)
        if reading:
            chars = sum(len(w.text) + 1 for w in c)
            want = max(want, starts[i] + chars / READ_CPS)
        out.append((starts[i], max(starts[i] + 0.1, min(want, nxt, total))))
    return out


# ------------------------------------------------------------------ assemblage
def build_ass(words: list[Word], hook: str, layout_kind: str, duration: float,
              font: str = "DejaVu Sans", credit: str = "", *, karaoke: bool = True,
              keywords: list[str] | tuple = (), cover: str = "", creator: str = "",
              watermark: str = "", outro_s: float = 0.0, peak_at: float | None = None,
              progress: bool = True) -> str:
    p = PLACEMENT.get(layout_kind, PLACEMENT["blur"])
    out = [HEADER.format(font=font, sub_align=p["sub"][0], sub_mv=p["sub"][1],
                         hook_mv=p["hook_mv"], brand=BRAND)]
    total = duration + outro_s
    strong = {_norm(k) for kw in keywords for k in str(kw).split() if _norm(k)}

    if credit:
        out.append(_event(2, 0, duration, "Credit", _clean(credit)))

    # Miniature : 1re image = couverture TikTok (titre géant + créateur)
    if cover:
        cover_end = min(COVER_S, duration)
        y = p["cover_y"]
        out.append(_event(6, 0, cover_end, "Cover",
                          rf"{{\pos(540,{y})\fad(0,180)}}{_clean(cover).upper()}"))
        if creator:
            out.append(_event(6, 0, cover_end, "Badge",
                              rf"{{\pos(540,{y - 170})\fad(0,180)}}{_clean(creator).upper()}"))

    if hook:
        end = duration if p["hook_full"] else min(duration, HOOK_S)
        out.append(_event(5, 0, end, "Hook",
                          r"{\fscx88\fscy88\t(0,160,\fscx103\fscy103)\t(160,260,\fscx100\fscy100)}"
                          + _clean(hook)))

    if watermark:
        start = 0.0 if p["hook_full"] else min(duration, HOOK_S)
        if start < duration:
            out.append(_event(4, start, duration, "Tag", _clean(watermark)))

    if progress and duration > 3:
        out.append(_event(7, 0, duration, "Shape",
                          rf"{{\pos(0,0)\1c{BRAND}\alpha&H20&\fscx0"
                          rf"\t(0,{int(duration * 1000)},\fscx100)\p1}}{_rect(W, 12)}{{\p0}}"))

    if peak_at is not None and 0.5 < peak_at < duration - 0.5:
        out.append(_event(8, peak_at, peak_at + 0.3, "Shape",
                          r"{\pos(0,0)\1c&HFFFFFF&\alpha&H70&\t(0,300,\alpha&HFF&)\p1}"
                          + _rect(W, H) + r"{\p0}"))

    # Sous-titres
    def fmt(w: Word, current: bool) -> str:
        t = _clean(w.text).upper()
        if not t:
            return ""
        if current:
            return HIGHLIGHT + t + r"{\r}"
        if _norm(w.text) in strong:
            return EMPHASIS + t + r"{\r}"
        return t

    if karaoke:
        chunks = chunk_words(words)
        for chunk, (c_start, c_end) in zip(chunks, chunk_times(chunks, duration)):
            for i, w in enumerate(chunk):
                start = c_start if i == 0 else max(c_start, w.start - SUB_LEAD)
                end = (max(start + 0.05, chunk[i + 1].start - SUB_LEAD)
                       if i + 1 < len(chunk) else c_end)
                end = min(end, c_end)
                line = " ".join(x for j, ww in enumerate(chunk) if (x := fmt(ww, j == i)))
                if line and end > start:
                    out.append(_event(0, start, end, "Sub", line))
    else:
        # Traduction : bouts de phrase entiers (2 lignes max), durée de lecture garantie
        chunks = chunk_words(words, max_words=5, max_len=2.4, max_chars=26, max_gap=0.8)
        for chunk, (start, end) in zip(chunks, chunk_times(chunks, duration, 0.7, reading=True)):
            line = " ".join(x for w in chunk if (x := fmt(w, False)))
            if line:
                out.append(_event(0, start, end, "Sub", POP + line))

    if outro_s > 0:
        out.append(_outro(duration, total, watermark))
    return "".join(out)


def _outro(t0: float, t1: float, handle: str) -> str:
    """Écran de fin : image figée assombrie, pseudo, boutons S'abonner / Partager."""
    ms = int((t1 - t0) * 1000)
    ev = [_event(10, t0, t1, "Shape",
                 r"{\pos(0,0)\1c&H000000&\alpha&HFF&\t(0,250,\alpha&H50&)\p1}"
                 + _rect(W, H) + r"{\p0}")]
    title = _clean(handle) if handle else "Abonne-toi pour la suite"
    ev.append(_event(11, t0, t1, "Outro",
                     r"{\pos(540,700)\fscx70\fscy70\t(0,200,\fscx100\fscy100)}" + title))
    buttons = [
        (900, "S'ABONNER", BRAND, "&H00FFFFFF&", 120),
        (1080, "PARTAGER  ➦", "&H00FFFFFF&", "&H00000000&", 300),
    ]
    for y, label, bg, fg, delay in buttons:
        pop = (rf"\fscx0\fscy0\t({delay},{delay + 180},\fscx108\fscy108)"
               rf"\t({delay + 180},{delay + 260},\fscx100\fscy100)")
        pulse = (rf"\t({delay + 700},{delay + 850},\fscx106\fscy106)"
                 rf"\t({delay + 850},{delay + 1000},\fscx100\fscy100)") if delay + 1000 < ms else ""
        ev.append(_event(11, t0, t1, "Shape",
                         rf"{{\an5\pos(540,{y})\1c{bg}\bord0\shad0{pop}{pulse}\p1}}"
                         + _rect(640, 130, 40) + r"{\p0}"))
        ev.append(_event(12, t0, t1, "Btn", rf"{{\pos(540,{y})\1c{fg}{pop}{pulse}}}{label}"))
    return "".join(ev)
