"""Featurings sur le projet d'un autre artiste (absents des sorties de l'invité)."""

import pytest
import requests

from conftest import collection, song
from musicradar import config, formatting, models, pipeline, scan, storage, text
from musicradar.sources import itunes as itunes_api


@pytest.fixture
def josman(itunes):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES ('Josman', 1, 'Josman')")
    conn.commit()
    conn.close()
    itunes.albums = {1: [collection(10, "TOUS MES DIAMANTS - Single", days_ago=4, artist="Josman")]}
    return itunes


def scanned(days=14):
    return {r.album_id: r for r in scan.catchup_batch(days).releases}


def test_feature_on_another_artists_album_is_found(josman):
    josman.songs = {"Josman": [song(500, "Sensible (feat. Josman)", "Ronisia", "Sensation", 50, days_ago=6)]}
    rels = scanned()
    feat = rels[500]
    assert feat.kind == models.KIND_FEATURING and feat.artist == "Ronisia"
    assert feat.note == "🎤 Josman en featuring · album « Sensation »"
    assert feat.url.endswith("?i=500")
    assert 10 in rels  # les sorties habituelles restent là


@pytest.mark.parametrize("track", [
    song(501, "Josman Freestyle", "Autre", "Mixtape", 51),          # nom dans le titre, pas crédité
    song(502, "Boussole (feat. Josman)", "Autre", "Boussole - Single", 52),  # single : déjà listé
    song(503, "Vieux (feat. Josman)", "Autre", "Ancien", 53, days_ago=60),   # trop ancien
    song(504, "Intro", "Josman", "J.O.$", 54, artist_id=1),          # son propre album
])
def test_irrelevant_tracks_are_skipped(josman, track):
    josman.songs = {"Josman": [track]}
    assert track["trackId"] not in scanned()


def test_credits_with_several_artists_and_commas():
    assert text.cmp_key("Josman") in text.credited("Song (feat. Youssoupha, Josman & Kalash)", "X")
    assert text.cmp_key("Josman") in text.credited("Song", "Ronisia & Josman")
    assert text.cmp_key("Tyler, The Creator") in text.credited("Song (feat. Tyler, The Creator)", "X")
    assert text.cmp_key("Josman") in text.credited("Song feat. Josman", "X")
    assert text.cmp_key("Josman") not in text.credited("Josman Freestyle", "X")


def test_same_feature_found_through_two_followed_artists_is_kept_once(josman):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES ('Kalash', 2, 'Kalash')")
    conn.commit()
    conn.close()
    track = song(600, "Titre (feat. Josman & Kalash)", "Autre", "Album", 60, days_ago=2)
    josman.songs = {"Josman": [track], "Kalash": [dict(track)]}
    assert [r.album_id for r in scan.catchup_batch(14).releases].count(600) == 1


def test_feature_on_a_followed_artists_album_is_not_duplicated(josman):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES ('Ronisia', 3, 'Ronisia')")
    conn.commit()
    conn.close()
    josman.albums[3] = [collection(50, "Sensation", days_ago=6, artist="Ronisia")]
    josman.songs = {"Josman": [song(500, "Sensible (feat. Josman)", "Ronisia", "Sensation", 50, days_ago=6, artist_id=3)]}
    rels = scanned()
    assert 50 in rels and 500 not in rels  # l'album de Ronisia suffit


def test_failed_search_does_not_break_the_scan(josman, monkeypatch):
    real = itunes_api.get_json

    def flaky(url, params):
        if params.get("entity") == "song":
            raise requests.ConnectionError("rate limit")
        return real(url, params)

    monkeypatch.setattr(itunes_api, "get_json", flaky)
    assert 10 in scanned()


def test_features_can_be_disabled(josman, monkeypatch):
    monkeypatch.setattr(config, "FEATURES_ENABLED", False)
    josman.songs = {"Josman": [song(500, "Sensible (feat. Josman)", "Ronisia", "Sensation", 50, days_ago=6)]}
    assert 500 not in scanned()
    assert not any(p.get("entity") == "song" for _, p in josman.calls)


def test_feature_card_and_analysis(josman):
    josman.songs = {"Josman": [song(500, "Sensible (feat. Josman)", "Ronisia", "Sensation", 50, days_ago=6)]}
    p = pipeline.prepare(scan.catchup_batch(14))
    feat = next(r for r in p.to_show if r.album_id == 500)
    assert feat.note.startswith("🎤")  # l'analyse ne l'écrase pas
    card = formatting.card_text(feat)
    assert "Josman en featuring" in card and "Sensation" in card
    assert card.count("eaturing") == 1  # pas de « Featuring » en double
    pipeline.finalize(p)
    assert scan.daily_batch().releases == []  # notifié une seule fois


# --- homonymes et doublons -----------------------------------------------------

def follow(name, artist_id):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES (?, ?, ?)", (name, artist_id, name))
    conn.commit()
    conn.close()


def test_no_feature_search_when_several_artists_share_the_name(itunes):
    follow("SAM", 7)
    itunes.albums = {7: []}
    itunes.search = {"SAM": [
        {"artistId": 7, "artistName": "SAM"},
        {"artistId": 8, "artistName": "Sam"},  # un autre Sam
    ]}
    itunes.songs = {"SAM": [song(700, "Love (feat. Sam)", "Chanteuse Pop", "Pop Album", 70, days_ago=2)]}
    assert 700 not in scanned()
    assert not any(p.get("entity") == "song" for _, p in itunes.calls)


def test_wrong_features_already_waiting_are_removed_but_your_choices_kept(itunes):
    follow("SAM", 7)
    itunes.albums = {7: []}
    itunes.search = {"SAM": [{"artistId": 7, "artistName": "SAM"}, {"artistId": 8, "artistName": "Sam"}]}
    from conftest import release
    storage.save_releases([
        release(701, "Faux (feat. Sam)", models.KIND_FEATURING, artist_id=7),
        release(702, "Gardé (feat. Sam)", models.KIND_FEATURING, artist_id=7, state=models.STATE_ADDED),
    ])
    scan.catchup_batch(14)
    assert storage.get_release(701) is None
    assert storage.get_release(702).state == models.STATE_ADDED


def test_name_check_is_cached(josman):
    josman.search = {"Josman": [{"artistId": 1, "artistName": "Josman"}]}
    scan.catchup_batch(14)
    scan.catchup_batch(14)
    checks = [p for _, p in josman.calls if p.get("entity") == "musicArtist"]
    assert len(checks) == 1


def test_feature_already_released_as_single_is_not_repeated(josman):
    # « Boussole » est d'abord sorti en single (notifié), puis sur l'album de l'artiste invitant.
    from conftest import release
    storage.save_releases([release(80, "Boussole (feat. Josman)", "Single", days_ago=20, artist="Autre", artist_id=1)])
    josman.songs = {"Josman": [song(800, "Boussole (feat. Josman)", "Autre", "Grand Album", 81, days_ago=2)]}
    assert 800 not in scanned()
