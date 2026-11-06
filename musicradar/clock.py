"""Date du jour dans le fuseau configuré (une sortie « du 31/10 » l'est à Paris)."""

from __future__ import annotations

from datetime import date, datetime

from . import config


def today() -> date:
    return datetime.now(config.TZ).date()
