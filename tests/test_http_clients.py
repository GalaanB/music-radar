"""Clients Apple et MusicBrainz : nouvelles tentatives, réponses abîmées, lecture des fiches."""

from datetime import date

import pytest
import requests

from conftest import collection
from musicradar import config
from musicradar.sources import itunes as itunes_api
from musicradar.sources import musicbrainz

# conftest remplace ces deux fonctions pendant chaque test : on garde les vraies.
REAL_GET_JSON = itunes_api.get_json
REAL_MB_GET = musicbrainz.get


class Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code, self.payload, self.bad_json = status, payload or {}, bad_json

    def json(self):
        if self.bad_json:
            raise ValueError("pas du JSON")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def http(monkeypatch):
    """Remplace requests.get par une file de réponses (ou d'exceptions) ; retourne les appels faits."""
    calls = []

    def install(*answers):
        queue = list(answers)

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(requests, "get", fake_get)
        return calls

    return install


# --- Apple -------------------------------------------------------------------------

@pytest.mark.parametrize("first", [
    Resp(403), Resp(429), Resp(500), Resp(bad_json=True), requests.ConnectionError("coupure"),
])
def test_apple_call_is_retried_once(http, first):
    calls = http(first, Resp(payload={"results": [1]}))
    assert REAL_GET_JSON(config.ITUNES_LOOKUP, {"id": 1}) == {"results": [1]}
    assert len(calls) == 2


def test_apple_call_gives_up_after_the_second_failure(http):
    calls = http(Resp(403), requests.ConnectionError("toujours en panne"))
    with pytest.raises(requests.ConnectionError):
        REAL_GET_JSON(config.ITUNES_LOOKUP, {"id": 1})
    assert len(calls) == 2


def test_malformed_album_entries_are_skipped(itunes):
    no_url = collection(2, "Sans lien")
    del no_url["collectionViewUrl"]
    bad_date = collection(3, "Date illisible")
    bad_date["releaseDate"] = "bientôt"
    itunes.albums = {1: [collection(1, "Correct - EP", artist="Josman", explicit=False, tracks=6), no_url, bad_date]}
    rels = itunes_api.fetch_artist_releases(1)
    assert [r.album_id for r in rels] == [1]
    r = rels[0]
    assert (r.kind, r.title, r.artist, r.artist_id, r.track_count, r.explicit) == ("EP", "Correct", "Josman", 1, 6, False)
    assert r.cover == "https://img/1/300x300bb.jpg"


def test_release_date_is_read_in_paris_time(itunes):
    late = collection(1, "Presque minuit")
    late["releaseDate"] = "2026-06-30T22:30:00Z"  # déjà le 1er juillet à Paris
    itunes.albums = {1: [late]}
    assert itunes_api.fetch_artist_releases(1)[0].release_date == date(2026, 7, 1)


def test_tracks_are_ordered_by_disc_then_number(monkeypatch):
    def lookup(url, params):
        return {"results": [
            {"wrapperType": "collection"},
            {"wrapperType": "track", "trackName": "D2-1", "discNumber": 2, "trackNumber": 1},
            {"wrapperType": "track", "trackName": "D1-2", "discNumber": 1, "trackNumber": 2},
            {"wrapperType": "track", "trackName": "D1-1", "discNumber": 1, "trackNumber": 1},
            {"wrapperType": "track", "trackName": ""},
        ]}

    monkeypatch.setattr(itunes_api, "get_json", lookup)
    assert itunes_api.tracks_of(100) == ["D1-1", "D1-2", "D2-1"]


def test_empty_track_list_is_not_cached(itunes):
    assert itunes_api.tracks_of(100) == []
    itunes_api.tracks_of(100)
    assert len(itunes.calls) == 2


def test_artist_search_removes_duplicates_and_caps_the_list(itunes):
    itunes.search = {"Sam": [{"artistId": i // 2, "artistName": f"Sam {i // 2}"} for i in range(2, 20)]}
    found = itunes_api.search_artists("Sam", limit=3)
    assert [c["id"] for c in found] == [1, 2, 3]
    assert found[0] == {"id": 1, "name": "Sam 1", "genre": "?", "url": ""}


def test_lookup_of_an_unknown_artist_returns_none(itunes):
    assert itunes_api.lookup_artist(404) is None


def test_overview_of_an_artist_without_release(itunes):
    assert itunes_api.artist_overview(5)["last"] == "aucune sortie"


# --- MusicBrainz -------------------------------------------------------------------

def test_musicbrainz_is_retried_once_when_saturated(http):
    calls = http(Resp(503), Resp(payload={"artists": []}))
    assert REAL_MB_GET("artist", {"query": "x"}) == {"artists": []}
    url, kwargs = calls[1]
    assert url == f"{config.MB_API}/artist"
    assert kwargs["params"] == {"query": "x", "fmt": "json"} and kwargs["headers"] == config.MB_HEADERS


def test_musicbrainz_still_saturated_is_an_error(http):
    http(Resp(503), Resp(503))
    with pytest.raises(requests.HTTPError):
        REAL_MB_GET("artist", {})


def test_musicbrainz_network_error_is_not_retried(http):
    calls = http(requests.ConnectionError("coupure"))
    with pytest.raises(requests.ConnectionError):
        REAL_MB_GET("artist", {})
    assert len(calls) == 1


@pytest.mark.parametrize(("total", "expected", "offsets"), [
    (250, 250, [0, 100, 200]),
    (450, 300, [0, 100, 200]),   # plafond de 300 enregistrements
    (0, 0, [0]),
])
def test_recordings_are_read_page_by_page(monkeypatch, total, expected, offsets):
    recs = [{"title": f"T{i}"} for i in range(total)]
    seen = []

    def paged(path, params):
        seen.append(params["offset"])
        return {"count": total, "recordings": recs[params["offset"] : params["offset"] + params["limit"]]}

    monkeypatch.setattr(musicbrainz, "get", paged)
    out = musicbrainz.recordings_crediting("mb-x", date(2026, 1, 1), date(2026, 1, 15))
    assert len(out) == expected and seen == offsets


@pytest.mark.parametrize(("url", "apple_id"), [
    ("https://music.apple.com/us/artist/quavo/1065981054", 1065981054),
    ("https://music.apple.com/fr/artist/quavo/1065981054?l=en", 1065981054),
    ("https://itunes.apple.com/us/artist/id1065981054", 1065981054),
    ("https://itunes.apple.com/us/artist/quavo/id1065981054", 1065981054),
])
def test_apple_id_is_read_from_a_musicbrainz_link(mb, itunes, monkeypatch, url, apple_id):
    mb.artists = {"Quavo": [{"id": "mb-q", "name": "Quavo"}, {"id": "mb-other", "name": "Quavo"}]}
    real = mb.__call__

    def with_url(path, params):
        if path == "artist/mb-q":
            return {"relations": [{"url": {"resource": url}}, {"url": {"resource": "https://example.org/quavo"}}]}
        return real(path, params)

    monkeypatch.setattr(musicbrainz, "get", with_url)
    assert musicbrainz.mbid_for(apple_id, "Quavo") == "mb-q"
