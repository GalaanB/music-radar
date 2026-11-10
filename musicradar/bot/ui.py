"""Briques communes : envoyer et modifier des messages, boutons, verrou de scan."""

from __future__ import annotations

import asyncio
import logging
import secrets

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..models import Release
from . import settings

logger = logging.getLogger("bot")

# Un seul scan à la fois : chaque scan fait des centaines d'appels espacés de
# 3 s, deux scans en parallèle dépasseraient la limite de débit d'Apple.
scan_lock = asyncio.Lock()

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

SCAN_WAIT = "⏳ Scan en cours (≈ 5 min avec la recherche des featurings)…"
SCAN_BUSY = "⏳ Un scan est déjà en cours : ta demande démarrera juste après (compte jusqu'à 5 min de plus)."


async def reply_wait(update: Update, text: str = SCAN_WAIT) -> None:
    """Accusé d'une commande longue, qui prévient si un scan occupe déjà le bot."""
    await update.message.reply_text(SCAN_BUSY if scan_lock.locked() else text)


def cover_preview(r: Release | None) -> LinkPreviewOptions:
    """Pochette en petite miniature : l'aperçu du lien pointe sur l'image."""
    if settings.COVERS == "small" and r is not None and r.cover:
        return LinkPreviewOptions(url=r.cover, prefer_small_media=True, show_above_text=False)
    return NO_PREVIEW


async def send(
    bot,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    preview: LinkPreviewOptions = NO_PREVIEW,
    silent: bool = False,
) -> None:
    await bot.send_message(
        chat_id=settings.CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        link_preview_options=preview,
        disable_notification=silent,
    )


async def send_all(bot, texts: list[str], silent: bool = False) -> None:
    for t in texts:
        await send(bot, t, silent=silent)


def mask(text: str) -> str:
    """Le token du bot ne doit jamais apparaître dans un message."""
    return text.replace(settings.TOKEN, "***")


async def notify_error(bot, what: str, exc: Exception, extra: str = "") -> None:
    logger.exception("Erreur pendant %s", what)
    try:
        await bot.send_message(chat_id=settings.CHAT_ID, text=mask(f"⚠️ Erreur pendant {what} : {exc}{extra}"))
    except Exception:
        logger.exception("Impossible de notifier l'erreur sur Telegram")


# --------------------------------------------------------------------------- #
# Boutons
# --------------------------------------------------------------------------- #

def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def card_keyboard(r: Release) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("➕ Ajoutée", f"r:l:{r.album_id}"), btn("🙈 Ignorer", f"r:i:{r.album_id}")]])


def undo_keyboard(r: Release) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("↩️ Annuler", f"r:u:{r.album_id}")]])


def queue_keyboard(r: Release) -> InlineKeyboardMarkup:
    """Boutons de la file /trier (préfixe q:n: conservé pour les anciens messages)."""
    return InlineKeyboardMarkup(
        [
            [btn("➕ Ajoutée", f"q:n:L:{r.album_id}"), btn("🙈 Ignorer", f"q:n:I:{r.album_id}")],
            [btn("⏭ Plus tard", f"q:n:S:{r.album_id}")],
        ]
    )


def authorized(query: CallbackQuery) -> bool:
    return query.message is not None and query.message.chat.id == settings.CHAT_ID


async def answer(query: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """
    Accusé de réception d'un bouton. Telegram le refuse si le bouton a été
    touché il y a trop longtemps ou après un redémarrage : sans gravité, l'action
    elle-même a déjà été faite, donc on l'ignore au lieu de lever une erreur.
    """
    try:
        await query.answer(text, show_alert=show_alert)
    except BadRequest as exc:
        logger.info("Accusé de réception de bouton refusé (%s)", exc)


async def edit(
    query: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None,
    preview: LinkPreviewOptions = NO_PREVIEW,
) -> None:
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=preview)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# --------------------------------------------------------------------------- #
# Données temporaires des boutons (jusqu'au redémarrage)
# --------------------------------------------------------------------------- #

def pending_choices(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault("adds", {})


def remember(context: ContextTypes.DEFAULT_TYPE, data: dict) -> str:
    """Garde en mémoire ce dont un bouton aura besoin ; renvoie un jeton court."""
    store = pending_choices(context)
    while len(store) >= 50:
        store.pop(next(iter(store)))
    token = secrets.token_hex(3)
    store[token] = data
    return token
