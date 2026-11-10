"""
Configuration commune des tests.

Aucun test ne touche au réseau : l'API iTunes est remplacée par `FakeItunes`
et Telegram par des objets simulés. Chaque test a sa propre base SQLite et son
propre artists.txt dans un dossier temporaire.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# musicradar.bot.settings lit ces variables dès l'import.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("TELEGRAM_CHAT_ID", "42")
os.environ.setdefault("ITUNES_DELAY", "0")
os.environ.setdefault("RADAR_DATA_DIR", tempfile.mkdtemp(prefix="radar-tests-"))

from musicradar import clock, config, models  # noqa: E402
from musicradar.sources import itunes as itunes_api  # noqa: E402
from musicradar.sources import musicbrainz  # noqa: E402

SAMPLE_ARTISTS = """\
# Un artiste par ligne.
#   Nom | 123456789
$CO
21 Savage
Damso
Drake
Laylow
Lil Baby
Lil Tecca
Selug
Selug Archives
Travis Scott
Tyler, The Creator
"""


class FakeItunes:
    """Remplace itunes.get_json : répond à partir de dictionnaires."""

    def __init__(self) -> None:
        self.albums: dict[int, list[dict]] = {}       # artist_id -> fiches "collection"
        self.tracks: dict[int, list[str]] = {}        # album_id -> titres
        self.search: dict[str, list[dict]] = {}       # terme -> résultats "musicArtist"
        self.songs: dict[str, list[dict]] = {}        # terme -> résultats "song" (featurings)
        self.artists: dict[int, dict] = {}            # artist_id -> fiche "artist"
        self.no_block: set[int] = set()               # artistes absents d'une réponse groupée
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, params: dict) -> dict:
        self.calls.append((url, dict(params)))
        if url == config.ITUNES_SEARCH:
            source = self.songs if params.get("entity") == "song" else self.search
            return {"results": source.get(params["term"], [])}
        entity = params.get("entity")
        if entity == "album" and isinstance(params["id"], str):
            # Requête groupée « 1,2,3 » : un bloc par artiste (fiche artiste, puis ses albums).
            results: list[dict] = []
            for aid in map(int, params["id"].split(",")):
                if aid not in self.no_block:
                    results += [{"wrapperType": "artist", "artistId": aid}, *self.albums.get(aid, [])]
            return {"results": results}
        if entity == "album":
            return {"results": [{"wrapperType": "artist"}, *self.albums.get(params["id"], [])]}
        if entity == "song":
            titles = self.tracks.get(params["id"], [])
            return {
                "results": [{"wrapperType": "collection"}]
                + [
                    {"wrapperType": "track", "trackName": t, "trackNumber": i, "discNumber": 1}
                    for i, t in enumerate(titles, 1)
                ]
            }
        info = self.artists.get(params["id"])
        return {"results": [info] if info else []}


def collection(
    cid: int,
    name: str,
    days_ago: int = 0,
    artist: str = "Artiste",
    explicit: bool = True,
    tracks: int = 10,
) -> dict:
    """Fiche "collection" telle que la renvoie l'API iTunes."""
    day = clock.today() - timedelta(days=days_ago)
    return {
        "wrapperType": "collection",
        "collectionId": cid,
        "collectionName": name,
        "artistName": artist,
        "releaseDate": f"{day.isoformat()}T07:00:00Z",
        "collectionViewUrl": f"https://music.apple.com/fr/album/{cid}",
        "artworkUrl100": f"https://img/{cid}/100x100bb.jpg",
        "trackCount": tracks,
        "collectionExplicitness": "explicit" if explicit else "cleaned",
    }


def song(
    track_id: int,
    track: str,
    artist: str,
    album: str,
    album_id: int,
    days_ago: int = 0,
    artist_id: int = 999,
) -> dict:
    """Résultat "track" d'une recherche iTunes (entity=song)."""
    day = clock.today() - timedelta(days=days_ago)
    return {
        "wrapperType": "track",
        "trackId": track_id,
        "trackName": track,
        "artistName": artist,
        "artistId": artist_id,
        "collectionId": album_id,
        "collectionName": album,
        "releaseDate": f"{day.isoformat()}T07:00:00Z",
        "trackViewUrl": f"https://music.apple.com/fr/album/{album_id}?i={track_id}",
        "artworkUrl100": f"https://img/{album_id}/100x100bb.jpg",
        "trackExplicitness": "explicit",
    }


def release(
    album_id: int,
    title: str,
    kind: str = "Album",
    days_ago: int = 0,
    artist_id: int = 1,
    artist: str = "Artiste",
    **kw,
) -> models.Release:
    return models.Release(
        album_id=album_id,
        artist=artist,
        title=title,
        kind=kind,
        release_date=clock.today() - timedelta(days=days_ago),
        url=f"https://music.apple.com/fr/album/{album_id}",
        artist_id=artist_id,
        cover=f"https://img/{album_id}/300x300bb.jpg",
        **kw,
    )


def _no_network(url: str, params: dict) -> dict:
    raise AssertionError(f"Appel réseau non simulé : {url} {params}")


class FakeMusicBrainz:
    """Remplace musicbrainz.get. Vide par défaut : aucun artiste trouvé."""

    def __init__(self) -> None:
        self.artists: dict[str, list[dict]] = {}       # nom cherché -> fiches artiste
        self.apple_links: dict[str, list[int]] = {}    # mbid -> IDs Apple Music liés
        self.recordings: dict[str, list[dict]] = {}    # mbid -> enregistrements
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, params: dict) -> dict:
        self.calls.append((path, dict(params)))
        if path == "artist":
            name = params["query"].split(":", 1)[1].strip('"')
            return {"artists": self.artists.get(name, [])}
        if path.startswith("artist/"):
            mbid = path.split("/", 1)[1]
            urls = [{"url": {"resource": f"https://music.apple.com/us/artist/{i}"}} for i in self.apple_links.get(mbid, [])]
            return {"relations": urls}
        if path == "recording":
            mbid = params["query"].split()[0].split(":", 1)[1]
            recs = self.recordings.get(mbid, [])
            return {"count": len(recs), "recordings": recs[params.get("offset", 0):]}
        raise AssertionError(path)


def recording(title: str, credits: list[tuple[str, str]], first_release: str) -> dict:
    """Enregistrement MusicBrainz : credits = [(nom, mbid), ...], le 1er est l'artiste principal."""
    return {
        "title": title,
        "first-release-date": first_release,
        "artist-credit": [{"name": n, "artist": {"id": i, "name": n}, "joinphrase": " & "} for n, i in credits],
    }


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Base, artists.txt et réseau isolés pour chaque test."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    artists_file = tmp_path / "artists.txt"
    artists_file.write_text(SAMPLE_ARTISTS, encoding="utf-8")
    monkeypatch.setattr(config, "ARTISTS_FILE", artists_file)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(itunes_api, "get_json", _no_network)
    monkeypatch.setattr(musicbrainz, "get", FakeMusicBrainz())
    return tmp_path


@pytest.fixture
def mb(monkeypatch: pytest.MonkeyPatch) -> FakeMusicBrainz:
    fake = FakeMusicBrainz()
    monkeypatch.setattr(musicbrainz, "get", fake)
    return fake


@pytest.fixture
def itunes(monkeypatch: pytest.MonkeyPatch) -> FakeItunes:
    fake = FakeItunes()
    monkeypatch.setattr(itunes_api, "get_json", fake)
    return fake
