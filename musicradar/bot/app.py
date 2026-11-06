"""
Assemblage du bot : commandes, boutons, planification, erreurs.

Planning (fuseau Europe/Paris par défaut) :
    - Récap QUOTIDIEN à DAILY_TIME, tous les jours sauf le vendredi : les
      nouvelles sorties pas encore notifiées (« Rien de nouveau » seulement si
      SEND_EMPTY_DAILY=1).
    - Récap du VENDREDI à WEEKLY_TIME : il remplace le quotidien ce jour-là,
      rappelle combien de sorties attendent dans /trier et annonce celles des
      7 jours suivants.

Seul TELEGRAM_CHAT_ID peut piloter le bot : tout le reste est ignoré.
"""

from __future__ import annotations

import logging
import traceback
from datetime import time as dtime
from logging.handlers import RotatingFileHandler

from telegram import BotCommand, Update
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

from .. import config
from . import artists, recaps, settings, sorting, ui

logger = logging.getLogger("bot")

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


COMMANDS = {
    "jour": recaps.cmd_jour,
    "semaine": recaps.cmd_semaine,
    "rattrapage": recaps.cmd_rattrapage,
    "actualiser": recaps.cmd_actualiser,
    "trier": sorting.cmd_trier,
    "ajouter": artists.cmd_ajouter,
    "retirer": artists.cmd_retirer,
    "artistes": artists.cmd_artistes,
    "verifier": artists.cmd_verifier,
    "aide": cmd_aide,
    "start": cmd_aide,
}

MENU = [
    BotCommand("trier", "Passer en revue les sorties non triées"),
    BotCommand("ajouter", "Ajouter un artiste"),
    BotCommand("retirer", "Retirer un artiste"),
    BotCommand("artistes", "Artistes suivis"),
    BotCommand("verifier", "Vérifier les artistes au nom ambigu"),
    BotCommand("jour", "Nouvelles sorties (à la demande)"),
    BotCommand("semaine", "Récap du vendredi (à la demande)"),
    BotCommand("rattrapage", "Sorties des n derniers jours"),
    BotCommand("actualiser", "Relire artists.txt"),
    BotCommand("aide", "Aide"),
]


async def on_plain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'/retirer SAM' envoyé en texte mis en forme (police de code…) : on l'exécute quand même."""
    msg_text = (update.message.text or "").strip()
    name, _, rest = msg_text[1:].partition(" ")
    handler = COMMANDS.get(name.split("@")[0].lower())
    if handler is None:
        await update.message.reply_text("Commande inconnue. /aide pour la liste.")
        return
    context.args = rest.split()
    await handler(update, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    detail = ui.mask(f"{type(err).__name__}: {err}")[:300]
    try:
        await context.bot.send_message(
            chat_id=settings.CHAT_ID,
            text=f"⚠️ Erreur imprévue{origin}\n{detail}\nDétail complet dans data/bot.log.",
        )
    except Exception:
        logger.exception("Impossible de signaler l'erreur sur Telegram")


def setup_logging() -> None:
    """Console (docker compose logs) + data/bot.log, qui survit aux reconstructions (3 x 1 Mo)."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(config.DATA_DIR / "bot.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
        ],
    )
    # httpx (utilisé par python-telegram-bot) logge chaque requête en INFO, URL
    # comprise, et cette URL contient le token du bot : on coupe ces logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _at(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm), tzinfo=config.TZ)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(MENU)


def build_app() -> Application:
    app = Application.builder().token(settings.TOKEN).post_init(_post_init).concurrent_updates(True).build()

    only_me = filters.Chat(chat_id=settings.CHAT_ID)
    for name, handler in COMMANDS.items():
        app.add_handler(CommandHandler(name, handler, filters=only_me))
    app.add_handler(
        MessageHandler(filters.TEXT & only_me & ~filters.COMMAND & filters.Regex(r"^\s*/\w"), on_plain_command)
    )
    # Les boutons vérifient eux-mêmes le chat (ui.authorized).
    app.add_handler(CallbackQueryHandler(sorting.on_card_button, pattern=r"^r:"))
    app.add_handler(CallbackQueryHandler(sorting.on_queue_button, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(artists.on_add_button, pattern=r"^ad:"))
    app.add_handler(CallbackQueryHandler(artists.on_verify_button, pattern=r"^vf:"))
    app.add_error_handler(on_error)

    jq = app.job_queue
    jq.run_daily(recaps.job_daily, time=_at(settings.DAILY_TIME), days=settings.ALL_DAYS_BUT_FRIDAY, name="daily")
    jq.run_daily(recaps.job_weekly, time=_at(settings.WEEKLY_TIME), days=(settings.FRIDAY,), name="weekly")
    jq.run_once(recaps.job_startup, when=5, name="startup")
    return app


def main() -> None:
    setup_logging()
    app = build_app()
    logger.info("Bot lancé. Quotidien %s (hors vendredi), vendredi %s.", settings.DAILY_TIME, settings.WEEKLY_TIME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
