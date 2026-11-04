"""
bot.py
======

Bot Telegram du Music Radar.

Planning (fuseau Europe/Paris par défaut) :
    - Récap QUOTIDIEN à DAILY_TIME  : tous les jours SAUF le vendredi,
      uniquement les sorties pas encore notifiées (rien n'est envoyé s'il n'y
      a rien de nouveau, sauf si SEND_EMPTY_DAILY=1).
    - Récap HEBDO le vendredi à WEEKLY_TIME : toutes les sorties de la
      semaine (samedi -> vendredi) + les sorties annoncées pour les jours
      suivants. Il remplace le quotidien ce jour-là.

Chaque sortie arrive avec deux boutons : « Ajoutée » / « Ignorer ». Au-delà
de CARDS_MAX sorties d'un coup (rattrapage, grosse semaine), le récap passe en
liste compacte et /trier les présente une par une.

Commandes (uniquement depuis TELEGRAM_CHAT_ID, tout le reste est ignoré) :
    /trier              passe en revue les sorties pas encore triées
    /ajouter <artiste>  ajoute un artiste (nom, `Nom | id` ou lien Apple Music)
    /retirer <artiste>  retire un artiste
    /artistes [filtre]  liste des artistes suivis
    /jour, /semaine     récaps à la demande
    /rattrapage [n]     sorties des n derniers jours (30 par défaut)
    /actualiser         relit artists.txt et résout les nouveaux artistes
    /aide               rappel des commandes
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import traceback
from datetime import time as dtime
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import radar

load_dotenv()
# Logs : console (docker compose logs) + fichier data/bot.log, qui survit aux
# reconstructions du conteneur (data/ est un volume). 3 x 1 Mo maximum.
radar.DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(radar.DATA_DIR / "bot.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
    ],
)
# httpx (utilisé par python-telegram-bot) logge chaque requête en INFO, URL
# comprise, et cette URL contient le token du bot. On coupe ces logs pour que
# le token n'apparaisse jamais dans `docker compose logs`.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
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

# Un seul scan à la fois : chaque scan fait ~160 appels espacés de 3 s, deux
# scans en parallèle dépasseraient la limite de débit d'iTunes.
_scan_lock = asyncio.Lock()

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
SEND_PAUSE_S = 0.4  # Telegram tolère ~1 message/s dans un même chat


def _parse_time(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm), tzinfo=radar.TZ)


# --------------------------------------------------------------------------- #
# Envoi
# --------------------------------------------------------------------------- #

def _cover_preview(r: radar.Release | None) -> LinkPreviewOptions:
    """Pochette en petite miniature : l'aperçu du lien pointe sur l'image."""
    if COVERS == "small" and r is not None and r.cover:
        return LinkPreviewOptions(url=r.cover, prefer_small_media=True, show_above_text=False)
    return NO_PREVIEW


async def _send(
    bot,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    preview: LinkPreviewOptions = NO_PREVIEW,
) -> None:
    await bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        link_preview_options=preview,
    )


async def _send_all(bot, texts: list[str]) -> None:
    for t in texts:
        await _send(bot, t)


async def _notify_error(bot, what: str, exc: Exception) -> None:
    logger.exception("Erreur pendant %s", what)
    try:
        await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Erreur pendant {what} : {exc}")
    except Exception:
        logger.exception("Impossible de notifier l'erreur sur Telegram")


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _card_keyboard(r: radar.Release) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn("➕ Ajoutée", f"r:l:{r.album_id}"), _btn("🙈 Ignorer", f"r:i:{r.album_id}")]]
    )


def _undo_keyboard(r: radar.Release) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("↩️ Annuler", f"r:u:{r.album_id}")]])


# Boutons de la file /trier (préfixe q:n: conservé pour les anciens messages).
def _queue_keyboard(queue: str, r: radar.Release) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                _btn("➕ Ajoutée", f"q:n:L:{r.album_id}"),
                _btn("🙈 Ignorer", f"q:n:I:{r.album_id}"),
            ],
            [_btn("⏭ Plus tard", f"q:n:S:{r.album_id}")],
        ]
    )


async def deliver(bot, title: str, p: radar.Prepared, empty_text: str, always: bool) -> None:
    """Envoie un récap : cartes à boutons si peu de sorties, sinon liste + /trier."""
    if not (p.to_show or p.nothing_new or p.upcoming) and not always:
        return

    n = len(p.to_show)
    sorted_note = f" ({p.already_sorted} déjà triée(s))" if p.already_sorted else ""

    if n == 0:
        await _send(bot, f"<b>{html.escape(title)}</b>\n\n{html.escape(empty_text)}{sorted_note}")
    elif n <= CARDS_MAX:
        await _send(bot, f"<b>{html.escape(title)}</b> — {n} sortie(s){sorted_note}")
        for r in p.to_show:
            await _send(bot, radar.card_text(r), _card_keyboard(r), _cover_preview(r))
            await asyncio.sleep(SEND_PAUSE_S)
    else:
        await _send_all(bot, radar.format_messages(title, p.to_show, empty_text))
        await _send(bot, f"👉 {n} sorties à passer en revue une par une : /trier{sorted_note}")

    await _send_all(bot, radar.format_nothing_new(p.nothing_new))
    await _send_all(bot, radar.format_upcoming(p.upcoming))


# --------------------------------------------------------------------------- #
# Récaps (partagés entre jobs planifiés et commandes manuelles)
# --------------------------------------------------------------------------- #

async def _run_recap(bot, make_batch, title: str, empty_text: str, always: bool, *args) -> None:
    async with _scan_lock:
        batch = await asyncio.to_thread(make_batch, *args)
        prepared = await asyncio.to_thread(radar.prepare, batch)
        await deliver(bot, title, prepared, empty_text, always)
        # On ne marque comme notifiées qu'APRÈS l'envoi réussi : en cas d'échec
        # Telegram, elles seront renvoyées au prochain passage.
        await asyncio.to_thread(radar.finalize, prepared)


async def do_daily(bot, force_message: bool) -> None:
    await _run_recap(
        bot, radar.daily_batch, "🎧 Sorties du jour", "Rien de nouveau.",
        always=force_message or SEND_EMPTY_DAILY,
    )


async def do_weekly(bot) -> None:
    await _run_recap(bot, radar.weekly_batch, "📆 Récap de la semaine", "Rien cette semaine.", always=True)


async def do_catchup(bot, days: int) -> None:
    await _run_recap(
        bot, radar.catchup_batch, f"⏪ Rattrapage ({days} jours)", "Rien trouvé sur la période.",
        True, days,
    )


async def do_resolve(bot) -> None:
    async with _scan_lock:
        bilan = await asyncio.to_thread(radar.resolve_artists)
    text = f"✅ Artistes : {bilan['resolved']} résolu(s), {bilan['cached']} déjà connu(s)."
    if bilan["removed"]:
        text += f"\n🗑 {bilan['removed']} retiré(s) (absents de artists.txt)."
    if bilan["not_found"]:
        text += "\n\n❌ Introuvables :\n" + "\n".join(f"• {n}" for n in bilan["not_found"])
    if bilan["to_check"]:
        text += (
            "\n\n🔎 À vérifier (nom différent, homonymes ou genre inattendu). "
            "Si ce n'est pas le bon artiste : /retirer puis /ajouter avec le lien "
            "Apple Music de l'artiste :\n"
            + "\n".join(f"• {n}" for n in bilan["to_check"])
        )
    # Découpage simple si la liste est longue.
    for i in range(0, len(text), 4000):
        await bot.send_message(chat_id=CHAT_ID, text=text[i : i + 4000])


# --------------------------------------------------------------------------- #
# Jobs planifiés
# --------------------------------------------------------------------------- #

async def job_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await do_daily(context.bot, force_message=False)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "le récap quotidien", exc)


async def job_weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await do_weekly(context.bot)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "le récap hebdomadaire", exc)


async def job_startup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Au démarrage : résout les artistes (rapide si tout est déjà en cache)."""
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text="🚀 Music Radar démarré. Résolution des artistes…")
        restored = await asyncio.to_thread(radar.migrate)
        if restored:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"↩️ {restored} version(s) alternative(s) remise(s) dans /trier.",
            )
        await do_resolve(context.bot)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "la résolution des artistes", exc)


# --------------------------------------------------------------------------- #
# Commandes : récaps
# --------------------------------------------------------------------------- #

SCAN_WAIT = "⏳ Scan en cours (≈ 20 min avec la recherche des featurings)…"


async def cmd_jour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(SCAN_WAIT)
    try:
        await do_daily(context.bot, force_message=True)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "/jour", exc)


async def cmd_semaine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(SCAN_WAIT)
    try:
        await do_weekly(context.bot)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "/semaine", exc)


async def cmd_rattrapage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = 30
    if context.args:
        try:
            days = max(1, min(int(context.args[0]), 365))
        except ValueError:
            await update.message.reply_text("Usage : /rattrapage 30")
            return
    await update.message.reply_text(f"⏳ Scan des {days} derniers jours (≈ 20 min avec la recherche des featurings)…")
    try:
        await do_catchup(context.bot, days)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "/rattrapage", exc)


async def cmd_actualiser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Lecture de artists.txt…")
    try:
        await do_resolve(context.bot)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "/actualiser", exc)


HELP_TEXT = (
    "<b>Music Radar</b>\n"
    "/trier — passer en revue les sorties pas encore triées\n"
    "/ajouter <i>artiste</i> — ajoute un artiste (nom, <code>Nom | id</code> ou lien Apple Music)\n"
    "/retirer <i>artiste</i> — retire un artiste\n"
    "/artistes — liste des artistes suivis\n"
    "/verifier — vérifier les artistes au nom ambigu\n"
    "/jour · /semaine · /rattrapage <i>n</i> — récaps à la demande\n"
    "/actualiser — relit artists.txt"
)


async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Tri : boutons sous chaque sortie, et file /trier
# --------------------------------------------------------------------------- #

async def _answer(query: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """
    Accusé de réception d'un bouton. Telegram le refuse si le bouton a été
    touché il y a trop longtemps ou après un redémarrage : sans gravité, l'action
    elle-même a déjà été faite, donc on l'ignore au lieu de lever une erreur.
    """
    try:
        await query.answer(text, show_alert=show_alert)
    except BadRequest as exc:
        logger.info("Accusé de réception de bouton refusé (%s)", exc)


def _authorized(query: CallbackQuery) -> bool:
    return query.message is not None and query.message.chat.id == CHAT_ID


async def _edit(
    query: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None,
    preview: LinkPreviewOptions = NO_PREVIEW,
) -> None:
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=preview
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


CARD_ACTIONS = {
    "l": (radar.STATE_ADDED, "➕ Ajoutée à ta playlist"),
    "i": (radar.STATE_IGNORED, "🙈 Ignorée"),
    "u": (radar.STATE_NEW, ""),
}


async def on_card_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boutons d'une carte du récap : r:<l|i|u>:<album_id>."""
    q = update.callback_query
    if not _authorized(q):
        await _answer(q)
        return
    try:
        _, action, sid = q.data.split(":")
        state, label = CARD_ACTIONS[action]
        album_id = int(sid)
    except (ValueError, KeyError):
        await _answer(q)
        return

    await asyncio.to_thread(radar.set_state, [album_id], state)
    r = await asyncio.to_thread(radar.get_release, album_id)
    if r is None:
        await _answer(q, "Sortie introuvable")
        return
    if state == radar.STATE_NEW:
        await _edit(q, radar.card_text(r), _card_keyboard(r), _cover_preview(r))
    else:
        await _edit(q, f"{radar.card_text(r)}\n{label}", _undo_keyboard(r))
    await _answer(q)


QUEUE_STATE = {"n": radar.STATE_NEW}
QUEUE_LABEL = {"n": "à trier"}
# (file, bouton) -> nouvel état de la sortie sur laquelle on a appuyé.
QUEUE_ACTIONS = {
    ("n", "L"): radar.STATE_ADDED,
    ("n", "I"): radar.STATE_IGNORED,
}


async def _queue_view(queue: str, after_id: int) -> tuple[str, InlineKeyboardMarkup | None, LinkPreviewOptions]:
    state = QUEUE_STATE[queue]
    nxt, total = await asyncio.to_thread(radar.next_in_queue, state, after_id)
    if nxt is None:
        left = await asyncio.to_thread(radar.count_state, state)
        text = "🎉 File terminée."
        if left:
            text += f"\nIl en reste {left} (passées avec « Plus tard ») : /trier pour y revenir."
        return text, None, NO_PREVIEW
    text = f"{radar.card_text(nxt)}\n<i>{total} {QUEUE_LABEL[queue]}</i>"
    return text, _queue_keyboard(queue, nxt), _cover_preview(nxt)


async def on_queue_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boutons de la file : q:<n|l>:<action>:<album_id courant (0 = début)>."""
    q = update.callback_query
    if not _authorized(q):
        await _answer(q)
        return
    try:
        _, queue, action, scur = q.data.split(":")
        cur = int(scur)
    except ValueError:
        await _answer(q)
        return
    if queue not in QUEUE_STATE:
        await _answer(q)
        return

    new_state = QUEUE_ACTIONS.get((queue, action))
    if new_state and cur:
        await asyncio.to_thread(radar.set_state, [cur], new_state)
    text, markup, preview = await _queue_view(queue, cur)
    await _edit(q, text, markup, preview)
    await _answer(q)


async def _start_queue(update: Update, queue: str) -> None:
    text, markup, preview = await _queue_view(queue, 0)
    if markup is None:
        text = "🎉 Rien à trier."
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=preview
    )


async def cmd_trier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_queue(update, "n")


# --------------------------------------------------------------------------- #
# Gestion des artistes depuis Telegram
# --------------------------------------------------------------------------- #

def _adds(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault("adds", {})


def _remember(context: ContextTypes.DEFAULT_TYPE, data: dict) -> str:
    """Garde en mémoire (jusqu'au redémarrage) ce dont un bouton aura besoin."""
    store = _adds(context)
    while len(store) >= 50:
        store.pop(next(iter(store)))
    token = secrets.token_hex(3)
    store[token] = data
    return token


async def _confirm_add(context: ContextTypes.DEFAULT_TYPE, res: dict) -> None:
    added = await asyncio.to_thread(
        radar.add_artist, res["name"], res["id"], res["matched"], res["forced"]
    )
    if not added:
        await _send(context.bot, f"Déjà dans la liste : {html.escape(res['name'])}")
        return
    try:
        recent = await asyncio.to_thread(radar.recent_releases, res["id"], 3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dernières sorties de %s indisponibles : %s", res["name"], exc)
        recent = []

    name = html.escape(res["name"])
    lines = [f"✅ <b>{name}</b> ajouté — {html.escape(res['genre'])}"]
    if res["matched"].casefold() != res["name"].casefold():
        lines[0] += f"\nTrouvé sous le nom « {html.escape(res['matched'])} »"
    if res["url"]:
        lines.append(f'<a href="{html.escape(res["url"], quote=True)}">Page Apple Music</a>')
    if recent:
        lines.append("Dernières sorties :")
        lines += [
            f"• {r.release_date:%d/%m/%Y} · {html.escape(r.title)} <i>({html.escape(r.kind)})</i>"
            for r in recent
        ]
    if not any(h in res["genre"].casefold() for h in radar.EXPECTED_GENRE_HINTS):
        lines.append("⚠️ Genre inattendu : vérifie que c'est le bon artiste.")
    lines.append("Il sera pris en compte dès le prochain récap.")

    token = _remember(context, {"name": res["name"]})
    markup = InlineKeyboardMarkup([[_btn("↩️ Annuler l'ajout", f"ad:{token}:u")]])
    await _send(context.bot, "\n".join(lines), markup)


async def cmd_ajouter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage : /ajouter Nom de l'artiste\n"
            "ou /ajouter Nom | 123456789\n"
            "ou /ajouter https://music.apple.com/…/artist/…"
        )
        return
    await update.message.reply_text("🔎 Recherche…")
    try:
        res = await asyncio.to_thread(radar.prepare_add, text)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "/ajouter", exc)
        return

    kind = res["kind"]
    if kind == "exists":
        await _send(context.bot, f"Déjà dans la liste : <b>{html.escape(res['name'])}</b>")
    elif kind == "none":
        await _send(context.bot, "❌ Aucun artiste trouvé. Essaie avec le lien de sa page Apple Music.")
    elif kind == "ready":
        await _confirm_add(context, res)
    else:  # "choose"
        cands = res["candidates"]
        token = _remember(context, {"name": res["name"], "cands": cands})
        lines = [f"Plusieurs résultats pour <b>{html.escape(res['name'])}</b>, lequel ?"]
        for i, c in enumerate(cands, 1):
            name = html.escape(c["name"])
            link = f'<a href="{html.escape(c["url"], quote=True)}">{name}</a>' if c["url"] else name
            lines.append(f"{i}. {link} — {html.escape(c['genre'])} · dernière sortie : {html.escape(c.get('last', '?'))}")
        buttons = [_btn(str(i), f"ad:{token}:{i - 1}") for i in range(1, len(cands) + 1)]
        buttons.append(_btn("✖️ Annuler", f"ad:{token}:x"))
        await _send(context.bot, "\n".join(lines), InlineKeyboardMarkup([buttons]))


async def on_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ad:<jeton>:<n° de candidat | u (annuler l'ajout) | x (abandon)>."""
    q = update.callback_query
    if not _authorized(q):
        await _answer(q)
        return
    try:
        _, token, choice = q.data.split(":")
    except ValueError:
        await _answer(q)
        return
    data = _adds(context).get(token)
    if data is None:
        await _answer(q, "Expiré (le bot a redémarré). Refais la commande.", show_alert=True)
        return

    if choice == "u":
        await asyncio.to_thread(radar.remove_artist, data["name"])
        _adds(context).pop(token, None)
        await _edit(q, f"↩️ {html.escape(data['name'])} retiré.", None)
        await _answer(q)
        return
    if choice == "x":
        _adds(context).pop(token, None)
        await _edit(q, "Annulé.", None)
        await _answer(q)
        return

    try:
        cand = data["cands"][int(choice)]
    except (KeyError, IndexError, ValueError):
        await _answer(q)
        return
    _adds(context).pop(token, None)
    await _edit(q, f"Choisi : {html.escape(cand['name'])} ({html.escape(cand['genre'])})", None)
    await _answer(q)
    # Plusieurs artistes portent ce nom : on force l'ID choisi.
    await _confirm_add(
        context,
        {
            "name": data["name"], "id": cand["id"], "matched": cand["name"],
            "genre": cand["genre"], "url": cand["url"], "forced": True,
        },
    )


async def cmd_retirer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text("Usage : /retirer Nom de l'artiste")
        return
    res = await asyncio.to_thread(radar.remove_artist, text)
    if res["status"] == "removed":
        names = ", ".join(html.escape(n) for n in res["names"])
        await _send(context.bot, f"🗑 Retiré : <b>{names}</b>. Il n'est plus suivi.")
    elif res["status"] == "ambiguous":
        listing = "\n".join(f"• {html.escape(n)}" for n in res["candidates"])
        await _send(context.bot, f"Plusieurs artistes correspondent, donne le nom exact :\n{listing}")
    else:
        await _send(context.bot, "Aucun artiste suivi ne correspond. /artistes pour voir la liste.")


# --------------------------------------------------------------------------- #
# /verifier : artistes dont le nom est porté par plusieurs artistes chez Apple
# --------------------------------------------------------------------------- #

def _artist_card(info: dict, followed: str) -> str:
    name = html.escape(info["name"])
    link = f'<a href="{html.escape(info["url"], quote=True)}">{name}</a>' if info.get("url") else name
    return (
        f"<b>{html.escape(followed)}</b> → {link} — {html.escape(info['genre'])}\n"
        f"<i>Dernière sortie : {html.escape(info['last'])}</i>"
    )


def _verify_keyboard(artist_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("✅ C'est le bon", f"vf:ok:{artist_id}"), _btn("🔄 Changer", f"vf:ch:{artist_id}")],
            [_btn("🗑 Ne plus suivre", f"vf:rm:{artist_id}")],
        ]
    )


async def cmd_verifier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⏳ Recherche des artistes au nom ambigu (quelques minutes la première fois)…"
    )
    try:
        items = await asyncio.to_thread(radar.ambiguous_artists)
    except Exception as exc:  # noqa: BLE001
        await _notify_error(context.bot, "/verifier", exc)
        return
    if not items:
        await _send(context.bot, "✅ Aucun artiste à vérifier.")
        return
    await _send(
        context.bot,
        f"🔎 <b>{len(items)} artiste(s) à vérifier</b> : plusieurs artistes portent ce nom chez Apple. "
        "Vérifie que le bot suit le bon (genre, dernière sortie, lien).",
    )
    for info in items:
        await _send(context.bot, _artist_card(info, info["followed"]), _verify_keyboard(info["id"]))
        await asyncio.sleep(SEND_PAUSE_S)


async def on_verify_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vf:ok|ch|rm:<id>  ·  vf:pk:<jeton>:<n° d'homonyme>."""
    q = update.callback_query
    if not _authorized(q):
        await _answer(q)
        return
    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "pk" and len(parts) == 4:
        data = _adds(context).pop(parts[2], None)
        if data is None:
            await _answer(q, "Expiré (le bot a redémarré). Refais /verifier.", show_alert=True)
            return
        try:
            cand = data["cands"][int(parts[3])]
        except (IndexError, ValueError):
            await _answer(q)
            return
        name = await asyncio.to_thread(radar.switch_artist, data["old"], cand["id"], cand["name"])
        await _edit(q, f"🔄 <b>{html.escape(name or '?')}</b> suit maintenant :\n{_artist_card(cand, name or '?')}", None)
        await _answer(q, "Artiste changé")
        return

    try:
        artist_id = int(parts[2])
    except (IndexError, ValueError):
        await _answer(q)
        return
    name = await asyncio.to_thread(radar.followed_name, artist_id)
    if name is None:
        await _edit(q, "Cet artiste n'est plus suivi.", None)
        await _answer(q)
        return

    if action == "ok":
        await asyncio.to_thread(radar.mark_verified, artist_id)
        await _edit(q, f"✅ <b>{html.escape(name)}</b> : confirmé.", None)
    elif action == "rm":
        await asyncio.to_thread(radar.remove_artist, name)
        await asyncio.to_thread(radar.drop_pending_of, artist_id)
        await _edit(q, f"🗑 <b>{html.escape(name)}</b> n'est plus suivi.", None)
    elif action == "ch":
        await _answer(q, "Recherche des homonymes…")
        cands = await asyncio.to_thread(radar.homonyms, artist_id)
        if not cands:
            await _edit(q, f"Aucun autre « {html.escape(name)} » trouvé chez Apple.", _verify_keyboard(artist_id))
            return
        token = _remember(context, {"old": artist_id, "cands": cands})
        lines = [f"Quel « {html.escape(name)} » veux-tu suivre ?"]
        lines += [f"{i}. {_artist_card(c, name)}" for i, c in enumerate(cands, 1)]
        buttons = [_btn(str(i), f"vf:pk:{token}:{i - 1}") for i in range(1, len(cands) + 1)]
        buttons.append(_btn("↩️ Garder l'actuel", f"vf:ok:{artist_id}"))
        await _edit(q, "\n".join(lines), InlineKeyboardMarkup([buttons]))
        return
    await _answer(q)


async def cmd_artistes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    needle = " ".join(context.args).strip() if context.args else ""
    names = await asyncio.to_thread(radar.artists_matching, needle)
    if not names:
        await update.message.reply_text("Aucun artiste.")
        return
    body = " · ".join(html.escape(n) for n in names)
    texts = radar.split_for_telegram([f"<b>{len(names)} artiste(s)</b>", body])
    await _send_all(context.bot, texts)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("trier", "Passer en revue les sorties non triées"),
            BotCommand("ajouter", "Ajouter un artiste"),
            BotCommand("retirer", "Retirer un artiste"),
            BotCommand("artistes", "Artistes suivis"),
            BotCommand("verifier", "Vérifier les artistes au nom ambigu"),
            BotCommand("jour", "Sorties du jour"),
            BotCommand("semaine", "Récap de la semaine"),
            BotCommand("rattrapage", "Sorties des n derniers jours"),
            BotCommand("actualiser", "Relire artists.txt"),
            BotCommand("aide", "Aide"),
        ]
    )


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toute erreur imprévue : trace complète dans les logs + résumé sur Telegram."""
    err = context.error
    if isinstance(err, NetworkError) and not isinstance(err, BadRequest):
        # Coupure réseau passagère (box, Telegram) : la bibliothèque réessaie
        # toute seule. On la note sans t'envoyer de message.
        logger.warning("Coupure réseau passagère : %s", err)
        return
    logger.error("Erreur non geree", exc_info=err)
    if err is None:
        return
    where = traceback.extract_tb(err.__traceback__)[-1] if err.__traceback__ else None
    origin = f" ({where.name}, ligne {where.lineno})" if where else ""
    detail = f"{type(err).__name__}: {err}".replace(TOKEN, "***")[:300]
    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⚠️ Erreur imprévue{origin}\n{detail}\nDétail complet dans data/bot.log.",
        )
    except Exception:
        logger.exception("Impossible de signaler l'erreur sur Telegram")


COMMANDS = {
    "jour": cmd_jour,
    "semaine": cmd_semaine,
    "rattrapage": cmd_rattrapage,
    "actualiser": cmd_actualiser,
    "trier": cmd_trier,
    "ajouter": cmd_ajouter,
    "retirer": cmd_retirer,
    "artistes": cmd_artistes,
    "verifier": cmd_verifier,
    "aide": cmd_aide,
    "start": cmd_aide,
}


async def on_plain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'/retirer SAM' envoyé en texte mis en forme : on l'exécute comme une commande."""
    text = (update.message.text or "").strip()
    name, _, rest = text[1:].partition(" ")
    handler = COMMANDS.get(name.split("@")[0].lower())
    if handler is None:
        await update.message.reply_text("Commande inconnue. /aide pour la liste.")
        return
    context.args = rest.split()
    await handler(update, context)


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(_post_init).concurrent_updates(True).build()

    # Seul ton chat peut piloter le bot.
    only_me = filters.Chat(chat_id=CHAT_ID)
    for name, handler in COMMANDS.items():
        app.add_handler(CommandHandler(name, handler, filters=only_me))
    # Commande collée avec une mise en forme (police de code…) : Telegram ne la
    # marque pas comme commande, on la reconnaît quand même.
    app.add_handler(
        MessageHandler(filters.TEXT & only_me & ~filters.COMMAND & filters.Regex(r"^\s*/\w"), on_plain_command)
    )
    # Les boutons vérifient eux-mêmes le chat (_authorized).
    app.add_handler(CallbackQueryHandler(on_card_button, pattern=r"^r:"))
    app.add_handler(CallbackQueryHandler(on_queue_button, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(on_add_button, pattern=r"^ad:"))
    app.add_handler(CallbackQueryHandler(on_verify_button, pattern=r"^vf:"))
    app.add_error_handler(_on_error)

    jq = app.job_queue
    jq.run_daily(job_daily, time=_parse_time(DAILY_TIME), days=ALL_DAYS_BUT_FRIDAY, name="daily")
    jq.run_daily(job_weekly, time=_parse_time(WEEKLY_TIME), days=(FRIDAY,), name="weekly")
    jq.run_once(job_startup, when=5, name="startup")

    logger.info("Bot lancé. Quotidien %s (hors vendredi), hebdo vendredi %s.", DAILY_TIME, WEEKLY_TIME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
