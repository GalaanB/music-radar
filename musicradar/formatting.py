"""Mise en forme des messages Telegram (HTML) : cartes, listes, rubriques."""

from __future__ import annotations

import html
from datetime import date

from . import config, text
from .models import KIND_FEATURING, Release


def _link(r: Release) -> str:
    return f'<a href="{html.escape(r.url, quote=True)}">{html.escape(r.title)}</a>'


def card_text(r: Release) -> str:
    """Carte d'une sortie : 2 lignes, plus la liste des titres déjà sortis s'il y en a."""
    kind = r.kind
    if r.kind != "Single" and r.total_tracks:
        kind += f" · {r.total_tracks} titres"
    meta = f"{html.escape(kind)} · {r.release_date:%d/%m/%Y}"
    if r.kind == KIND_FEATURING and r.note:
        meta = f"{r.release_date:%d/%m/%Y}"  # la note dit déjà « 🎤 X en featuring »
    if r.note:
        meta += f" · {html.escape(r.note)}"
    card = f"<b>{html.escape(r.artist)}</b> — {_link(r)}\n<i>{meta}</i>"
    if r.detail:
        card += f"\n↳ {html.escape(r.detail)}"
    return card


def _short_tag(r: Release) -> str:
    """Étiquette courte pour la liste compacte."""
    if r.nothing_new:
        return " · ♻️ rien de neuf"
    if r.new_tracks is not None and r.total_tracks:
        return f" · 🆕 {r.new_tracks}/{r.total_tracks}"
    if r.is_variant:
        return f" · 🔁 {text.variant_label(r.title)}"
    return ""


def split_for_telegram(lines: list[str]) -> list[str]:
    """Regroupe des lignes en messages < 4000 caractères, jamais au milieu d'une ligne."""
    messages: list[str] = []
    buffer = ""
    for line in lines:
        if len(buffer) + len(line) + 1 > config.TELEGRAM_MAX_LEN and buffer:
            messages.append(buffer)
            buffer = ""
        buffer += line + "\n"
    if buffer:
        messages.append(buffer)
    return messages


def compact_list(title: str, releases: list[Release], empty_text: str) -> list[str]:
    """Liste compacte, groupée par date puis triée par artiste."""
    if not releases:
        return [f"<b>{html.escape(title)}</b>\n\n{html.escape(empty_text)}"]
    lines = [f"<b>{html.escape(title)}</b> — {len(releases)} sortie(s)"]
    current_day: date | None = None
    for r in releases:
        if r.release_date != current_day:
            current_day = r.release_date
            lines.append(f"\n📅 <b>{current_day.strftime('%d/%m/%Y')}</b>")
        lines.append(f"• <b>{html.escape(r.artist)}</b> — {_link(r)} <i>({html.escape(r.kind)})</i>{_short_tag(r)}")
    return split_for_telegram(lines)


def nothing_new(releases: list[Release]) -> list[str]:
    if not releases:
        return []
    lines = ["♻️ <b>Rééditions sans nouveauté</b> (mises de côté, déjà dans l'album d'origine)"]
    lines += [f"• <b>{html.escape(r.artist)}</b> — {_link(r)}" for r in releases]
    return split_for_telegram(lines)


def upcoming(releases: list[Release]) -> list[str]:
    """Une ligne par sortie à venir, plafonnée pour rester discret."""
    if not releases:
        return []
    lines = ["🗓 <b>À venir</b>"]
    for r in releases[: config.UPCOMING_MAX_LINES]:
        lines.append(f"• {r.release_date:%d/%m} · <b>{html.escape(r.artist)}</b> — {_link(r)}")
    extra = len(releases) - config.UPCOMING_MAX_LINES
    if extra > 0:
        lines.append(f"<i>… et {extra} autre(s)</i>")
    return split_for_telegram(lines)
