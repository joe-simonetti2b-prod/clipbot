"""Structures de données partagées entre les modules."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class Platform(str, Enum):
    TWITCH = "twitch"
    YOUTUBE = "youtube"
    KICK = "kick"


@dataclass
class StreamCandidate:
    """Un live repéré par la veille."""
    platform: Platform
    channel: str                 # login Twitch / slug Kick / nom de chaîne YouTube
    stream_id: str               # id du stream (Twitch), videoId (YouTube), channel_id (Kick)
    url: str                     # URL lisible par yt-dlp
    title: str
    category: str
    viewers: int
    language: str = ""
    chat_ref: str = ""           # canal IRC / liveChatId YouTube / chatroom Kick
    hls_url: str = ""            # flux vidéo direct (Kick) : lu par FFmpeg sans yt-dlp
    boost: float = 1.0           # priorité (créateurs « focus » : x3 par défaut)

    @property
    def weight(self) -> float:
        """Poids de classement : viewers × priorité du créateur."""
        return self.viewers * self.boost

    @property
    def key(self) -> str:
        return f"{self.platform.value}:{self.stream_id}"


@dataclass
class ChatMessage:
    ts: float                    # horodatage epoch (secondes) à la réception
    author: str
    text: str


@dataclass
class HypeEvent:
    """Pic d'engagement détecté dans le chat."""
    stream_key: str
    peak_ts: float               # instant du pic côté chat (epoch)
    score: float                 # score composite (z-score pondéré)
    msgs_per_s: float
    reason: str                  # ex : "rire", "action", "burst"
    top_tokens: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
