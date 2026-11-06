"""Gestion des artistes depuis Telegram : /ajouter, /retirer, /artistes, /verifier."""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .. import artists, config, formatting, storage
from ..sources import itunes
from . import settings, ui

logger = logging.getLogger("bot.artists")


# --------------------------------------------------------------------------- #
# /ajouter
# --------------------------------------------------------------------------- #

async def _confirm_add(context: ContextTypes.DEFAULT_TYPE, res: dict) -> None:
    added = await asyncio.to_thread(artists.add, res["name"], res["id"], res["matched"], res["forced"])
    if not added:
        await ui.send(context.bot, f"Déjà dans la liste : {html.escape(res['name'])}")
        return
    try:
        recent = await asyncio.to_thread(itunes.recent_releases, res["id"], 3)
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
            f"• {r.release_date:%d/%m/%Y} · {html.escape(r.title)} <i>({html.escape(r.kind)})</i>" for r in recent
        ]
    if not any(h in res["genre"].casefold() for h in config.EXPECTED_GENRE_HINTS):
        lines.append("⚠️ Genre inattendu : vérifie que c'est le bon artiste.")
    lines.append("Il sera pris en compte dès le prochain récap.")

    token = ui.remember(context, {"name": res["name"]})
    markup = InlineKeyboardMarkup([[ui.btn("↩️ Annuler l'ajout", f"ad:{token}:u")]])
    await ui.send(context.bot, "\n".join(lines), markup)


async def cmd_ajouter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "Usage : /ajouter Nom de l'artiste\nou /ajouter Nom | 123456789\nou /ajouter https://music.apple.com/…/artist/…"
        )
        return
    await update.message.reply_text("🔎 Recherche…")
    try:
        res = await asyncio.to_thread(artists.prepare_add, query)
    except Exception as exc:  # noqa: BLE001
        await ui.notify_error(context.bot, "/ajouter", exc)
        return

    kind = res["kind"]
    if kind == "exists":
        await ui.send(context.bot, f"Déjà dans la liste : <b>{html.escape(res['name'])}</b>")
    elif kind == "none":
        await ui.send(context.bot, "❌ Aucun artiste trouvé. Essaie avec le lien de sa page Apple Music.")
    elif kind == "ready":
        await _confirm_add(context, res)
    else:  # "choose"
        cands = res["candidates"]
        token = ui.remember(context, {"name": res["name"], "cands": cands})
        lines = [f"Plusieurs résultats pour <b>{html.escape(res['name'])}</b>, lequel ?"]
        for i, c in enumerate(cands, 1):
            name = html.escape(c["name"])
            link = f'<a href="{html.escape(c["url"], quote=True)}">{name}</a>' if c["url"] else name
            lines.append(f"{i}. {link} — {html.escape(c['genre'])} · dernière sortie : {html.escape(c.get('last', '?'))}")
        buttons = [ui.btn(str(i), f"ad:{token}:{i - 1}") for i in range(1, len(cands) + 1)]
        buttons.append(ui.btn("✖️ Annuler", f"ad:{token}:x"))
        await ui.send(context.bot, "\n".join(lines), InlineKeyboardMarkup([buttons]))


async def on_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ad:<jeton>:<n° de candidat | u (annuler l'ajout) | x (abandon)>."""
    q = update.callback_query
    if not ui.authorized(q):
        await ui.answer(q)
        return
    try:
        _, token, choice = q.data.split(":")
    except ValueError:
        await ui.answer(q)
        return
    store = ui.pending_choices(context)
    data = store.get(token)
    if data is None:
        await ui.answer(q, "Expiré (le bot a redémarré). Refais la commande.", show_alert=True)
        return

    if choice == "u":
        await asyncio.to_thread(artists.remove, data["name"])
        store.pop(token, None)
        await ui.edit(q, f"↩️ {html.escape(data['name'])} retiré.", None)
        await ui.answer(q)
        return
    if choice == "x":
        store.pop(token, None)
        await ui.edit(q, "Annulé.", None)
        await ui.answer(q)
        return

    try:
        cand = data["cands"][int(choice)]
    except (KeyError, IndexError, ValueError):
        await ui.answer(q)
        return
    store.pop(token, None)
    await ui.edit(q, f"Choisi : {html.escape(cand['name'])} ({html.escape(cand['genre'])})", None)
    await ui.answer(q)
    # Plusieurs artistes portent ce nom : on force l'ID choisi.
    await _confirm_add(
        context,
        {"name": data["name"], "id": cand["id"], "matched": cand["name"], "genre": cand["genre"], "url": cand["url"], "forced": True},
    )


# --------------------------------------------------------------------------- #
# /retirer, /artistes
# --------------------------------------------------------------------------- #

async def cmd_retirer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("Usage : /retirer Nom de l'artiste")
        return
    res = await asyncio.to_thread(artists.remove, query)
    if res["status"] == "removed":
        names = ", ".join(html.escape(n) for n in res["names"])
        await ui.send(context.bot, f"🗑 Retiré : <b>{names}</b>. Il n'est plus suivi.")
    elif res["status"] == "ambiguous":
        listing = "\n".join(f"• {html.escape(n)}" for n in res["candidates"])
        await ui.send(context.bot, f"Plusieurs artistes correspondent, donne le nom exact :\n{listing}")
    else:
        await ui.send(context.bot, "Aucun artiste suivi ne correspond. /artistes pour voir la liste.")


async def cmd_artistes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    needle = " ".join(context.args).strip() if context.args else ""
    names = await asyncio.to_thread(artists.matching, needle)
    if not names:
        await update.message.reply_text("Aucun artiste.")
        return
    body = " · ".join(html.escape(n) for n in names)
    await ui.send_all(context.bot, formatting.split_for_telegram([f"<b>{len(names)} artiste(s)</b>", body]))


# --------------------------------------------------------------------------- #
# /verifier : artistes dont le nom est porté par plusieurs artistes chez Apple
# --------------------------------------------------------------------------- #

def _artist_card(info: dict, followed: str) -> str:
    name = html.escape(info["name"])
    link = f'<a href="{html.escape(info["url"], quote=True)}">{name}</a>' if info.get("url") else name
    return f"<b>{html.escape(followed)}</b> → {link} — {html.escape(info['genre'])}\n<i>Dernière sortie : {html.escape(info['last'])}</i>"


def _verify_keyboard(artist_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [ui.btn("✅ C'est le bon", f"vf:ok:{artist_id}"), ui.btn("🔄 Changer", f"vf:ch:{artist_id}")],
            [ui.btn("🗑 Ne plus suivre", f"vf:rm:{artist_id}")],
        ]
    )


async def cmd_verifier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ui.reply_wait(update, "⏳ Recherche des artistes au nom ambigu (quelques minutes la première fois)…")
    try:
        # Sous le verrou : pas d'appels Apple en parallèle d'un scan (limite de débit).
        async with ui.scan_lock:
            items = await asyncio.to_thread(artists.ambiguous)
    except Exception as exc:  # noqa: BLE001
        await ui.notify_error(context.bot, "/verifier", exc)
        return
    if not items:
        await ui.send(context.bot, "✅ Aucun artiste à vérifier.")
        return
    await ui.send(
        context.bot,
        f"🔎 <b>{len(items)} artiste(s) à vérifier</b> : plusieurs artistes portent ce nom chez Apple. "
        "Vérifie que le bot suit le bon (genre, dernière sortie, lien).",
    )
    for info in items:
        await ui.send(context.bot, _artist_card(info, info["followed"]), _verify_keyboard(info["id"]), silent=True)
        await asyncio.sleep(settings.SEND_PAUSE_S)


async def on_verify_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vf:ok|ch|rm:<id>  ·  vf:pk:<jeton>:<n° d'homonyme>."""
    q = update.callback_query
    if not ui.authorized(q):
        await ui.answer(q)
        return
    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "pk" and len(parts) == 4:
        data = ui.pending_choices(context).pop(parts[2], None)
        if data is None:
            await ui.answer(q, "Expiré (le bot a redémarré). Refais /verifier.", show_alert=True)
            return
        try:
            cand = data["cands"][int(parts[3])]
        except (IndexError, ValueError):
            await ui.answer(q)
            return
        name = await asyncio.to_thread(artists.switch, data["old"], cand["id"], cand["name"])
        await ui.edit(q, f"🔄 <b>{html.escape(name or '?')}</b> suit maintenant :\n{_artist_card(cand, name or '?')}", None)
        await ui.answer(q, "Artiste changé")
        return

    try:
        artist_id = int(parts[2])
    except (IndexError, ValueError):
        await ui.answer(q)
        return
    name = await asyncio.to_thread(storage.followed_name, artist_id)
    if name is None:
        await ui.edit(q, "Cet artiste n'est plus suivi.", None)
        await ui.answer(q)
        return

    if action == "ok":
        await asyncio.to_thread(storage.mark_verified, artist_id)
        await ui.edit(q, f"✅ <b>{html.escape(name)}</b> : confirmé.", None)
    elif action == "rm":
        await asyncio.to_thread(artists.remove, name)
        await asyncio.to_thread(storage.drop_pending_of, artist_id)
        await ui.edit(q, f"🗑 <b>{html.escape(name)}</b> n'est plus suivi.", None)
    elif action == "ch":
        await ui.answer(q, "Recherche des homonymes…")
        cands = await asyncio.to_thread(artists.homonyms, artist_id)
        if not cands:
            await ui.edit(q, f"Aucun autre « {html.escape(name)} » trouvé chez Apple.", _verify_keyboard(artist_id))
            return
        token = ui.remember(context, {"old": artist_id, "cands": cands})
        lines = [f"Quel « {html.escape(name)} » veux-tu suivre ?"]
        lines += [f"{i}. {_artist_card(c, name)}" for i, c in enumerate(cands, 1)]
        buttons = [ui.btn(str(i), f"vf:pk:{token}:{i - 1}") for i in range(1, len(cands) + 1)]
        buttons.append(ui.btn("↩️ Garder l'actuel", f"vf:ok:{artist_id}"))
        await ui.edit(q, "\n".join(lines), InlineKeyboardMarkup([buttons]))
        return
    await ui.answer(q)
