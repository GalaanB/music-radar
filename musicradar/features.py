"""
Featurings sur le projet d'un autre artiste (ex. « TMTC » de Yung Miami avec
Quavo). Apple ne les range pas dans les sorties de l'artiste invité.

Deux sources, dans cet ordre :
1. MusicBrainz : les invités y sont reliés par identifiant (fiable, même pour
   un nom porté par plusieurs artistes), puis le titre est retrouvé chez Apple.
2. Recherche par nom chez Apple, pour les artistes absents de MusicBrainz et
   seulement si leur nom est porté par un seul artiste (sinon : confusions).
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import requests

from . import clock, config, storage, text
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
    time.sleep(config.REQUEST_DELAY_S)
    want, who = text.norm_title(title), text.cmp_key(primary)
    for t in itunes.search_songs(f"{primary} {title}", limit=15):
        if text.norm_title(t.get("trackName", "")) == want and who in text.credited(
            t.get("trackName", ""), t.get("artistName", "")
        ):
            return t
    return None


def from_musicbrainz(
    artist_id: int,
    name: str,
    mbid: str,
    since: date,
    known_albums: set[int],
    known_titles: text.TitleIndex,
) -> list[Release]:
    """
    Titres récents où l'artiste est crédité sans être l'artiste principal,
    d'après MusicBrainz, puis retrouvés chez Apple. La première date de sortie
    doit être dans la période (une vieille chanson sur une compilation récente
    ne compte pas).
    """
    until = clock.today() + timedelta(days=config.UPCOMING_DAYS)
    out: list[Release] = []
    seen_titles: set[tuple[str, str]] = set()
    for rec in musicbrainz.recordings_crediting(mbid, since, until):
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
) -> tuple[list[Release], int]:
    """
    Featurings de tous les artistes suivis. `known` : sorties déjà connues
    (catalogues + base), pour ne pas renvoyer un feat déjà notifié en single.
    Retourne (featurings, nombre d'erreurs).
    """
    known_albums = already_found | {rel.album_id for rel in known}
    known_titles = text.title_index((r.title, r.artist) for r in known if r.kind != KIND_FEATURING)
    found: list[Release] = []
    unreliable: list[int] = []
    errors = 0
    mb_failures = 0
    for aid, name in names.items():
        try:
            reliable: bool | None = itunes.name_is_unique(aid, name)
        except Exception as exc:  # noqa: BLE001
            reliable = None  # inconnu : ni purge, ni recherche par nom
            logger.warning("Vérification du nom de %s impossible (%s)", name, exc)
        if reliable is False:
            unreliable.append(aid)  # ses anciens featurings « par nom » sont douteux

        feats: list[Release] | None = None
        if config.MUSICBRAINZ_ENABLED and mb_failures < config.MB_MAX_FAILURES:
            try:
                mbid = musicbrainz.mbid_for(aid, name)
                if mbid:
                    feats = from_musicbrainz(aid, name, mbid, since, known_albums, known_titles)
                mb_failures = 0
            except requests.RequestException as exc:
                mb_failures += 1
                logger.warning("MusicBrainz indisponible pour %s (%s)", name, exc)
                if mb_failures == config.MB_MAX_FAILURES:
                    logger.warning("MusicBrainz ne répond plus : recherche Apple pour la suite du scan")
        try:
            if feats is None and reliable:
                time.sleep(config.REQUEST_DELAY_S)
                feats = from_apple_search(aid, name, since, known_albums, known_titles)
            found += feats or []
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.error("Featurings de %s : échec de la recherche (%s)", name, exc)

    storage.drop_pending_features(unreliable)
    return found, errors
