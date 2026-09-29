"""
Montage final vertical 9:16, H.264, son normalisé à -14 LUFS (référence
TikTok/Shorts), sous-titres et hook incrustés — le tout en UNE passe.

Taille de sortie réglable (OUTPUT_HEIGHT) : 1080×1920 par défaut, 720×1280
sur les petites machines gratuites (≈ 2,2× moins de calcul). Les sous-titres
ASS sont écrits pour 1080×1920 ; libass les met à l'échelle tout seul.

Synchronisation son/image (lèvres) : image ET son sont calés sur le MÊME zéro,
l'instant de début du clip (fps start_time=0, aresample first_pts=0). Remettre
chaque piste à zéro séparément (ancienne méthode) décalait le son de l'image dès
que les deux pistes du live ne démarraient pas au même instant.
Fin de vidéo : la dernière image est figée (tpad) pendant l'outro, le son se tait.
Débit plafonné (~4,5 Mb/s) pour rester sous la limite de 50 Mo de Telegram.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .resources import niced

from .layout import Layout

log = logging.getLogger(__name__)


def _even(x: float) -> int:
    return int(round(x / 2)) * 2


PUNCH_ZOOM = 1.12      # zoom « punch-in » sur le moment fort (coupe franche, style TikTok)
PUNCH_S = 1.3


def build_filter(layout: Layout, ass_path: Path, height: int = 1920, outro_s: float = 0.0,
                 punch: tuple[float, float] | None = None) -> str:
    pad = f"tpad=stop_mode=clone:stop_duration={outro_s:.2f}," if outro_s > 0 else ""
    zoom = ""
    if punch:
        # Seules les ~40 images du moment fort sont agrandies (coût quasi nul) puis
        # superposées au reste ; avant et après, l'image est strictement identique.
        W, H = _even(height * 9 / 16), height
        a, b = punch
        zoom = (f"split=2[pa][pb];[pb]trim=start={a:.2f}:end={b:.2f},"
                f"scale=w=trunc(iw*{PUNCH_ZOOM}/2)*2:h=-2,crop={W}:{H}[pz];"
                f"[pa][pz]overlay=eof_action=pass:repeatlast=0,")
    return _layout_filter(layout, ass_path, height).replace(
        "fps=30,", f"fps=30:start_time=0,{pad}{zoom}", 1)


def punch_window(peak_at: float | None, duration: float) -> tuple[float, float] | None:
    if peak_at is None or not (0.6 < peak_at < duration - 0.6):
        return None
    return max(0.0, peak_at - 0.1), min(duration, peak_at + PUNCH_S)


def _layout_filter(layout: Layout, ass_path: Path, H: int) -> str:
    W = _even(H * 9 / 16)
    ass = f"ass=filename='{ass_path.as_posix()}'"
    if layout.kind == "face":
        # Fenêtre 9:16 pleine hauteur, centrée sur le visage, bornée aux bords.
        return (
            f"[0:v]crop=w='ih*9/16':h=ih:x='max(0,min(iw-ow,{layout.cx:.4f}*iw-ow/2))':y=0,"
            f"scale={W}:{H},setsar=1,fps=30,{ass}[v]"
        )
    if layout.kind == "split" and layout.cam:
        x, y, w, h = layout.cam
        top = _even(H / 3)                      # facecam : 1/3 haut, jeu : 2/3 bas
        bot = H - top
        return (
            "[0:v]split=2[c][g];"
            f"[c]crop=w='{w:.4f}*iw':h='{h:.4f}*ih':x='{x:.4f}*iw':y='{y:.4f}*ih',"
            f"scale={W}:{top}:force_original_aspect_ratio=increase,crop={W}:{top}[top];"
            f"[g]crop=w='min(iw,ih*{W}/{bot})':h=ih,scale={W}:{bot}[bot];"
            f"[top][bot]vstack,setsar=1,fps=30,{ass}[v]"
        )
    # blur : image entière (légèrement recadrée en 4:3) sur fond flouté.
    # Le flou est calculé en demi-résolution : 4× moins cher, invisible à l'œil.
    return (
        "[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={W // 2}:{H // 2}:force_original_aspect_ratio=increase,crop={W // 2}:{H // 2},"
        f"gblur=sigma=20,eq=brightness=-0.10,scale={W}:{H}[bg];"
        f"[fgsrc]crop=w='min(iw,ih*4/3)':h=ih,scale={W}:-2[fg];"
        f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2,setsar=1,fps=30,{ass}[v]"
    )


async def render(src: Path, offset: float, duration: float, layout: Layout,
                 ass_text: str, out: Path, height: int = 1920, preset: str = "veryfast",
                 threads: int = 2, outro_s: float = 0.0, peak_at: float | None = None) -> bool:
    total = duration + max(outro_s, 0.0)
    punch = punch_window(peak_at, duration)
    afilter = "aresample=async=1:first_pts=0,loudnorm=I=-14:TP=-1.5:LRA=11"
    if outro_s > 0:
        afilter += f",apad=pad_dur={outro_s:.2f}"
    ass_path = out.with_suffix(".ass")
    ass_path.write_text(ass_text, encoding="utf-8")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        # -threads AVANT -i = décodeur : sans ça il ouvre un fil par cœur de l'hôte,
        # chacun gardant des images en mémoire (cause de l'arrêt « mémoire » sur Render).
        *(["-threads", str(threads)] if threads > 0 else []),
        "-ss", f"{offset:.2f}", "-t", f"{duration:.2f}", "-i", str(src),
        *(["-filter_complex_threads", str(threads)] if threads > 0 else []),
        "-filter_complex", build_filter(layout, ass_path, height, outro_s, punch),
        "-map", "[v]", "-map", "0:a:0?",
        "-af", afilter,
        "-t", f"{total:.2f}",  # borne de sortie : durée exacte garantie
        "-c:v", "libx264", "-preset", preset, "-crf", "21",
        *(["-threads", str(threads)] if threads > 0 else []),
        "-maxrate", "4500k", "-bufsize", "9000k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-movflags", "+faststart", str(out),
    ]
    # Priorité basse : sur 0,1 CPU, un montage à pleine priorité affamait le programme
    # principal et Render le redémarrait (contrôle de santé sans réponse en 5 s).
    proc = await asyncio.create_subprocess_exec(*niced(cmd), stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    ass_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        log.error("Montage échoué : %s", err.decode(errors="ignore")[-400:])
        return False
    return True
