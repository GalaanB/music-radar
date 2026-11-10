"""
Récaps : quotidien (tous les jours sauf le vendredi), récap du vendredi,
rattrapage à la demande, et ce qu'il se passe au démarrage du bot.
"""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

from .. import artists, formatting, pipeline, scan, storage
from ..models import Prepared
from . import settings, ui

logger = logging.getLogger("bot.recaps")


async def deliver(bot, title: str, p: Prepared, empty_text: str, always: bool, reminder: bool = False) -> None:
    """
    Envoie un récap : cartes à boutons si peu de sorties, sinon liste + /trier.
    Une seule notification par récap : l'en-tête sonne, la suite arrive en silence.
    `reminder` ajoute le nombre de sorties reçues plus tôt et encore à trier.
    """
    if not (p.to_show or p.nothing_new or p.upcoming) and not always:
        return

    n = len(p.to_show)
    sorted_note = f" ({p.already_sorted} déjà triée(s))" if p.already_sorted else ""

    if n == 0:
        await ui.send(bot, f"<b>{html.escape(title)}</b>\n\n{html.escape(empty_text)}{sorted_note}")
    elif n <= settings.CARDS_MAX:
        await ui.send(bot, f"<b>{html.escape(title)}</b> — {n} sortie(s){sorted_note}")
        for r in p.to_show:
            await ui.send(bot, formatting.card_text(r), ui.card_keyboard(r), ui.cover_preview(r), silent=True)
            await asyncio.sleep(settings.SEND_PAUSE_S)
    else:
        texts = formatting.compact_list(title, p.to_show, empty_text)
        await ui.send(bot, texts[0])
        await ui.send_all(bot, texts[1:], silent=True)
        await ui.send(bot, f"👉 {n} sorties à passer en revue une par une : /trier{sorted_note}", silent=True)

    if reminder:
        older = await asyncio.to_thread(storage.count_pending, [r.album_id for r in p.to_show])
        if older:
            await ui.send(bot, f"📥 {older} sortie(s) reçue(s) plus tôt t'attend(ent) encore dans /trier.", silent=True)

    await ui.send_all(bot, formatting.nothing_new(p.nothing_new), silent=True)
    await ui.send_all(bot, formatting.upcoming(p.upcoming), silent=True)


async def run_recap(bot, make_batch, title: str, empty_text: str, always: bool, *args, reminder: bool = False) -> None:
    async with ui.scan_lock:
        batch = await asyncio.to_thread(make_batch, *args)
        prepared = await asyncio.to_thread(pipeline.prepare, batch)
        await deliver(bot, title, prepared, empty_text, always, reminder=reminder)
        # On ne marque comme notifiées qu'APRÈS l'envoi réussi : en cas d'échec
        # Telegram, elles seront renvoyées au prochain passage.
        await asyncio.to_thread(pipeline.finalize, prepared)


async def do_daily(bot, force_message: bool) -> None:
    await run_recap(
        bot, scan.daily_batch, "🎧 Nouvelles sorties", "Rien de nouveau.",
        always=force_message or settings.SEND_EMPTY_DAILY,
    )


async def do_weekly(bot) -> None:
    await run_recap(
        bot, scan.weekly_batch, "📆 Récap du vendredi", "Rien de nouveau depuis le dernier récap.",
        always=True, reminder=True,
    )


async def do_catchup(bot, days: int) -> None:
    await run_recap(bot, scan.catchup_batch, f"⏪ Rattrapage ({days} jours)", "Rien trouvé sur la période.", True, days)


# --------------------------------------------------------------------------- #
# Résolution des artistes (démarrage, /actualiser)
# --------------------------------------------------------------------------- #

def bilan_has_news(bilan: dict) -> bool:
    return bool(bilan["resolved"] or bilan["removed"] or bilan["not_found"] or bilan["to_check"])


async def do_resolve(bot, bilan: dict | None = None) -> None:
    if bilan is None:
        async with ui.scan_lock:
            bilan = await asyncio.to_thread(artists.resolve)
    text = f"✅ Artistes : {bilan['resolved']} résolu(s), {bilan['cached']} déjà connu(s)."
    if bilan["removed"]:
        text += f"\n🗑 {bilan['removed']} retiré(s) (absents de artists.txt)."
    if bilan["not_found"]:
        text += "\n\n❌ Introuvables :\n" + "\n".join(f"• {n}" for n in bilan["not_found"])
    if bilan["to_check"]:
        text += (
            "\n\n🔎 À vérifier (nom différent, homonymes ou genre inattendu). "
            "Si ce n'est pas le bon artiste : /verifier, ou /retirer puis /ajouter avec le lien "
            "Apple Music de l'artiste :\n"
            + "\n".join(f"• {n}" for n in bilan["to_check"])
        )
    for i in range(0, len(text), 4000):
        await bot.send_message(chat_id=settings.CHAT_ID, text=text[i : i + 4000])


# --------------------------------------------------------------------------- #
# Jobs planifiés
# --------------------------------------------------------------------------- #

async def scheduled(context: ContextTypes.DEFAULT_TYPE, what: str, run, job_fn) -> None:
    """Lance un récap planifié ; en cas d'échec, un seul nouvel essai 1 h plus tard. Sauvegarde si succès."""
    is_retry = bool(context.job and isinstance(context.job.data, dict) and context.job.data.get("retry"))
    try:
        await run()
    except Exception as exc:  # noqa: BLE001
        if is_retry:
            await ui.notify_error(context.bot, what, exc, "\nDeuxième échec : prochain essai au récap suivant.")
        else:
            await ui.notify_error(context.bot, what, exc, "\nNouvel essai automatique dans 1 h.")
            context.job_queue.run_once(job_fn, when=settings.RETRY_DELAY_S, data={"retry": True})
        return
    try:
        await asyncio.to_thread(storage.backup)
    except Exception:
        logger.exception("Sauvegarde de la base impossible")


async def job_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    await scheduled(context, "le récap quotidien", lambda: do_daily(context.bot, force_message=False), job_daily)


async def job_weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
    await scheduled(context, "le récap du vendredi", lambda: do_weekly(context.bot), job_weekly)


async def job_startup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Au démarrage : un seul message, silencieux, sauf s'il y a du nouveau côté artistes."""
    try:
        restored = await asyncio.to_thread(storage.migrate)
        async with ui.scan_lock:
            bilan = await asyncio.to_thread(artists.resolve)
        total = bilan["resolved"] + bilan["cached"]
        text = f"🚀 Music Radar redémarré · {total} artistes suivis."
        if restored:
            text += f"\n↩️ {restored} version(s) alternative(s) remise(s) dans /trier."
        await context.bot.send_message(chat_id=settings.CHAT_ID, text=text, disable_notification=True)
        if bilan_has_news(bilan):
            await do_resolve(context.bot, bilan)
    except Exception as exc:  # noqa: BLE001
        await ui.notify_error(context.bot, "le démarrage", exc)


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #

async def cmd_jour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ui.reply_wait(update)
    try:
        await do_daily(context.bot, force_message=True)
    except Exception as exc:  # noqa: BLE001
        await ui.notify_error(context.bot, "/jour", exc)


async def cmd_semaine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ui.reply_wait(update)
    try:
        await do_weekly(context.bot)
    except Exception as exc:  # noqa: BLE001
        await ui.notify_error(context.bot, "/semaine", exc)


async def cmd_rattrapage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = 30
    if context.args:
        try:
            days = max(1, min(int(context.args[0]), 365))
        except ValueError:
            await update.message.reply_text("Usage : /rattrapage 30")
            return
    await ui.reply_wait(update, f"⏳ Scan des {days} derniers jours (≈ 5 min avec la recherche des featurings)…")
    try:
        await do_catchup(context.bot, days)
    except Exception as exc:  # noqa: BLE001
        await ui.notify_error(context.bot, "/rattrapage", exc)


async def cmd_actualiser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ui.reply_wait(update, "⏳ Lecture de artists.txt…")
    try:
        await do_resolve(context.bot)
    except Exception as exc:  # noqa: BLE001
        await ui.notify_error(context.bot, "/actualiser", exc)
