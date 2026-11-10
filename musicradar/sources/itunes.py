"""
Client de l'API publique iTunes / Apple Music (sans clé).

Toutes les requêtes passent par `get_json`, qui espace les appels pour rester
sous la limite de débit non officielle (~20 requêtes/minute), y compris quand
plusieurs threads appellent Apple en même temps (scan + /ajouter).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import requests

from .. import config, storage, text
from ..models import Release
from ..ratelimit import RateLimiter

logger = logging.getLogger("radar.itunes")

_limiter = RateLimiter(lambda: config.REQUEST_DELAY_S)


def get_json(url: str, params: dict) -> dict:
    """GET espacé du précédent, avec 1 retry en cas d'erreur (403/429/réseau) après une pause."""
    for attempt in (1, 2):
        _limiter.wait()
        try:
            resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT_S)
            if resp.status_code in (403, 429):
                raise requests.HTTPError(f"HTTP {resp.status_code} (rate limit ?)")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise
            logger.warning("Appel iTunes échoué (%s), nouvelle tentative dans 30 s", exc)
            time.sleep(30)
    return {}  # inatteignable, pour le typage


def cover_url(artwork_url: str) -> str:
    """Pochette en 300 x 300 au lieu des 100 x 100 renvoyés par défaut."""
    return re.sub(r"/\d+x\d+bb", "/300x300bb", artwork_url)


# --------------------------------------------------------------------------- #
# Artistes
# --------------------------------------------------------------------------- #

def search_artist(name: str) -> tuple[int, str, str, bool] | None:
    """
    Retourne (id, nom trouvé, genre principal, ambigu) ou None.
    `ambigu` = plusieurs artistes distincts du catalogue portent exactement ce nom.
    """
    data = get_json(config.ITUNES_SEARCH, {"term": name, "entity": "musicArtist", "limit": 5, "country": config.COUNTRY})
    results = data.get("results", [])
    if not results:
        return None
    # Priorité à la correspondance exacte (insensible à la casse), sinon au 1er.
    exact = [r for r in results if r.get("artistName", "").casefold() == name.casefold()]
    chosen = exact[0] if exact else results[0]
    # Le même artiste peut remonter plusieurs fois avec le même ID : on compte
    # les ID distincts pour ne pas signaler un faux homonyme.
    distinct_exact = {r.get("artistId") for r in exact}
    return (chosen["artistId"], chosen["artistName"], chosen.get("primaryGenreName", "?"), len(distinct_exact) > 1)


def search_artists(term: str, limit: int = 5) -> list[dict]:
    """Candidats d'une recherche, dédoublonnés par ID : [{id, name, genre, url}]."""
    data = get_json(config.ITUNES_SEARCH, {"term": term, "entity": "musicArtist", "limit": 10, "country": config.COUNTRY})
    out: list[dict] = []
    seen_ids: set[int] = set()
    for r in data.get("results", []):
        aid = r.get("artistId")
        if not aid or aid in seen_ids:
            continue
        seen_ids.add(aid)
        out.append(
            {
                "id": aid,
                "name": r.get("artistName", "?"),
                "genre": r.get("primaryGenreName", "?"),
                "url": r.get("artistLinkUrl", ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def lookup_artist(artist_id: int) -> dict | None:
    data = get_json(config.ITUNES_LOOKUP, {"id": artist_id, "country": config.COUNTRY})
    for r in data.get("results", []):
        if r.get("wrapperType") == "artist":
            return {
                "id": r.get("artistId", artist_id),
                "name": r.get("artistName", "?"),
                "genre": r.get("primaryGenreName", "?"),
                "url": r.get("artistLinkUrl", ""),
            }
    return None


def artist_overview(artist_id: int) -> dict:
    """Genre, lien et dernière sortie d'un artiste, en un seul appel."""
    data = get_json(
        config.ITUNES_LOOKUP,
        {"id": artist_id, "entity": "album", "limit": config.ALBUMS_PER_ARTIST, "country": config.COUNTRY},
    )
    info: dict = {"id": artist_id, "name": "?", "genre": "?", "url": "", "last": "aucune sortie"}
    latest: tuple[str, str] | None = None
    for item in data.get("results", []):
        if item.get("wrapperType") == "artist":
            info.update(
                name=item.get("artistName", "?"),
                genre=item.get("primaryGenreName", "?"),
                url=item.get("artistLinkUrl", ""),
            )
        elif (
            item.get("wrapperType") == "collection" and item.get("releaseDate")
            and (latest is None or item["releaseDate"] > latest[0])
        ):
            latest = (item["releaseDate"], item.get("collectionName", "?"))
    if latest:
        info["last"] = f"{latest[1]} ({latest[0][8:10]}/{latest[0][5:7]}/{latest[0][:4]})"
    return info


def name_is_unique(artist_id: int, name: str) -> bool:
    """
    Un featuring repéré par recherche n'est identifié que par le nom écrit dans
    le titre (« feat. SAM ») : Apple ne donne pas l'ID de l'invité. Si plusieurs
    artistes portent ce nom chez Apple (SAM, Luv, Dau…), on ne peut pas savoir
    lequel est crédité. Vérifié une fois, puis tous les 30 jours. Si le nom
    devient ambigu, les featurings non triés de l'artiste sont retirés de /trier
    (voir storage.save_name_reliability).
    """
    cached = storage.cached_name_reliability(artist_id)
    if cached is not None:
        return cached
    data = get_json(config.ITUNES_SEARCH, {"term": name, "entity": "musicArtist", "limit": 25, "country": config.COUNTRY})
    key = text.cmp_key(name)
    same_name = {r.get("artistId") for r in data.get("results", []) if text.cmp_key(r.get("artistName", "")) == key}
    reliable = same_name <= {artist_id}
    storage.save_name_reliability(artist_id, reliable)
    if not reliable:
        logger.info("Nom « %s » porté par %d artistes chez Apple", name, len(same_name))
    return reliable


# --------------------------------------------------------------------------- #
# Sorties et titres
# --------------------------------------------------------------------------- #

def fetch_artist_releases(itunes_id: int) -> list[Release]:
    """Toutes les fiches (albums, EP, singles) qu'Apple associe à l'artiste."""
    data = get_json(
        config.ITUNES_LOOKUP,
        {
            "id": itunes_id,
            "entity": "album",
            "sort": "recent",
            "limit": config.ALBUMS_PER_ARTIST,
            "country": config.COUNTRY,
        },
    )
    releases: list[Release] = []
    for item in data.get("results", []):
        if item.get("wrapperType") != "collection":
            continue  # le 1er résultat est l'artiste lui-même
        try:
            kind, title = text.kind_and_title(item["collectionName"])
            released = datetime.fromisoformat(item["releaseDate"].replace("Z", "+00:00")).astimezone(config.TZ).date()
            releases.append(
                Release(
                    album_id=item["collectionId"],
                    artist=item.get("artistName", "?"),
                    title=title,
                    kind=kind,
                    release_date=released,
                    url=item["collectionViewUrl"],
                    artist_id=itunes_id,
                    cover=cover_url(item.get("artworkUrl100", "")),
                    track_count=int(item.get("trackCount") or 0),
                    explicit=item.get("collectionExplicitness") == "explicit",
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Entrée album ignorée (%s) : %s", exc, item.get("collectionName"))
    return releases


def recent_releases(artist_id: int, n: int = 3) -> list[Release]:
    """Les n sorties les plus récentes d'un artiste (pour vérifier que c'est le bon)."""
    rels = fetch_artist_releases(artist_id)
    return sorted(rels, key=lambda r: r.release_date, reverse=True)[:n]


def tracks_of(album_id: int) -> list[str]:
    """Titres d'un album : depuis le cache, sinon 1 appel."""
    cached = storage.cached_tracks(album_id)
    if cached:
        return cached
    data = get_json(config.ITUNES_LOOKUP, {"id": album_id, "entity": "song", "limit": 200, "country": config.COUNTRY})
    items = [
        (int(i.get("discNumber") or 1), int(i.get("trackNumber") or 0), i["trackName"])
        for i in data.get("results", [])
        if i.get("wrapperType") == "track" and i.get("trackName")
    ]
    items.sort(key=lambda x: (x[0], x[1]))
    titles = [t for _, _, t in items]
    if titles:
        storage.save_tracks(album_id, titles)
    return titles


def search_songs(term: str, limit: int) -> list[dict]:
    """Titres (entity=song) d'une recherche, classés par Apple (popularité)."""
    data = get_json(config.ITUNES_SEARCH, {"term": term, "entity": "song", "limit": limit, "country": config.COUNTRY})
    return [t for t in data.get("results", []) if t.get("wrapperType") == "track"]
