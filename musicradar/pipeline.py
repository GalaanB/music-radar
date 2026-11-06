"""
D'un lot brut à un envoi : reprendre ton tri existant, analyser ce qui ne l'a
pas été, enregistrer, puis, une fois l'envoi réussi, marquer comme notifié.
"""

from __future__ import annotations

from . import analysis, storage
from .models import STATE_IGNORED, STATE_NEW, Batch, Prepared, Release


def _absorb_twins(rels: list[Release]) -> dict[int, str]:
    """
    Si la base contient une autre fiche de la même sortie (version "clean",
    doublon de catalogue), on la supprime au profit de celle du scan, en lui
    transmettant ton tri. Retourne {album_id: état hérité}.
    """
    by_key: dict[tuple[str, str, str], list[Release]] = {}
    for row in storage.all_releases():
        by_key.setdefault(row.dup_key, []).append(row)

    inherited: dict[int, str] = {}
    to_delete: list[int] = []
    for r in rels:
        twins = [t for t in by_key.get(r.dup_key, []) if t.album_id != r.album_id]
        if not twins:
            continue
        decided = next((t.state for t in twins if t.state != STATE_NEW), None)
        if decided:
            inherited[r.album_id] = decided
        to_delete += [t.album_id for t in twins]
    storage.delete_releases(to_delete)
    return inherited


def prepare(batch: Batch) -> Prepared:
    """
    Reprend ton tri existant, analyse ce qui ne l'a pas encore été (seulement
    les sorties non triées), enregistre le tout et sépare ce qui mérite d'être
    montré de ce qui ne t'apporte rien.
    """
    rels = batch.releases
    inherited = _absorb_twins(rels)
    existing = storage.load_releases([r.album_id for r in rels])
    for r in rels:
        old = existing.get(r.album_id)
        if old:
            r.state = old.state
            if old.analyzed:
                r.total_tracks, r.new_tracks, r.note, r.detail = old.total_tracks, old.new_tracks, old.note, old.detail
                r.analyzed = True
        if r.state == STATE_NEW and r.album_id in inherited:
            r.state = inherited[r.album_id]
    for r in rels:
        if r.state == STATE_NEW and not r.analyzed:
            analysis.analyze(r, batch.catalog)
    storage.save_releases(rels)
    # L'enregistrement ne touche jamais à l'état : on applique à part l'état hérité.
    for r in rels:
        if r.album_id in inherited:
            storage.set_state([r.album_id], r.state)

    pending = [r for r in rels if r.state == STATE_NEW]
    return Prepared(
        to_show=[r for r in pending if not r.nothing_new],
        nothing_new=[r for r in pending if r.nothing_new],
        already_sorted=len(rels) - len(pending),
        upcoming=batch.upcoming,
        everything=rels,
    )


def finalize(p: Prepared) -> None:
    """À appeler APRÈS l'envoi réussi : marque notifié, range les rééditions vides."""
    storage.mark_seen(p.everything)
    storage.set_state([r.album_id for r in p.nothing_new], STATE_IGNORED)
