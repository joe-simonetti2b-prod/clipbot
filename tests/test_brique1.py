"""
Tests hors-ligne : aucun appel réseau.
  * détecteur de hype sur un chat simulé (bruit de fond + pic)
  * extraction d'un clip depuis un tampon de segments générés par ffmpeg
"""
import asyncio
import random
import subprocess
import time
from datetime import datetime

import pytest

from clipbot.config import CaptureConfig, HypeConfig
from clipbot.hype import HypeDetector
from clipbot.models import ChatMessage, HypeEvent, Platform, StreamCandidate
from clipbot.recorder import ClipExtractor, StreamRecorder, SEG_FMT

NORMAL = ["salut", "gg", "t'as vu le match hier", "quel jeu c'est", "bonsoir le chat",
          "il est fort", "ah ouais", "on attend quoi", "!discord", "ok"]


def simulate(detector, t0, seconds, rate, texts, authors=500, rng=None):
    rng = rng or random.Random(0)
    events = []
    for s in range(seconds):
        now = t0 + s
        for _ in range(rng.randint(max(0, int(rate) - 2), int(rate) + 2)):
            detector.add(ChatMessage(now + rng.random(), f"u{rng.randint(1, authors)}",
                                     rng.choice(texts)))
        ev = detector.tick(now + 1)
        if ev:
            events.append(ev)
    return events


def test_no_trigger_on_steady_chat():
    d = HypeDetector("twitch:test", HypeConfig())
    assert simulate(d, 0, 600, 4, NORMAL) == []


def test_laugh_spike_triggers_once_near_peak():
    d = HypeDetector("twitch:test", HypeConfig())
    rng = random.Random(1)
    simulate(d, 0, 180, 4, NORMAL, rng=rng)                                   # fond calme
    ev = simulate(d, 180, 10, 30, ["KEKW", "KEKWWWW", "ptdrrr 😂", "LUL"], rng=rng)  # fou rire
    ev += simulate(d, 190, 60, 4, NORMAL, rng=rng)                            # retour au calme
    assert len(ev) == 1
    assert ev[0].reason == "rire"
    assert 180 <= ev[0].peak_ts <= 195


def test_single_spammer_is_ignored():
    d = HypeDetector("twitch:test", HypeConfig())
    rng = random.Random(2)
    simulate(d, 0, 180, 4, NORMAL, rng=rng)
    ev = simulate(d, 180, 10, 30, ["POG"], authors=1, rng=rng)  # un seul auteur
    assert ev == []


def test_goal_is_classified_as_action():
    d = HypeDetector("youtube:test", HypeConfig())
    rng = random.Random(3)
    simulate(d, 0, 180, 6, NORMAL, rng=rng)
    ev = simulate(d, 180, 8, 40, ["BUUUUT", "GOAL 🔥", "QUEL BUT", "CLIP IT"], rng=rng)
    ev += simulate(d, 188, 40, 6, NORMAL, rng=rng)
    assert len(ev) == 1 and ev[0].reason == "action"


def test_clip_extraction_from_ring_buffer(tmp_path):
    cap = CaptureConfig(work_dir=tmp_path, segment_s=2, reencode=True)
    hype = HypeConfig(chat_delay_s=0, pre_roll_s=3, post_roll_s=3)
    c = StreamCandidate(Platform.TWITCH, "demo", "1", "https://x", "", "", 0)
    rec = StreamRecorder(c, cap)

    # Fabrique 6 segments de 2 s horodatés dans le passé (= tampon rempli)
    start = int(time.time()) - 30
    for i in range(6):
        name = datetime.fromtimestamp(start + 2 * i).strftime(SEG_FMT)
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-f", "mpegts",
             str(rec.buffer_dir / f"seg_{name}.ts")],
            check=True,
        )
    ev = HypeEvent("twitch:1", peak_ts=start + 6, score=5, msgs_per_s=20, reason="rire")
    raw = asyncio.run(ClipExtractor(cap, hype).extract(rec, ev))
    out = raw.path

    assert out and out.exists() and out.with_suffix(".json").exists()
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True).stdout)
    assert dur == pytest.approx(6, abs=0.3)
