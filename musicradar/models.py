"""Modèle de données : une sortie, un lot de sorties, un envoi préparé."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import text

# États possibles d'une sortie dans ton tri.
STATE_NEW = "new"              # notifiée, pas encore triée
STATE_ADDED = "added"          # « Ajoutée » : rangée dans ta playlist Apple Music
STATE_TO_LISTEN = "to_listen"  # ancien bouton « À écouter » (converti en "added")
STATE_LISTENED = "listened"    # écoutée
STATE_IGNORED = "ignored"      # ignorée

KIND_FEATURING = "Featuring"   # titre où un artiste suivi est invité


@dataclass
class Release:
    album_id: int      # ID Apple de l'album (ou du titre, pour un featuring)
    artist: str
    title: str
    kind: str          # "Album", "EP", "Single" ou "Featuring"
    release_date: date
    url: str
    artist_id: int = 0  # ID Apple de l'artiste SUIVI qui a mené à cette sortie
    cover: str = ""
    track_count: int = 0
    # Résultat de l'analyse de nouveauté (None = pas de comparaison possible).
    total_tracks: int | None = None
    new_tracks: int | None = None
    note: str = ""
    detail: str = ""   # titres concernés par la note (« Déjà sortis : A · B »)
    state: str = STATE_NEW
    analyzed: bool = False
    explicit: bool = False  # version explicite (préférée à la version "clean")

    @property
    def dup_key(self) -> tuple[str, str, str]:
        """Même artiste + même type + même titre = même sortie, quel que soit l'ID."""
        return (text.cmp_key(self.artist), self.kind, text.norm_title(self.title))

    @property
    def is_edition(self) -> bool:
        return text.split_edition(self.title)[1]

    @property
    def nothing_new(self) -> bool:
        """Réédition dont tous les titres existent déjà dans l'album d'origine."""
        return self.new_tracks == 0 and self.note.startswith("♻️")

    @property
    def is_variant(self) -> bool:
        """Version slowed, sped up, instrumentale, remix… d'une autre sortie."""
        return text.is_variant(self.title, album=self.kind in ("Album", "EP"))


@dataclass
class Batch:
    """Résultat brut d'un scan."""
    releases: list[Release]                  # sorties datées d'aujourd'hui ou avant
    upcoming: list[Release]                  # sorties datées dans le futur proche
    catalog: dict[int, list[Release]] = field(default_factory=dict)  # par artiste suivi


@dataclass
class Prepared:
    """Lot analysé et trié, prêt à être envoyé."""
    to_show: list[Release]       # à afficher (pas encore triées, avec du neuf)
    nothing_new: list[Release]   # rééditions sans nouveauté (signalées en une ligne)
    already_sorted: int          # déjà triées par toi : on ne les remontre pas
    upcoming: list[Release]
    everything: list[Release]    # toutes, pour les marquer comme notifiées
