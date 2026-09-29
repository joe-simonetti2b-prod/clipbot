"""
Mode esport (/esport) : le bot ne suit plus que l'esport — LoL, VALORANT, Counter-Strike —
en français d'abord, international ensuite.

Trois familles de chaînes :
  * EVENT      : diffusions officielles et gros co-streams de compétition (OTP, Caedrel,
                 LEC, VCT, ESL…) — suivies dès qu'elles sont en live sur un jeu esport ;
  * MATCH_ONLY : créateurs qui ne font de l'esport que pendant les matchs (Kameto,
                 Gotaga…) — suivis UNIQUEMENT quand le titre annonce un match ;
  * PRO        : joueurs / streamers compétitifs — suivis quand ils jouent à un jeu esport.

Une chaîne introuvable est simplement ignorée (vérification automatique au démarrage),
et le filtre par jeu écarte tout homonyme qui streamerait autre chose.
"""
from __future__ import annotations

import re

from .models import StreamCandidate

GAMES = {"league of legends", "valorant", "counter-strike", "counter-strike 2"}

# (login Twitch, français ?)
EVENT = [
    ("otplol_", True), ("otp", True), ("ogaminglol", True), ("valorant_fr", True),
    ("karminecorp", True), ("gentlemates", True), ("teamvitality", True),
    ("caedrel", False), ("riotgames", False), ("lec", False), ("lck", False),
    ("valorant", False), ("valorant_emea", False), ("eslcs", False),
    ("blastpremier", False), ("pgl", False),
]
MATCH_ONLY = [("kamet0", True), ("gotaga", True), ("squeezie", True)]
PRO = [
    # League of Legends — FR (les plus regardés du mois), puis international
    ("traytonlol", True), ("sardoche", True), ("skyyart", True), ("hiro_llol", True),
    ("wakzlol", True), ("gobgg", True), ("splinter", True), ("nuclol", True),
    ("jezu_lol", True), ("ogk_decoy", True), ("slipix", True), ("chap_gg", True),
    ("fakemonster", True), ("saken_lol", True), ("solary", True),
    ("thebausffs", False), ("agurin", False), ("loltyler1", False), ("rekkles", False),
    # VALORANT
    ("scream", True), ("nivera", True), ("tenz", False), ("tarik", False),
    # Counter-Strike
    ("moman", True), ("mrbboy45", True), ("brawks", True), ("fuury_off", True),
    ("shoxiejesuss", True), ("croissantstrike", True), ("kyojinnn", True),
    ("ohnepixel", False), ("s1mple", False),
]

ALL = {n: fr for n, fr in EVENT + MATCH_ONLY + PRO}
FRENCH = {n for n, fr in ALL.items() if fr}
EVENT_NAMES = {n for n, _ in EVENT}
MATCH_ONLY_NAMES = {n for n, _ in MATCH_ONLY}

MATCH_RE = re.compile(
    r"(?<![a-z])(vs\.?|versus|match|bo[1357]|lec|lfl|lck|lpl|worlds|msi|emea|vct|masters|"
    r"champions|major|blast|esl|iem|pgl|finale?s?|playoffs?|quarts?|demi|co-?stream|"
    r"watch ?party|kc|karmine|m8|gentle ?mates|vitality|g2|fnatic|game \d)(?![a-z])", re.I)

FR_BOOST = 2.0        # le compte est français : un live FR vaut 2× son audience
MATCH_BOOST = 1.5     # un vrai match (titre) passe devant une partie classée


def specs() -> list[str]:
    """Liste passée à la veille (plateforme forcée : pas de recherche d'homonymes sur Kick)."""
    return [f"twitch:{n}" for n in ALL]


def is_match(c: StreamCandidate) -> bool:
    return bool(MATCH_RE.search(c.title or ""))


def accepts(c: StreamCandidate) -> bool:
    """Ce live est-il de l'esport à clipper ?"""
    name = c.channel.lower()
    if name not in ALL or (c.category or "").strip().lower() not in GAMES:
        return False
    return is_match(c) if name in MATCH_ONLY_NAMES else True


def boost(c: StreamCandidate) -> float:
    name = c.channel.lower()
    b = FR_BOOST if name in FRENCH else 1.0
    if name in EVENT_NAMES or is_match(c):
        b *= MATCH_BOOST
    return b


def top_clip_logins() -> list[str]:
    """Chaînes dont on récupère les meilleurs clips du jour (officielles et FR d'abord)."""
    order = [n for n, _ in EVENT if n in FRENCH] + [n for n, _ in MATCH_ONLY] + \
            [n for n, _ in PRO if n in FRENCH] + [n for n, _ in EVENT if n not in FRENCH]
    return list(dict.fromkeys(order))


STYLE = """
Ce compte est un compte ESPORT francophone (LoL, VALORANT, Counter-Strike).
- L'accroche et le titre nomment l'enjeu concret : équipe, joueur, score, tournoi
  (ex : « KC ÉLIMINE G2 », « 1V4 POUR LE MATCH », « PENTAKILL EN FINALE »).
- Vocabulaire de la communauté (outplay, clutch, ace, pentakill, baron steal, ff15…),
  jamais d'explication condescendante.
- Hashtags : jeu + compétition + équipes réelles citées (#leagueoflegends #lol #lec
  #karminecorp #valorant #vct #cs2…), plus #esport."""
