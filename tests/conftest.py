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
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# bot.py lit ces variables dès l'import.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("TELEGRAM_CHAT_ID", "42")
os.environ.setdefault("ITUNES_DELAY", "0")
os.environ.setdefault("RADAR_DATA_DIR", tempfile.mkdtemp(prefix="radar-tests-"))

import radar  # noqa: E402

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
    """Remplace radar._get_json : répond à partir de dictionnaires."""

    def __init__(self) -> None:
        self.albums: dict[int, list[dict]] = {}       # artist_id -> fiches "collection"
        self.tracks: dict[int, list[str]] = {}        # album_id -> titres
        self.search: dict[str, list[dict]] = {}       # terme -> résultats "musicArtist"
        self.artists: dict[int, dict] = {}            # artist_id -> fiche "artist"
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, params: dict) -> dict:
        self.calls.append((url, dict(params)))
        if url == radar.ITUNES_SEARCH:
            return {"results": self.search.get(params["term"], [])}
        entity = params.get("entity")
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
    day = radar._today() - timedelta(days=days_ago)
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


def release(
    album_id: int,
    title: str,
    kind: str = "Album",
    days_ago: int = 0,
    artist_id: int = 1,
    artist: str = "Artiste",
    **kw,
) -> radar.Release:
    return radar.Release(
        album_id=album_id,
        artist=artist,
        title=title,
        kind=kind,
        release_date=radar._today() - timedelta(days=days_ago),
        url=f"https://music.apple.com/fr/album/{album_id}",
        artist_id=artist_id,
        cover=f"https://img/{album_id}/300x300bb.jpg",
        **kw,
    )


def _no_network(url: str, params: dict) -> dict:
    raise AssertionError(f"Appel réseau non simulé : {url} {params}")


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Base, artists.txt et réseau isolés pour chaque test."""
    monkeypatch.setattr(radar, "DATA_DIR", tmp_path / "data")
    artists = tmp_path / "artists.txt"
    artists.write_text(SAMPLE_ARTISTS, encoding="utf-8")
    monkeypatch.setattr(radar, "ARTISTS_FILE", artists)
    monkeypatch.setattr(radar.time, "sleep", lambda s: None)
    monkeypatch.setattr(radar, "_get_json", _no_network)
    return tmp_path


@pytest.fixture
def itunes(monkeypatch: pytest.MonkeyPatch) -> FakeItunes:
    fake = FakeItunes()
    monkeypatch.setattr(radar, "_get_json", fake)
    return fake
