"""
Transcription avec horodatage au mot, deux moteurs :

  * Groq (si GROQ_API_KEY) : whisper-large-v3-turbo dans le cloud, gratuit
    (≈ 8 h d'audio/jour sur l'offre gratuite), très rapide, aucune RAM ni CPU
    consommés localement -> idéal pour les hébergeurs gratuits.
  * Whisper local (faster-whisper int8, CPU) : aucun compte requis.
    Le modèle est libéré après chaque clip, sauf WHISPER_KEEP_LOADED=true.

En cas d'échec de Groq, bascule automatique sur le moteur local.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
LANG_CODES = {"french": "fr", "english": "en", "spanish": "es", "german": "de",
              "italian": "it", "portuguese": "pt"}

_model_cache: dict = {}


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    words: list[Word]
    language: str

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words).strip()


def _extract_audio(src: Path, offset: float, duration: float, dst: Path, codec: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1",
         "-ss", f"{offset:.2f}", "-t", f"{duration:.2f}", "-i", str(src),
         # Même zéro que le montage : la piste vidéo (copiée vers nulle part, ~0 CPU)
         # sert de repère, et le son est complété par du silence s'il démarre après
         # l'image. Sans ça, les mots seraient décalés des lèvres.
         "-map", "0:v:0?", "-c", "copy", "-f", "null", "-",
         "-map", "0:a:0", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "16000",
         *codec, str(dst)],
        check=True,
    )


# ------------------------------------------------------------------ Groq
def parse_groq(payload: dict) -> Transcript:
    words = [Word(float(w["start"]), float(w["end"]), str(w["word"]).strip())
             for w in payload.get("words") or [] if str(w.get("word", "")).strip()]
    lang = str(payload.get("language") or "").lower()
    return Transcript(words, LANG_CODES.get(lang, lang[:2]))


async def _transcribe_groq(session: aiohttp.ClientSession, api_key: str,
                           src: Path, offset: float, duration: float) -> Transcript:
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "audio.flac"   # FLAC : ~4× plus léger que WAV, sans perte
        await asyncio.to_thread(_extract_audio, src, offset, duration, audio, ["-c:a", "flac"])
        form = aiohttp.FormData()
        form.add_field("model", GROQ_MODEL)
        form.add_field("response_format", "verbose_json")
        form.add_field("timestamp_granularities[]", "word")
        form.add_field("file", audio.read_bytes(), filename="audio.flac", content_type="audio/flac")
        async with session.post(GROQ_URL, data=form,
                                headers={"Authorization": f"Bearer {api_key}"},
                                timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status != 200:
                raise RuntimeError(f"Groq HTTP {r.status} : {(await r.text())[:200]}")
            return parse_groq(await r.json())


# ---------------------------------------------------------------- local
def _transcribe_local_sync(src: Path, offset: float, duration: float,
                           model_name: str, threads: int, keep: bool) -> Transcript:
    from faster_whisper import WhisperModel  # import tardif : lourd

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        _extract_audio(src, offset, duration, wav, [])
        model = _model_cache.get(model_name) or WhisperModel(
            model_name, device="cpu", compute_type="int8", cpu_threads=threads)
        try:
            segments, info = model.transcribe(
                str(wav), word_timestamps=True, vad_filter=True, beam_size=1,
            )
            words = [
                Word(w.start, w.end, w.word.strip())
                for seg in segments for w in (seg.words or []) if w.word.strip()
            ]
            return Transcript(words, info.language)
        finally:
            if keep:
                _model_cache[model_name] = model
            else:
                _model_cache.pop(model_name, None)
                del model
                gc.collect()


async def transcribe(src: Path, offset: float, duration: float, model_name: str,
                     threads: int, groq_key: str = "", session: aiohttp.ClientSession | None = None,
                     keep_loaded: bool = False) -> Transcript:
    if groq_key and session is not None:
        try:
            return await _transcribe_groq(session, groq_key, src, offset, duration)
        except Exception as e:
            log.warning("Groq indisponible (%s) -> Whisper local", e)
    try:
        return await asyncio.to_thread(_transcribe_local_sync, src, offset, duration,
                                       model_name, threads, keep_loaded)
    except Exception as e:  # pas de sous-titres plutôt que pas de clip
        log.error("Transcription impossible : %s", e)
        return Transcript([], "")
