"""
Garde-fou mémoire et CPU pour les petites machines (Render gratuit : 512 Mo, ~0,1 CPU).

Le service était tué par l'hébergeur (« OOM », mémoire dépassée) quand un montage
FFmpeg, un téléchargement yt-dlp et deux captures de live tournaient en même temps.
Trois protections :

  * `heavy` : un seul travail lourd à la fois (montage OU téléchargement de clip) ;
  * `wait_room()` : avant un travail lourd, on attend que la mémoire redescende ;
    si elle ne redescend pas, on déleste un live (moins de captures en parallèle) ;
  * `NICE` : FFmpeg / yt-dlp tournent en priorité basse, pour que le programme
    principal (Telegram, contrôle de santé de Render) reste réactif.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

CGROUP = Path("/sys/fs/cgroup")
NICE = ["nice", "-n", "15"]          # préfixe des commandes lourdes
PRESSURE_S = 15 * 60                  # délestage d'un live pendant 15 min


def niced(cmd: list[str]) -> list[str]:
    """Commande lourde en priorité basse (si `nice` existe sur la machine)."""
    import shutil
    return NICE + cmd if shutil.which("nice") else cmd


def _read_int(p: Path) -> int | None:
    try:
        raw = p.read_text().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def _stat(p: Path) -> dict[str, int]:
    out = {}
    try:
        for line in p.read_text().splitlines():
            k, _, v = line.partition(" ")
            if v.strip().isdigit():
                out[k] = int(v)
    except OSError:
        pass
    return out


def memory_used() -> int | None:
    """Mémoire « réellement » occupée par le conteneur (comme la compte l'hébergeur :
    le cache de fichiers inactif, récupérable, est retiré)."""
    cur = _read_int(CGROUP / "memory.current")                     # cgroup v2
    if cur is not None:
        return max(0, cur - _stat(CGROUP / "memory.stat").get("inactive_file", 0))
    cur = _read_int(CGROUP / "memory" / "memory.usage_in_bytes")    # cgroup v1
    if cur is not None:
        return max(0, cur - _stat(CGROUP / "memory" / "memory.stat").get("total_inactive_file", 0))
    total = 0                                                        # repli : somme des RSS
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    for d in Path("/proc").glob("[0-9]*"):
        try:
            total += int((d / "statm").read_text().split()[1]) * page
        except (OSError, ValueError, IndexError):
            continue
    return total or None


def memory_limit(default_mb: int) -> int:
    lim = _read_int(CGROUP / "memory.max") or _read_int(CGROUP / "memory" / "memory.limit_in_bytes")
    if lim and lim < 1 << 50:           # « illimité » en v1 = nombre gigantesque
        return lim
    return default_mb * 1024 * 1024


class Resources:
    def __init__(self, limit_mb: int = 512, reader=memory_used):
        self.limit = memory_limit(limit_mb)
        self.read = reader
        self.heavy = asyncio.Lock()
        self.pressure_until = 0.0
        self.extra_off_until = 0.0          # 3e live coupé temporairement (manque de place)
        self.on_pressure = lambda: None     # branché : relance la veille (moins de lives)

    def slots(self, wanted: int) -> int:
        """Nombre de lives à suivre. Au-delà de 2, le live en plus n'est gardé que s'il
        reste de la place : dès qu'un montage doit attendre la mémoire, on revient à 2
        pendant 30 min. Sous vraie pression : 1 seul live."""
        if self.under_pressure:
            return 1
        if wanted <= 2:
            return wanted
        now = time.time()
        if now < self.extra_off_until:
            return wanted - 1
        r = self.used_ratio()
        if r is not None and r > 0.70 and not self.heavy.locked():
            self.extra_off_until = now + 1800     # déjà serré sans montage en cours
            return wanted - 1
        return wanted

    def tighten(self, why: str) -> None:
        first = time.time() >= self.extra_off_until
        self.extra_off_until = time.time() + 1800
        if first:
            log.info("Mémoire : %s -> retour à 2 lives pendant 30 min", why)
            self.on_pressure()

    @property
    def under_pressure(self) -> bool:
        return time.time() < self.pressure_until

    def used_ratio(self) -> float | None:
        used = self.read()
        return None if used is None else used / self.limit

    def shed(self, why: str) -> None:
        first = not self.under_pressure
        self.pressure_until = time.time() + PRESSURE_S
        if first:
            log.warning("Mémoire : %s -> un seul live suivi pendant 15 min", why)
            self.on_pressure()

    async def wait_room(self, need_mb: int, max_wait_s: float = 600, poll_s: float = 5) -> None:
        """Attend qu'il reste `need_mb` libres (avec 8 % de marge). Au bout d'un moment,
        déleste un live pour libérer de la place, puis continue quoi qu'il arrive."""
        need = need_mb * 1024 * 1024
        start, shed_done, tight_done = time.time(), False, False
        while True:
            used = self.read()
            if used is None or used + need < self.limit * 0.92:
                return
            waited = time.time() - start
            if waited > 45 and not tight_done:
                self.tighten(f"{used / 2**20:.0f} Mo utilisés, montage en attente")
                tight_done = True
            if waited > 120 and not shed_done:
                self.shed(f"{used / 2**20:.0f} Mo utilisés, montage en attente")
                shed_done = True
            if waited > max_wait_s:
                log.warning("Mémoire toujours haute (%.0f Mo) : on tente quand même", used / 2**20)
                return
            await asyncio.sleep(poll_s)

    async def watchdog(self, every_s: float = 10) -> None:
        """Surveille en continu : au-delà de 88 % on déleste un live avant que
        l'hébergeur ne tue tout le service."""
        while True:
            r = self.used_ratio()
            if r is not None and r > 0.88:
                self.shed(f"{r:.0%} de la mémoire utilisée")
            await asyncio.sleep(every_s)
