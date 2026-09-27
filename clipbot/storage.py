"""
État persistant (SQLite dans /data : disque persistant sur Oracle, éphémère sur Render).

Deux tables :
  * clips : un enregistrement par clip, avec son statut dans le pipeline
            extracted -> processing -> ready -> approved -> publishing
            -> published | rejected | discarded | failed
  * kv    : réglages modifiables à chaud (propriétaire Telegram, mode auto,
            pause, chaînes ajoutées, jetons TikTok…)

Accès synchrone volontaire : volumes minuscules (quelques dizaines de
requêtes/heure), SQLite en WAL répond en < 1 ms.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created       REAL NOT NULL,
    platform      TEXT, channel TEXT, stream_url TEXT, stream_title TEXT, category TEXT,
    reason        TEXT, score REAL, tokens TEXT,
    raw_path      TEXT, final_path TEXT,
    trim_offset   REAL DEFAULT 0, trim_duration REAL,
    transcript    TEXT, hook TEXT, caption TEXT, ai_score INTEGER,
    status        TEXT NOT NULL,
    tg_message_id INTEGER,
    publish_id    TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # Au redémarrage, un clip resté « en cours » est remis dans la file.
        self.db.execute("UPDATE clips SET status='extracted' WHERE status='processing'")
        self.db.execute("UPDATE clips SET status='approved' WHERE status='publishing'")

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    on_set = None  # rappel optionnel (sauvegarde Telegram des réglages importants)

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        if self.on_set:
            self.on_set(key)

    # --------------------------------------------------------------- clips
    def add_clip(self, **fields) -> int:
        fields.setdefault("created", time.time())
        fields.setdefault("status", "extracted")
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.db.execute(f"INSERT INTO clips({cols}) VALUES({marks})", tuple(fields.values()))
        return cur.lastrowid

    def update_clip(self, clip_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE clips SET {sets} WHERE id=?", (*fields.values(), clip_id))

    def clip(self, clip_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()

    def next_clip(self, status: str, avoid_channel: str | None = None) -> sqlite3.Row | None:
        """Meilleur clip en attente ; si possible d'un autre créateur que `avoid_channel`
        (alternance : chaque créateur suivi a ses clips)."""
        return self.db.execute(
            "SELECT * FROM clips WHERE status=? ORDER BY (channel IS ? ) ASC, score DESC, created "
            "LIMIT 1", (status, avoid_channel)
        ).fetchone()

    def overflow(self, status: str, keep: int) -> list[sqlite3.Row]:
        """Clips d'un statut au-delà des `keep` meilleurs (par score de hype)."""
        return self.db.execute(
            "SELECT * FROM clips WHERE status=? ORDER BY score DESC, created DESC LIMIT -1 OFFSET ?",
            (status, keep),
        ).fetchall()

    def claim(self, clip_id: int, expected: str, new: str) -> bool:
        """Transition atomique de statut (évite un double traitement)."""
        cur = self.db.execute(
            "UPDATE clips SET status=? WHERE id=? AND status=?", (new, clip_id, expected)
        )
        return cur.rowcount == 1

    def counts_since(self, since: float) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) n FROM clips WHERE created>=? GROUP BY status", (since,)
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def expired(self, before: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM clips WHERE created<? AND status IN "
            "('published','rejected','discarded','failed') "
            "AND (raw_path IS NOT NULL OR final_path IS NOT NULL)",
            (before,),
        ).fetchall()
