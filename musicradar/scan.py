"""
Parcours de tous les artistes suivis et constitution des lots :
quotidien, vendredi (avec les sorties à venir) et rattrapage.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from . import clock, config, features, storage
from .models import Batch, Release
from .sources import itunes

logger = logging.getLogger("radar.scan")


def _rank(r: Release) -> tuple:
    """Entre deux fiches d'une même sortie : l'explicite, puis la plus complète, puis la 1re publiée."""
    return (not r.explicit, -r.track_count, r.release_date, r.album_id)


def dedupe(releases: list[Release]) -> list[Release]:
    """
    Apple publie souvent plusieurs fiches pour une même sortie (version
    explicite + version "clean", ou doublon de catalogue), avec des ID
    différents. On n'en garde qu'une par (artiste, type, titre), tant que
    leurs dates sont proches (voir Release.is_same_release).
    """
    groups: dict[tuple[str, str, str], list[Release]] = {}
    for r in releases:
        group = groups.setdefault(r.dup_key, [])
        for i, cur in enumerate(group):
            if r.is_same_release(cur):
                if _rank(r) < _rank(cur):
                    group[i] = r
                break
        else:
            group.append(r)
    keep = {id(r) for group in groups.values() for r in group}
    return [r for r in releases if id(r) in keep]


def scan(since: date) -> tuple[list[Release], dict[int, list[Release]]]:
    """
    Lit le catalogue de tous les artistes résolus, par paquets (une requête
    Apple toutes les ~3 s), puis cherche leurs featurings. Retourne les sorties dont
    la date est >= `since` (futures comprises) et le catalogue de chaque
    artiste, réutilisé par l'analyse de nouveauté sans appel de plus.
    """
    names = storage.followed_artists()
    if not names:
        raise RuntimeError("Aucun artiste résolu : lance d'abord /actualiser.")

    # MusicBrainz a sa propre limite de débit : on l'interroge dès maintenant,
    # en parallèle de la lecture des catalogues chez Apple.
    prefetch = features.MusicBrainzPrefetch(names, since) if config.FEATURES_ENABLED else None
    try:
        return _scan(names, since, prefetch)
    finally:
        if prefetch:
            prefetch.close()


def _scan(
    names: dict[int, str], since: date, prefetch: features.MusicBrainzPrefetch | None
) -> tuple[list[Release], dict[int, list[Release]]]:
    found: dict[int, Release] = {}
    catalog: dict[int, list[Release]] = {}
    errors = 0
    failures_in_a_row = 0
    ids = list(names)
    for start in range(0, len(ids), config.LOOKUP_BATCH_SIZE):
        chunk = ids[start : start + config.LOOKUP_BATCH_SIZE]
        grouped: dict[int, list[Release]] = {}
        if len(chunk) > 1:
            try:
                grouped = itunes.fetch_many_artist_releases(chunk)
            except Exception as exc:  # noqa: BLE001
                # Repli : chaque artiste du paquet est redemandé seul, avec le
                # décompte d'échecs habituel.
                logger.warning("Paquet de %d artistes illisible (%s) : reprise un par un", len(chunk), exc)
        for aid in chunk:
            if aid in grouped:
                rels = grouped[aid]
            else:
                try:
                    rels = itunes.fetch_artist_releases(aid)
                except Exception as exc:
                    errors += 1
                    failures_in_a_row += 1
                    logger.error("Artiste %s : échec de récupération (%s)", aid, exc)
                    if failures_in_a_row >= config.APPLE_MAX_FAILURES:
                        raise RuntimeError(
                            f"Apple ne répond plus ({failures_in_a_row} échecs d'affilée) : scan interrompu. "
                            "Il sera refait au prochain récap, rien ne sera perdu."
                        ) from exc
                    continue
            failures_in_a_row = 0
            catalog[aid] = rels
            for rel in rels:
                if rel.release_date >= since:
                    found[rel.album_id] = rel  # un feat. peut apparaître chez plusieurs artistes

    if config.FEATURES_ENABLED:
        known = [rel for rels in catalog.values() for rel in rels] + storage.all_releases()
        feats, feat_errors = features.find(names, since, known, set(found), prefetch)
        errors += feat_errors
        for rel in feats:
            found.setdefault(rel.album_id, rel)

    if errors:
        logger.warning("%d recherche(s) en erreur sur %d artistes", errors, len(names))
    unique = dedupe(list(found.values()))
    ordered = sorted(unique, key=lambda r: (r.release_date, r.artist.casefold()), reverse=True)
    return ordered, catalog


def _make_batch(since: date, upcoming_days: int = 0) -> Batch:
    found, catalog = scan(since)
    today = clock.today()
    past = [r for r in found if r.release_date <= today]
    future: list[Release] = []
    if upcoming_days:
        limit = today + timedelta(days=upcoming_days)
        future = sorted(
            (r for r in found if today < r.release_date <= limit),
            key=lambda r: (r.release_date, r.artist.casefold()),
        )
    return Batch(releases=past, upcoming=future, catalog=catalog)


def _only_unnotified(batch: Batch) -> Batch:
    seen = storage.seen_ids()
    # Une AUTRE fiche de la même sortie a déjà été notifiée (ex. la version
    # "clean" hier, l'explicite aujourd'hui) : pas de deuxième notification.
    # On ne regarde que les fiches réellement notifiées : une sortie enregistrée
    # lors d'un envoi qui a échoué doit repartir au récap suivant.
    notified: dict[tuple[str, str, str], list[Release]] = {}
    for row in storage.all_releases():
        if row.album_id in seen:
            notified.setdefault(row.dup_key, []).append(row)
    batch.releases = [
        r for r in batch.releases
        if r.album_id not in seen and not any(r.is_same_release(n) for n in notified.get(r.dup_key, []))
    ]
    return batch


def daily_batch() -> Batch:
    """
    Sorties des DAILY_LOOKBACK_DAYS derniers jours PAS ENCORE notifiées (Apple
    affiche parfois une sortie avec quelques jours de retard). Les sorties à
    date future (pré-sorties) sont ignorées et seront captées le jour J.
    """
    return _only_unnotified(_make_batch(clock.today() - timedelta(days=config.DAILY_LOOKBACK_DAYS)))


def weekly_batch() -> Batch:
    """
    Vendredi : comme le quotidien (qu'il remplace ce jour-là), plus les sorties
    annoncées pour les jours suivants. Les sorties de la semaine déjà reçues ne
    sont pas renvoyées ; le bot rappelle combien attendent encore dans /trier.
    """
    return _only_unnotified(
        _make_batch(clock.today() - timedelta(days=config.DAILY_LOOKBACK_DAYS), upcoming_days=config.UPCOMING_DAYS)
    )


def catchup_batch(days: int) -> Batch:
    """Toutes les sorties des `days` derniers jours (celles déjà triées sont filtrées ensuite)."""
    return _make_batch(clock.today() - timedelta(days=days))
