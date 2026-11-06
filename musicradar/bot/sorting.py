"""Tri des sorties : boutons sous chaque carte du récap, et file /trier."""

from __future__ import annotations

import asyncio

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .. import formatting, storage
from ..models import STATE_ADDED, STATE_IGNORED, STATE_NEW
from . import ui

CARD_ACTIONS = {
    "l": (STATE_ADDED, "➕ Ajoutée à ta playlist"),
    "i": (STATE_IGNORED, "🙈 Ignorée"),
    "u": (STATE_NEW, ""),
}
# Bouton de la file /trier -> nouvel état de la sortie affichée ("S" = Plus tard).
QUEUE_ACTIONS = {"L": STATE_ADDED, "I": STATE_IGNORED}


async def on_card_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boutons d'une carte du récap : r:<l|i|u>:<album_id>."""
    q = update.callback_query
    if not ui.authorized(q):
        await ui.answer(q)
        return
    try:
        _, action, sid = q.data.split(":")
        state, label = CARD_ACTIONS[action]
        album_id = int(sid)
    except (ValueError, KeyError):
        await ui.answer(q)
        return

    await asyncio.to_thread(storage.set_state, [album_id], state)
    r = await asyncio.to_thread(storage.get_release, album_id)
    if r is None:
        await ui.answer(q, "Sortie introuvable")
        return
    if state == STATE_NEW:
        await ui.edit(q, formatting.card_text(r), ui.card_keyboard(r), ui.cover_preview(r))
    else:
        await ui.edit(q, f"{formatting.card_text(r)}\n{label}", ui.undo_keyboard(r))
    await ui.answer(q)


async def _queue_view(after_id: int) -> tuple[str, InlineKeyboardMarkup | None, LinkPreviewOptions]:
    nxt, total = await asyncio.to_thread(storage.next_in_queue, STATE_NEW, after_id)
    if nxt is None:
        left = await asyncio.to_thread(storage.count_state, STATE_NEW)
        text = "🎉 File terminée."
        if left:
            text += f"\nIl en reste {left} (passées avec « Plus tard ») : /trier pour y revenir."
        return text, None, ui.NO_PREVIEW
    return f"{formatting.card_text(nxt)}\n<i>{total} à trier</i>", ui.queue_keyboard(nxt), ui.cover_preview(nxt)


async def on_queue_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boutons de la file : q:n:<L|I|S>:<album_id affiché>. Les anciens q:l:… sont ignorés."""
    q = update.callback_query
    if not ui.authorized(q):
        await ui.answer(q)
        return
    try:
        _, queue, action, scur = q.data.split(":")
        cur = int(scur)
    except ValueError:
        await ui.answer(q)
        return
    if queue != "n":
        await ui.answer(q)
        return

    new_state = QUEUE_ACTIONS.get(action)
    if new_state and cur:
        await asyncio.to_thread(storage.set_state, [cur], new_state)
    text, markup, preview = await _queue_view(cur)
    await ui.edit(q, text, markup, preview)
    await ui.answer(q)


async def cmd_trier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup, preview = await _queue_view(0)
    if markup is None:
        text = "🎉 Rien à trier."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=preview)
