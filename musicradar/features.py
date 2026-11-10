"""
Featurings sur le projet d'un autre artiste (ex. « TMTC » de Yung Miami avec
Quavo). Apple ne les range pas dans les sorties de l'artiste invité.

Deux sources, dans cet ordre :
1. MusicBrainz : les invités y sont reliés par identifiant (fiable, même pour
   un nom porté par plusieurs artistes), puis le titre est retrouvé chez Apple.
2. Recherche par nom chez Apple, pour les artistes absents de MusicBrainz et
   seulement si leur nom est porté par un seul artiste (sinon : confusions).

MusicBrainz et Apple ont chacun leur limite de débit : MusicBrainz est
interrogé dans un thread à part (`MusicBrainzPrefetch`), pendant que le scan
lit les catalogues chez Apple.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

from . import clock, config, text
from .models import KIND_FEATURING, Release
from .sources import itunes, musicbrainz

logger = logging.getLogger("radar.features")

def release_from_track(t: dict, artist_id: int, name: str) -> Release:
    """Sortie « Featuring » à partir d'un titre Apple (résultat de recherche)."""
    released = datetime.fromisoformat(t["releaseDate"].replace("Z", "+00:00")).astimezone(config.TZ).date()
    kind, album = text.kind_and_title(t.get("collectionName", ""))
    return Release(
        album_id=t["trackId"],
        artist=t.get("artistName", "?"),
        title=t["trackName"],
        kind=KIND_FEATURING,
        release_date=released,
        url=t.get("trackViewUrl", ""),
        artist_id=artist_id,
        cover=itunes.cover_url(t.get("artworkUrl100", "")),
        explicit=t.get("trackExplicitness") == "explicit",
        note=f"🎤 {name} en featuring · {kind.lower()} « {album} »",
        analyzed=True,
    )


def from_apple_search(
    artist_id: int,
    name: str,
    since: date,
    known_albums: set[int],
    known_titles: text.TitleIndex,
) -> list[Release]:
    """
    Titres où l'artiste est crédité (dans le titre ou l'interprète) sur le
    projet d'un autre. Apple classe par popularité : pour un artiste très
    prolifique, un feat récent peut ne pas sortir dans les 200 résultats.
    Les singles en featuring sont déjà dans ses sorties : on ne garde que les
    titres d'albums et d'EP.
    """
    key = text.cmp_key(name)
    out: list[Release] = []
    for t in itunes.search_songs(name, limit=200):
        try:
            if t.get("artistId") == artist_id:
                continue  # titre de l'artiste lui-même : déjà dans ses sorties
            if t.get("collectionId") in known_albums or t.get("collectionName", "").endswith(" - Single"):
                continue
            if key not in text.credited(t.get("trackName", ""), t.get("artistName", "")):
                continue  # le nom apparaît dans le titre sans être un crédit
            if text.already_out(known_titles, t.get("artistName", ""), t.get("trackName", "")):
                continue  # déjà sorti en single (et donc déjà notifié comme tel)
            rel = release_from_track(t, artist_id, name)
            if rel.release_date >= since:
                out.append(rel)
        except (KeyError, ValueError) as exc:
            logger.warning("Titre ignoré (%s) : %s", exc, t.get("trackName"))
    return out


def _apple_track(title: str, primary: str) -> dict | None:
    """Retrouve chez Apple un titre repéré dans MusicBrainz (lien, pochette, date)."""
    want, who = text.norm_title(title), text.cmp_key(primary)
    for t in itunes.search_songs(f"{primary} {title}", limit=15):
        if text.norm_title(t.get("trackName", "")) == want and who in text.credited(
            t.get("trackName", ""), t.get("artistName", "")
        ):
            return t
    return None


class MusicBrainzPrefetch:
    """
    Interroge MusicBrainz pour chaque artiste suivi, dans l'ordre, sans attendre
    que le scan en ait besoin : fiche de l'artiste (MBID) puis enregistrements
    où il est crédité. Un seul thread, donc une requête à la fois.

    Coupe-circuit : après MB_MAX_FAILURES échecs d'affilée, les artistes
    suivants ne sont plus demandés (la recherche Apple prend le relais).
    """

    def __init__(self, names: dict[int, str], since: date) -> None:
        self._since = since
        self._until = clock.today() + timedelta(days=config.UPCOMING_DAYS)
        self._failures = 0  # lu et modifié par le seul thread de travail
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="musicbrainz")
        self._futures: dict[int, Future] = {}
        if config.MUSICBRAINZ_ENABLED:
            self._futures = {aid: self._pool.submit(self._fetch, aid, name) for aid, name in names.items()}

    def _fetch(self, artist_id: int, name: str) -> tuple[str | None, list[dict]] | None:
        if self._failures >= config.MB_MAX_FAILURES:
            return None
        try:
            mbid = musicbrainz.mbid_for(artist_id, name)
            recordings = musicbrainz.recordings_crediting(mbid, self._since, self._until) if mbid else []
        except requests.RequestException:
            self._failures += 1
            raise
        self._failures = 0
        return mbid, recordings

    def result(self, artist_id: int) -> tuple[str | None, list[dict]] | None:
        """
        (MBID ou None, enregistrements) de l'artiste, en attendant si besoin que
        son tour soit passé. None si MusicBrainz n'a pas été interrogé (désactivé
        ou coupe-circuit). Relève l'erreur réseau rencontrée, le cas échéant.
        """
        future = self._futures.get(artist_id)
        return future.result() if future else None

    def close(self) -> None:
        """Abandonne ce qui n'a pas commencé et attend la fin de la requête en cours."""
        self._pool.shutdown(wait=True, cancel_futures=True)


def from_musicbrainz(
    artist_id: int,
    name: str,
    mbid: str,
    recordings: list[dict],
    since: date,
    known_albums: set[int],
    known_titles: text.TitleIndex,
) -> list[Release]:
    """
    Titres récents où l'artiste est crédité sans être l'artiste principal,
    d'après MusicBrainz (`recordings`), puis retrouvés chez Apple. La première
    date de sortie doit être dans la période (une vieille chanson sur une
    compilation récente ne compte pas).
    """
    out: list[Release] = []
    seen_titles: set[tuple[str, str]] = set()
    for rec in recordings:
        credits = rec.get("artist-credit", [])
        if not credits or credits[0].get("artist", {}).get("id") == mbid:
            continue  # titre de l'artiste lui-même : déjà dans ses sorties Apple
        if mbid not in {c.get("artist", {}).get("id") for c in credits}:
            continue
        first = rec.get("first-release-date", "")
        if len(first) < 10 or first < since.isoformat():
            continue
        primary = credits[0].get("name") or credits[0].get("artist", {}).get("name", "")
        ident = (text.cmp_key(primary), text.norm_title(rec.get("title", "")))
        if ident in seen_titles or text.already_out(known_titles, primary, rec.get("title", "")):
            continue
        seen_titles.add(ident)
        try:
            t = _apple_track(rec["title"], primary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Recherche Apple de « %s » impossible : %s", rec.get("title"), exc)
            continue
        if t is None or t.get("collectionId") in known_albums or t.get("artistId") == artist_id:
            continue
        if text.already_out(known_titles, t.get("artistName", ""), t.get("trackName", "")):
            continue  # même titre déjà connu sous une autre fiche Apple
        rel = release_from_track(t, artist_id, name)
        if rel.release_date >= since:  # pas une vieille version du titre chez Apple
            out.append(rel)
    return out


def find(
    names: dict[int, str],
    since: date,
    known: list[Release],
    already_found: set[int],
    prefetch: MusicBrainzPrefetch | None = None,
) -> tuple[list[Release], int]:
    """
    Featurings de tous les artistes suivis. `known` : sorties déjà connues
    (catalogues + base), pour ne pas renvoyer un feat déjà notifié en single.
    `prefetch` : interrogation de MusicBrainz déjà lancée par le scan (sinon
    elle est lancée ici). Retourne (featurings, nombre d'erreurs).
    """
    own_prefetch = prefetch is None
    prefetch = prefetch or MusicBrainzPrefetch(names, since)
    try:
        return _find(names, since, known, already_found, prefetch)
    finally:
        if own_prefetch:
            prefetch.close()


def _find(
    names: dict[int, str],
    since: date,
    known: list[Release],
    already_found: set[int],
    prefetch: MusicBrainzPrefetch,
) -> tuple[list[Release], int]:
    known_albums = already_found | {rel.album_id for rel in known}
    known_titles = text.title_index((r.title, r.artist) for r in known if r.kind != KIND_FEATURING)
    found: list[Release] = []
    errors = 0
    mb_failures = 0
    for aid, name in names.items():
        try:
            reliable: bool | None = itunes.name_is_unique(aid, name)
        except Exception as exc:  # noqa: BLE001
            reliable = None  # inconnu : pas de recherche par nom
            logger.warning("Vérification du nom de %s impossible (%s)", name, exc)

        feats: list[Release] | None = None
        try:
            answer = prefetch.result(aid)
            if answer is not None:
                mbid, recordings = answer
                if mbid:
                    feats = from_musicbrainz(aid, name, mbid, recordings, since, known_albums, known_titles)
                mb_failures = 0
        except requests.RequestException as exc:
            mb_failures += 1
            logger.warning("MusicBrainz indisponible pour %s (%s)", name, exc)
            if mb_failures == config.MB_MAX_FAILURES:
                logger.warning("MusicBrainz ne répond plus : recherche Apple pour la suite du scan")
        try:
            if feats is None and reliable:
                feats = from_apple_search(aid, name, since, known_albums, known_titles)
            found += feats or []
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.error("Featurings de %s : échec de la recherche (%s)", name, exc)

    return found, errors
