"""
Analyse de nouveauté : qu'est-ce que cette sortie t'apporte vraiment ?

- Featuring : rien à analyser (sa note est posée au scan).
- Version alternative (slowed, remix…) : de quel son elle est la version.
- Album / EP : combien de titres sont inédits, en comparant avec les singles
  déjà sortis, l'album d'origine d'une réédition et les albums déjà analysés.
  Sans référence, aucune affirmation n'est faite.
"""

from __future__ import annotations

import logging

from . import config, storage, text
from .models import KIND_FEATURING, Release
from .sources import itunes

logger = logging.getLogger("radar.analysis")


def find_original(r: Release, catalog: dict[int, list[Release]]) -> str:
    """
    Cherche l'original d'une variante chez le même artiste : une sortie du même
    titre (catalogue récent), sinon un titre d'album déjà analysé. Retourne un
    texte prêt à afficher, ou "" si rien n'est trouvé.
    """
    want = text.norm_title(text.variant_base(r.title))
    if not want:
        return ""
    candidates = sorted(
        (
            x for x in catalog.get(r.artist_id, [])
            if x.album_id != r.album_id and not x.is_variant
            and want in (text.norm_title(x.title), text.norm_title(text.split_edition(x.title)[0]))
        ),
        key=lambda x: x.release_date,
    )
    if candidates:
        x = candidates[0]
        return f"de « {x.title} » ({x.kind.lower()} du {x.release_date:%d/%m/%Y})"
    for track, album in storage.tracks_with_album(r.artist_id, excluding=r.album_id):
        if text.norm_title(track) == want and not text.is_variant(album):
            return f"de « {track} » (album « {album} »)"
    return ""


def analyze(r: Release, catalog: dict[int, list[Release]]) -> None:
    """Remplit r.note / r.new_tracks / r.total_tracks (voir le docstring du module)."""
    r.analyzed = True
    if r.kind == KIND_FEATURING:
        return
    base_title, is_edition = text.split_edition(r.title)

    if r.is_variant:
        origin = find_original(r, catalog)
        r.note = f"🔁 version {text.variant_label(r.title)}" + (f" {origin}" if origin else "")
        return
    if r.kind == "Single" or not config.NOVELTY_ENABLED:
        return

    try:
        tracks = itunes.tracks_of(r.album_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Liste des titres de '%s' indisponible (%s)", r.title, exc)
        r.analyzed = False  # on réessaiera à la prochaine occasion
        return
    if not tracks:
        return
    r.total_tracks = len(tracks)

    catalog_rels = catalog.get(r.artist_id, [])
    prior = [x for x in catalog_rels if x.album_id != r.album_id and x.release_date < r.release_date]
    from_singles = {text.norm_title(x.title) for x in prior if x.kind == "Single"}

    # Album d'origine : Apple date parfois la réédition du même jour que
    # l'original, d'où le <= (et non <) ici.
    same_or_before = [
        x for x in catalog_rels
        if x.album_id != r.album_id and x.release_date <= r.release_date and x.kind != "Single" and not x.is_variant
    ]
    base_norms: set[str] = set()
    base_found = False
    want = text.norm_title(base_title)
    if is_edition:
        matches = sorted(
            (x for x in same_or_before if text.norm_title(text.split_edition(x.title)[0]) == want),
            key=lambda x: (x.is_edition, x.release_date),  # l'édition standard d'abord
        )
    else:
        # Réédition sans mot-clé ("Album 2.0", "Album: Reloaded") : un album
        # précédent dont le titre est le début de celui-ci, et qui partage
        # au moins la moitié de ses titres.
        matches = sorted(
            (x for x in same_or_before if not x.is_edition and want.startswith(text.norm_title(x.title) + " ")),
            key=lambda x: x.release_date,
        )
    if matches:
        try:
            base_norms = {text.norm_title(t) for t in itunes.tracks_of(matches[0].album_id)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Album d'origine de '%s' indisponible (%s)", r.title, exc)
        overlap = sum(1 for t in tracks if text.norm_title(t) in base_norms)
        if is_edition:
            base_found = bool(base_norms)
        elif base_norms and overlap * 2 >= len(base_norms):
            base_found = is_edition = True
        else:
            base_norms = set()

    from_db = {text.norm_title(t) for t in storage.tracks_of_artist_before(r.artist_id, r.album_id, r.release_date)}
    known = from_singles | base_norms | from_db
    norms = [text.norm_title(t) for t in tracks]
    known_count = sum(1 for t in norms if t in known)
    new_count = len(tracks) - known_count
    only_singles = all(t in from_singles for t in norms if t in known)
    already = f"{known_count} déjà sorti{'s' if known_count > 1 else ''}" + (" en single" if only_singles else "")

    if is_edition and base_found:
        r.new_tracks = new_count
        r.note = (
            "♻️ réédition · aucun inédit"
            if new_count == 0
            else f"♻️ réédition · {text.plural(new_count, 'inédit', 'inédits')} sur {len(tracks)}"
        )
    elif known_count:
        r.new_tracks = new_count
        if new_count == 0:
            r.note = f"🔁 aucun inédit · {already}"
        else:
            r.note = f"🆕 {text.plural(new_count, 'inédit', 'inédits')} sur {len(tracks)} · {already}"
    elif is_edition:
        r.note = "♻️ réédition"
