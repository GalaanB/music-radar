"""Réglages du bot Telegram, lus dans l'environnement (.env)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])  # seul chat autorisé à piloter le bot
DAILY_TIME = os.getenv("DAILY_TIME", "09:00")
WEEKLY_TIME = os.getenv("WEEKLY_TIME", "09:00")
SEND_EMPTY_DAILY = os.getenv("SEND_EMPTY_DAILY", "0") == "1"
# Jusqu'à ce nombre de sorties, une carte (avec boutons) par sortie ; au-delà,
# liste compacte + /trier.
CARDS_MAX = int(os.getenv("CARDS_MAX", "12"))
# Pochette : "small" = miniature à côté du texte (aperçu de lien Telegram),
# "off" = aucune pochette.
COVERS = os.getenv("COVERS", "small").lower()

# python-telegram-bot : 0 = dimanche ... 5 = vendredi, 6 = samedi.
FRIDAY = 5
ALL_DAYS_BUT_FRIDAY = (0, 1, 2, 3, 4, 6)

SEND_PAUSE_S = 0.4      # Telegram tolère ~1 message/s dans un même chat
RETRY_DELAY_S = 3600    # un récap planifié qui échoue est retenté une fois, 1 h plus tard
