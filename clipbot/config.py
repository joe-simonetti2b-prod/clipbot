"""
Configuration centralisée du bot.

Toutes les valeurs sensibles (clés API, secrets) viennent de variables
d'environnement (fichier /opt/clipbot/.env sur Oracle, onglet « Environment » sur Render) — jamais du code.
Les réglages modifiables depuis Telegram (/auto, /pause, chaînes suivies…)
sont stockés en base et priment sur ces valeurs par défaut.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "", cast=str):
    """Champ lu dans l'environnement au moment de l'instanciation."""
    return field(default_factory=lambda: cast(os.getenv(name, default)))


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _parse_tags(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        name, sep, tags = part.partition("=")
        if sep and name.strip() and tags.strip():
            out[name.strip().lower().split(":")[-1]] = tags.strip()
    return out


def _bool(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "oui", "on"}


@dataclass
class DiscoveryConfig:
    twitch_client_id: str = _env("TWITCH_CLIENT_ID")
    twitch_client_secret: str = _env("TWITCH_CLIENT_SECRET")
    youtube_api_key: str = _env("YOUTUBE_API_KEY")
    kick_client_id: str = _env("KICK_CLIENT_ID")
    kick_client_secret: str = _env("KICK_CLIENT_SECRET")

    languages: list[str] = field(default_factory=lambda: _env_list("LANGUAGES", "fr"))
    # Catégories Twitch (game_id) — 518203 = Sports, 509658 = Just Chatting
    twitch_game_ids: list[str] = field(default_factory=lambda: _env_list("TWITCH_GAME_IDS"))
    # Catégories YouTube : 17 = Sports, 20 = Gaming, 24 = Entertainment
    youtube_category_ids: list[str] = field(
        default_factory=lambda: _env_list("YOUTUBE_CATEGORY_IDS", "17,20,24")
    )
    min_viewers: int = _env("MIN_VIEWERS", "1500", int)
    max_concurrent_streams: int = _env("MAX_CONCURRENT_STREAMS", "3", int)
    poll_interval_s: int = _env("DISCOVERY_INTERVAL_S", "300", int)
    # Créateurs prioritaires (ex. focus FR) : leurs viewers comptent FOCUS_BOOST fois
    focus_channels: list[str] = field(default_factory=lambda: _env_list("FOCUS_CHANNELS"))
    focus_boost: float = _env("FOCUS_BOOST", "3", float)
    # Rotation : un live suivi n'est remplacé que par un live nettement plus regardé
    switch_ratio: float = _env("SWITCH_RATIO", "1.3", float)
    min_watch_s: int = _env("MIN_WATCH_S", "600", int)          # durée minimale de suivi
    # Chaînes autorisées. Complétable depuis Telegram (/add, /remove).
    allowed_channels: list[str] = field(default_factory=lambda: _env_list("ALLOWED_CHANNELS"))


@dataclass
class HypeConfig:
    window_s: float = 10.0          # fenêtre glissante de mesure
    baseline_alpha: float = 0.02    # vitesse d'adaptation de la moyenne mobile (EWMA)
    z_threshold: float = 3.0        # écart au bruit de fond pour déclencher
    min_msgs_per_s: float = 1.5     # plancher absolu (évite les faux positifs sur petits chats)
    warmup_s: float = 60.0          # temps d'apprentissage avant tout déclenchement
    cooldown_s: float = 45.0        # délai minimal entre deux clips d'un même live
    # Pics trop faibles ignorés dès la détection (économise CPU et file d'attente)
    min_score: float = _env("MIN_HYPE_SCORE", "5", float)
    chat_delay_s: float = 8.0       # le chat réagit en retard sur l'image
    # LONG_CLIPS=true -> clips de ~65 s (éligibilité aux fonds créateurs > 1 min)
    pre_roll_s: float = field(default_factory=lambda: 45.0 if _bool(os.getenv("LONG_CLIPS", "")) else 25.0)
    post_roll_s: float = field(default_factory=lambda: 20.0 if _bool(os.getenv("LONG_CLIPS", "")) else 15.0)


@dataclass
class CaptureConfig:
    work_dir: Path = _env("CLIPBOT_WORKDIR", "./data", Path)
    segment_s: int = 6              # durée d'un segment du tampon circulaire
    buffer_s: int = 300             # profondeur du tampon (5 min de live gardées)
    stream_format: str = _env("STREAM_FORMAT", "best[height<=1080]/best")
    max_height: int = _env("CAPTURE_MAX_HEIGHT", "720", int)   # qualité max du flux Kick
    reencode: bool = False          # la découpe brute copie ; le montage 9:16 ré-encode ensuite


@dataclass
class ProcessingConfig:
    # Transcription : Groq (gratuit, rapide, 0 RAM locale) si GROQ_API_KEY, sinon Whisper local
    groq_api_key: str = _env("GROQ_API_KEY")
    whisper_model: str = _env("WHISPER_MODEL", "small")      # tiny/base/small : qualité vs CPU
    whisper_threads: int = _env("WHISPER_THREADS", "2", int)
    whisper_keep_loaded: bool = _env("WHISPER_KEEP_LOADED", "false", _bool)
    # Sortie vidéo : 1920 (1080×1920) ou 1280 (720×1280, pour petites machines)
    output_height: int = _env("OUTPUT_HEIGHT", "1920", int)
    x264_preset: str = _env("X264_PRESET", "veryfast")
    # Fils de calcul FFmpeg/OpenCV. Par défaut FFmpeg en lance un par cœur de l'HÔTE
    # (des dizaines sur un cloud mutualisé) -> ~350 Mo de RAM ; 1 fil = ~170 Mo.
    ffmpeg_threads: int = _env("FFMPEG_THREADS", "2", int)
    font: str = _env("SUBTITLE_FONT", "DejaVu Sans")
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY")        # optionnel : textes + tri IA
    anthropic_model: str = _env("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    min_ai_score: int = _env("MIN_AI_SCORE", "5", int)       # clips notés en dessous = jetés
    cta_text: str = _env("CTA_TEXT")                           # ex : "Lien en bio 🔗"
    ad_disclosure: bool = _env("AD_DISCLOSURE", "false", _bool)  # ajoute « Publicité »
    retention_days: int = _env("RETENTION_DAYS", "3", int)
    # File d'attente bornée : au-delà, les clips en attente les moins bons sont abandonnés
    max_backlog: int = _env("MAX_BACKLOG", "3", int)
    # IA gratuite (Groq) pour la note, l'accroche et la légende quand pas de clé Anthropic
    groq_llm_model: str = _env("GROQ_LLM_MODEL", "llama-3.3-70b-versatile")
    # Langue cible : les sous-titres d'un live dans une autre langue sont traduits (vide = non)
    target_lang: str = _env("TARGET_LANG", "")
    # Mentions obligatoires des campagnes de clipping, par chaîne. Format :
    #   CHANNEL_TAGS=xqc=@xqc #xqc #xqcclips; kamet0=@kamet0 #kameto
    channel_tags: dict[str, str] = field(default_factory=lambda: _parse_tags(os.getenv("CHANNEL_TAGS", "")))
    # Crédit discret incrusté en bas de la vidéo (« twitch.tv/xqc ») : attribution visible
    video_credit: bool = _env("VIDEO_CREDIT", "true", _bool)
    # Tag du compte incrusté en petit sur chaque vidéo (ex : @clipclaptrap) ; /tag pour changer
    watermark: str = _env("WATERMARK")
    # Fin de vidéo (~1,8 s) avec boutons « S'abonner » et « Partager » ; /outro on|off
    outro: bool = _env("OUTRO", "true", _bool)
    # Clips postés par les viewers dans le chat + meilleurs clips Twitch du jour
    viewer_clips: bool = _env("VIEWER_CLIPS", "true", _bool)
    top_clips: bool = _env("TOP_CLIPS", "true", _bool)


@dataclass
class PublishConfig:
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_pair_code: str = _env("TELEGRAM_PAIR_CODE")
    # Facultatif : évite de refaire /start sur un hébergeur sans disque persistant
    telegram_owner_id: str = _env("TELEGRAM_OWNER_ID")
    tiktok_client_key: str = _env("TIKTOK_CLIENT_KEY")
    tiktok_client_secret: str = _env("TIKTOK_CLIENT_SECRET")
    # inbox  = brouillon dans l'app TikTok (fonctionne avant l'audit, 5/jour max)
    # direct = publication publique directe (nécessite l'audit TikTok)
    tiktok_mode: str = _env("TIKTOK_MODE", "inbox")
    # YouTube Shorts (Google Cloud → identifiants OAuth « Application Web »)
    youtube_client_id: str = _env("YOUTUBE_CLIENT_ID")
    youtube_client_secret: str = _env("YOUTUBE_CLIENT_SECRET")
    youtube_privacy: str = _env("YOUTUBE_PRIVACY", "public")
    auto_publish: bool = _env("AUTO_PUBLISH", "false", _bool)
    # TikTok n'accepte que 5 brouillons en attente par 24 h : on les dépense aux bonnes
    # heures (heure locale), espacés, pour que chaque notif arrive quand tu peux publier.
    timezone: str = _env("TIMEZONE", "Europe/Paris")
    tiktok_hours: str = _env("TIKTOK_HOURS", "11-23")
    tiktok_gap_min: int = _env("TIKTOK_GAP_MIN", "90", int)
    # Voie manuelle (illimitée) : les bons clips en plus arrivent sur Telegram, prêts à
    # partager vers TikTok avec la légende copiable d'un tap.
    manual_per_day: int = _env("MANUAL_PER_DAY", "10", int)
    manual_min_score: int = _env("MANUAL_MIN_SCORE", "7", int)
    # Au-delà, un moment de live n'intéresse plus personne : pas publié.
    fresh_hours: float = _env("FRESH_HOURS", "30", float)
    # Statistiques du compte + liste des vidéos (missions TikTok, lien auto pour Whop).
    # Si l'app ne les a pas activées dans le portail développeur, la connexion le dit et
    # les coupe elle-même : pas besoin d'y toucher à la main dans ce cas.
    tiktok_analytics: bool = _env("TIKTOK_ANALYTICS", "true", _bool)


@dataclass
class ServerConfig:
    port: int = _env("PORT", "8080", int)
    # Render fournit RENDER_EXTERNAL_URL automatiquement ; ailleurs, PUBLIC_URL
    public_url: str = field(default_factory=lambda: (
        os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    ).rstrip("/"))
    # Render gratuit s'endort après 15 min sans visite : le bot se réveille lui-même
    keepalive: bool = field(default_factory=lambda: _bool(
        os.getenv("KEEPALIVE", "true" if os.getenv("RENDER_EXTERNAL_URL") else "false")))
    contact_email: str = _env("CONTACT_EMAIL")


@dataclass
class Settings:
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    hype: HypeConfig = field(default_factory=HypeConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
