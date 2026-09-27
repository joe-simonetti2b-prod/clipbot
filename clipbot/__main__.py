"""
Point d'entrée.

    python -m clipbot                 # service complet (ce que lance le conteneur)
    python -m clipbot --check         # vérifie la configuration et les outils, puis quitte
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil


def _load_dotenv(path: str = ".env") -> None:
    """Chargeur .env minimal pour les tests locaux (en production, les variables viennent de l'hébergeur)."""
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.split(" #", 1)[0]  # commentaire en fin de ligne
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
    except FileNotFoundError:
        pass


def _check() -> int:
    from .config import Settings
    s = Settings()
    ok = True
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        found = shutil.which(tool)
        print(f"{'✅' if found else '❌'} {tool}")
        ok &= bool(found)
    for label, val in [
        ("TELEGRAM_BOT_TOKEN", s.publish.telegram_token),
        ("TELEGRAM_PAIR_CODE", s.publish.telegram_pair_code),
    ]:
        print(f"{'✅' if val else '❌'} {label} (obligatoire)")
        ok &= bool(val)
    for label, val in [
        ("TWITCH_CLIENT_ID", s.discovery.twitch_client_id),
        ("TIKTOK_CLIENT_KEY", s.publish.tiktok_client_key),
        ("ANTHROPIC_API_KEY", s.processing.anthropic_api_key),
        ("domaine public", s.server.public_url),
    ]:
        print(f"{'✅' if val else '➖'} {label} (optionnel)")
    return 0 if ok else 1


def main() -> None:
    _load_dotenv()
    ap = argparse.ArgumentParser(prog="clipbot")
    ap.add_argument("--check", action="store_true", help="vérifier la configuration")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Le healthcheck Render appelle /health toutes les 5 s : inutile d'en noyer les logs
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if args.check:
        raise SystemExit(_check())

    from .app import App
    from .config import Settings
    asyncio.run(App(Settings()).run())


if __name__ == "__main__":
    main()
