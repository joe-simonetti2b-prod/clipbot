"""
Choix automatique de la mise en page verticale 9:16.

On échantillonne ~1 image/s et on cherche des visages (cascade Haar
d'OpenCV : rapide sur CPU, aucun modèle à télécharger). Trois cas :

  face  : un visage occupe une bonne partie de l'image (IRL, Just Chatting,
          interview, réaction) -> recadrage 9:16 centré sur le visage.
  split : petit visage fixe dans un coin (facecam de gameplay)
          -> facecam en haut, jeu en bas : le format qui performe le mieux.
  blur  : pas de visage exploitable (match, gameplay sans cam)
          -> image entière au centre sur fond flouté, rien n'est coupé.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Layout:
    kind: str                      # "face" | "split" | "blur"
    cx: float = 0.5                # centre horizontal du visage (0..1) — mode face
    cam: tuple[float, float, float, float] | None = None  # x, y, w, h (0..1) — mode split


MAX_SAMPLES = 16
SAMPLE_W = 640


def keyframes_gray(src: Path, offset: float, duration: float) -> list:
    """Images clés du passage, en niveaux de gris 640 px de large.

    FFmpeg ne décode QUE les images clés (-skip_frame nokey) : quelques dizaines
    d'images au lieu de ~2 000, soit ~50× moins de calcul qu'un décodage complet.
    (L'ancienne méthode, un « seek » OpenCV par seconde, prenait 6 min sur 0,15 CPU.)
    """
    import numpy as np

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(src)], capture_output=True, text=True, check=True).stdout.strip()
    w, h = (int(v) for v in probe.split("\n")[0].split("x")[:2])
    fh = max(2, round(SAMPLE_W * h / w / 2) * 2)
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "1",
         "-skip_frame", "nokey", "-ss", f"{offset:.2f}", "-t", f"{duration:.2f}", "-i", str(src),
         "-vf", f"scale={SAMPLE_W}:{fh},format=gray", "-fps_mode", "passthrough",
         "-an", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout
    size = SAMPLE_W * fh
    frames = [np.frombuffer(raw, np.uint8, size, i * size).reshape(fh, SAMPLE_W)
              for i in range(len(raw) // size)]
    if len(frames) > MAX_SAMPLES:  # échantillon régulier
        step = len(frames) / MAX_SAMPLES
        frames = [frames[int(i * step)] for i in range(MAX_SAMPLES)]
    return frames


def _analyze_sync(src: Path, offset: float, duration: float) -> Layout:
    import cv2

    cv2.setNumThreads(1)  # machine gratuite : pas de pool de fils gourmand en RAM
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces: list[tuple[float, float, float, float]] = []
    frames = keyframes_gray(src, offset, duration)
    samples = len(frames)
    for gray in frames:
        gray = cv2.equalizeHist(gray)
        found = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(24, 24))
        if len(found):
            x, y, fw, fh = max(found, key=lambda r: r[2] * r[3])
            sh, sw = gray.shape
            faces.append((x / sw, y / sh, fw / sw, fh / sh))

    if samples == 0 or len(faces) / samples < 0.5:
        return Layout("blur")

    cxs = [f[0] + f[2] / 2 for f in faces]
    cys = [f[1] + f[3] / 2 for f in faces]
    fws = [f[2] for f in faces]
    cx, cy, fw = statistics.median(cxs), statistics.median(cys), statistics.median(fws)
    stable = len(faces) > 2 and statistics.pstdev(cxs) < 0.04 and statistics.pstdev(cys) < 0.04
    near_edge = min(cx, 1 - cx) < 0.3 or min(cy, 1 - cy) < 0.3

    if fw >= 0.09:
        return Layout("face", cx=cx)
    if stable and near_edge:
        # La facecam fait ~3,5× la largeur du visage, au format 16:9.
        cw = min(1.0, fw * 3.5)
        ch = cw  # en coordonnées normalisées, une boîte 16:9 dans une image 16:9 a w == h
        x = min(max(cx - cw / 2, 0.0), 1 - cw)
        y = min(max(cy - ch / 2, 0.0), 1 - ch)
        return Layout("split", cam=(x, y, cw, ch))
    return Layout("blur")


async def analyze(src: Path, offset: float, duration: float) -> Layout:
    try:
        layout = await asyncio.to_thread(_analyze_sync, src, offset, duration)
    except Exception as e:
        log.warning("Analyse de cadrage impossible (%s) -> fond flouté", e)
        layout = Layout("blur")
    log.info("Mise en page choisie : %s", layout.kind)
    return layout
