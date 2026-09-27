"""
Brique 1d — Capture du flux et extraction des clips.

Pourquoi un tampon circulaire plutôt que « télécharger après coup » ?
Quand le chat explose, le moment viral est déjà passé de 5 à 15 s. Les lives
Twitch/Kick n'offrent qu'un DVR limité et yt-dlp ne sait pas « revenir en
arrière » de façon fiable. On enregistre donc en continu :

    yt-dlp (lit le HLS, gère tokens/pubs)  --pipe-->  ffmpeg -f segment
                                                     (copie sans ré-encodage,
                                                      segments de 6 s horodatés)

et un janitor efface tout ce qui dépasse `buffer_s`. Coût CPU quasi nul.
Au déclenchement, on assemble les segments couvrant [pic - pre_roll,
pic + post_roll] puis on coupe précisément.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import CaptureConfig, HypeConfig
from .models import HypeEvent, StreamCandidate

log = logging.getLogger(__name__)

SEG_RE = re.compile(r"seg_(\d{8}-\d{6})\.ts$")
SEG_FMT = "%Y%m%d-%H%M%S"


def _seg_start(p: Path) -> float | None:
    m = SEG_RE.search(p.name)
    return datetime.strptime(m.group(1), SEG_FMT).timestamp() if m else None


def _safe(s: str) -> str:
    return re.sub(r"[^\w-]+", "_", s)[:40]


class StreamRecorder:
    """Enregistre un live en segments dans un dossier tampon, avec auto-relance."""

    def __init__(self, candidate: StreamCandidate, cfg: CaptureConfig):
        self.c = candidate
        self.cfg = cfg
        self.buffer_dir = cfg.work_dir / "buffer" / _safe(candidate.key)
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self._procs: list[asyncio.subprocess.Process] = []
        self._stopping = False
        self.alive = asyncio.Event()

    # ------------------------------------------------------------ pipeline
    async def _spawn(self) -> None:
        r_fd, w_fd = os.pipe()
        ytdlp = await asyncio.create_subprocess_exec(
            "yt-dlp", "--quiet", "--no-warnings", "--no-part",
            "-f", self.cfg.stream_format, "-o", "-", self.c.url,
            stdout=w_fd, stderr=asyncio.subprocess.PIPE,
        )
        ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(self.cfg.segment_s),
            "-segment_format", "mpegts", "-reset_timestamps", "1",
            "-strftime", "1", str(self.buffer_dir / "seg_%Y%m%d-%H%M%S.ts"),
            stdin=r_fd, stderr=asyncio.subprocess.PIPE,
        )
        os.close(r_fd)
        os.close(w_fd)
        self._procs = [ytdlp, ffmpeg]

    async def run(self) -> None:
        """Boucle de supervision : relance en cas de coupure, abandonne si le live est fini."""
        failures = 0
        while not self._stopping and failures < 5:
            started = time.time()
            await self._spawn()
            self.alive.set()
            ytdlp, ffmpeg = self._procs
            await ffmpeg.wait()
            await self._kill()
            self.alive.clear()
            if self._stopping:
                break
            err = (await ytdlp.stderr.read()).decode(errors="ignore").strip()[-300:]
            # Une coupure après une longue capture = token expiré ou micro-coupure.
            failures = 0 if time.time() - started > 120 else failures + 1
            log.warning("Capture %s interrompue (%s) — tentative %d/5",
                        self.c.key, err or "fin de flux", failures)
            await asyncio.sleep(min(5 * failures, 30))
        log.info("Capture %s terminée", self.c.key)

    async def janitor(self) -> None:
        """Supprime les segments plus vieux que la profondeur du tampon."""
        while not self._stopping:
            limit = time.time() - self.cfg.buffer_s
            for p in self.buffer_dir.glob("seg_*.ts"):
                ts = _seg_start(p)
                if ts is not None and ts < limit:
                    p.unlink(missing_ok=True)
            await asyncio.sleep(self.cfg.segment_s)

    async def stop(self) -> None:
        self._stopping = True
        await self._kill()
        shutil.rmtree(self.buffer_dir, ignore_errors=True)

    async def _kill(self) -> None:
        for p in self._procs:
            if p.returncode is None:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except asyncio.TimeoutError:
                    p.kill()


@dataclass
class RawClip:
    path: Path
    offset: float      # début utile dans le fichier (s)
    duration: float    # durée utile (s)


class ClipExtractor:
    """Transforme un HypeEvent en fichier .mp4 + métadonnées JSON."""

    def __init__(self, cfg: CaptureConfig, hype: HypeConfig):
        self.cfg = cfg
        self.hype = hype
        self.out_dir = cfg.work_dir / "clips"
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def window(self, ev: HypeEvent) -> tuple[float, float]:
        # Le pic du chat est en retard sur l'image : on recale.
        moment = ev.peak_ts - self.hype.chat_delay_s
        return moment - self.hype.pre_roll_s, moment + self.hype.post_roll_s

    async def extract(self, rec: StreamRecorder, ev: HypeEvent) -> RawClip | None:
        t0, t1 = self.window(ev)
        # Attendre que les segments couvrant la fin du clip soient écrits.
        wait = t1 + self.cfg.segment_s + 2 - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

        segs = sorted(
            (ts, p) for p in rec.buffer_dir.glob("seg_*.ts")
            if (ts := _seg_start(p)) is not None
        )
        # Un segment est utile s'il chevauche [t0, t1]. Sa fin = début du suivant.
        chosen = []
        for i, (ts, p) in enumerate(segs):
            end = segs[i + 1][0] if i + 1 < len(segs) else ts + self.cfg.segment_s
            if end > t0 and ts < t1:
                chosen.append((ts, p))
        if not chosen:
            log.error("Aucun segment pour l'événement %s (tampon vide ?)", ev.stream_key)
            return None

        first_ts = chosen[0][0]
        offset = max(0.0, t0 - first_ts)
        duration = t1 - max(t0, first_ts)

        stamp = datetime.fromtimestamp(ev.peak_ts).strftime(SEG_FMT)
        base = self.out_dir / f"{_safe(rec.c.platform.value)}_{_safe(rec.c.channel)}_{stamp}_{ev.reason}"
        concat_list = base.with_suffix(".txt")
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for _, p in chosen))

        if self.cfg.reencode:
            # Coupe précise ici (mode autonome, sans étage de montage)
            out = base.with_suffix(".mp4")
            args = ["-ss", f"{offset:.2f}", "-t", f"{duration:.2f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
            trim = RawClip(out, 0.0, duration)
        else:
            # Copie brute des segments (≈0 CPU) ; la coupe précise est faite
            # par le montage 9:16 qui ré-encode de toute façon.
            out = base.with_suffix(".ts")
            args = ["-c", "copy"]
            trim = RawClip(out, offset, duration)

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", str(concat_list), *args, str(out)]
        proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        concat_list.unlink(missing_ok=True)
        if proc.returncode != 0:
            log.error("ffmpeg a échoué : %s", err.decode(errors="ignore")[-300:])
            return None

        meta = {
            "event": ev.to_dict(),
            "stream": {**asdict(rec.c), "platform": rec.c.platform.value},
            "clip": {"start_epoch": t0, "end_epoch": t1, "duration_s": round(duration, 2),
                     "trim_offset": trim.offset},
        }
        base.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        log.info("Clip extrait : %s (%.0fs, %s, score %.1f)", out.name, duration, ev.reason, ev.score)
        return trim
