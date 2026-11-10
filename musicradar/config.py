"""Réglages lus dans l'environnement (.env) et constantes du projet."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(os.getenv("RADAR_DATA_DIR", "./data"))
ARTISTS_FILE = Path(os.getenv("ARTISTS_FILE", "./artists.txt"))
COUNTRY = os.getenv("ITUNES_COUNTRY", "fr")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Paris"))

# --- Apple (API iTunes publique) ---
ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
# L'API iTunes tolère environ 20 requêtes/minute (limite non officielle).
# 3,2 s entre deux appels reste sous ce seuil.
REQUEST_DELAY_S = float(os.getenv("ITUNES_DELAY", "3.2"))
HTTP_TIMEOUT_S = 20
# L'API ne trie pas vraiment par date (une sortie de 2023 peut précéder une de
# 2026) : on lit toutes les fiches renvoyées, c'est la même requête.
ALBUMS_PER_ARTIST = 200

# --- Récaps ---
# Le récap quotidien regarde les N derniers jours (hors sorties déjà notifiées) :
# Apple affiche parfois une sortie avec plusieurs jours de retard.
DAILY_LOOKBACK_DAYS = int(os.getenv("DAILY_LOOKBACK_DAYS", "14"))
# Sorties à venir annoncées dans le récap du vendredi.
UPCOMING_DAYS = int(os.getenv("UPCOMING_DAYS", "7"))
UPCOMING_MAX_LINES = int(os.getenv("UPCOMING_MAX", "8"))
TELEGRAM_MAX_LEN = 4000  # limite Telegram = 4096, petite marge de sécurité

# --- Dédoublonnage ---
# Deux fiches Apple de même artiste, même type et même titre sont la même
# sortie (explicite / clean) si leurs dates sont à moins de N jours d'écart.
# Au-delà, ce sont deux sorties homonymes (un 2e « Freestyle » deux ans après).
TWIN_MAX_DAYS = 30

# --- Analyse de nouveauté (1 appel Apple de plus par album/EP analysé) ---
NOVELTY_ENABLED = os.getenv("NOVELTY", "1") == "1"

# --- Featurings sur le projet d'un autre artiste ---
FEATURES_ENABLED = os.getenv("FEATURES", "1") == "1"
# MusicBrainz (base musicale ouverte) relie les invités par identifiant, pas par
# nom : source principale des featurings. MUSICBRAINZ=0 la désactive.
MUSICBRAINZ_ENABLED = os.getenv("MUSICBRAINZ", "1") == "1"
MB_API = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "MusicRadar/1.0 ( https://github.com/GalaanB/music-radar )"}
MB_DELAY_S = 1.1  # MusicBrainz : 1 requête par seconde au maximum
# Vérification « nom porté par un seul artiste » et lien MusicBrainz : refaits
# au bout de ce délai quand ils ont échoué.
NAME_RECHECK_DAYS = 30

# --- Pannes ---
# Au-delà de ce nombre d'échecs d'affilée, le service est considéré en panne :
# Apple -> le scan s'arrête (il sera refait au prochain récap, qui couvre 14
# jours) ; MusicBrainz -> recherche Apple pour la suite du scan.
APPLE_MAX_FAILURES = 5
MB_MAX_FAILURES = 3

# --- Sauvegardes de la base (data/backups/) ---
BACKUPS_KEPT = 7

# Un artiste dont le genre Apple Music ne contient aucun de ces mots est
# signalé "à vérifier" (ex. un groupe de rock qui porte le même nom qu'un
# rappeur). Ça ne bloque rien, ça alerte juste.
EXPECTED_GENRE_HINTS = ("hip", "rap", "r&b", "soul", "pop", "trap", "urban")
