"""Featurings trouvés via MusicBrainz (invités reliés par identifiant)."""

from datetime import timedelta

import pytest

from conftest import collection, recording, song
from musicradar import clock, config, models, scan, storage
from musicradar.sources import musicbrainz


def ago(days):
    return (clock.today() - timedelta(days=days)).isoformat()


QUAVO, QUAVO_MC, YUNG_MIAMI, GAME = "mb-quavo", "mb-quavo-mc", "mb-yung-miami", "mb-the-game"


@pytest.fixture
def quavo(itunes, mb):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES ('Quavo', 10, 'Quavo')")
    conn.commit()
    conn.close()
    itunes.albums = {10: [collection(100, "Own Album", days_ago=30, artist="Quavo")]}
    # Deux « Quavo » chez MusicBrainz : seul le bon renvoie vers l'ID Apple 10.
    mb.artists = {"Quavo": [{"id": QUAVO_MC, "name": "Quavo"}, {"id": QUAVO, "name": "Quavo"}]}
    mb.apple_links = {QUAVO: [10], QUAVO_MC: [555]}
    mb.recordings = {QUAVO: [
        recording("TMTC", [("Yung Miami", YUNG_MIAMI), ("Quavo", QUAVO), ("G Herbo", "mb-g")], ago(5)),
        recording("HAAVIN", [("Quavo", QUAVO)], ago(3)),                          # son propre titre
        recording("Bando", [("Angélique Kidjo", "mb-ak"), ("Quavo", QUAVO)], ago(190)),  # vieux, sur une compil
    ]}
    itunes.songs = {
        "Yung Miami TMTC": [song(9001, "TMTC (feat. Quavo & G Herbo)", "Yung Miami", "Album de Yung Miami", 900, days_ago=5)],
    }
    return itunes, mb


def test_mbid_is_chosen_by_apple_link_not_by_name(quavo):
    assert musicbrainz.mbid_for(10, "Quavo") == QUAVO


def test_feature_credited_only_in_title_is_found(quavo):
    rels = {r.album_id: r for r in scan.catchup_batch(14).releases}
    tmtc = rels[9001]
    assert tmtc.kind == models.KIND_FEATURING and tmtc.artist == "Yung Miami"
    assert tmtc.note == "🎤 Quavo en featuring · album « Album de Yung Miami »"
    assert not any("Bando" in r.title or "HAAVIN" in r.title for r in rels.values())


def test_feature_not_on_apple_is_skipped(quavo):
    itunes, _ = quavo
    itunes.songs = {}
    assert not any(r.kind == models.KIND_FEATURING for r in scan.catchup_batch(14).releases)


def test_ambiguous_name_works_through_musicbrainz(itunes, mb):
    # « SAM » est ambigu chez Apple, mais MusicBrainz le relie par l'ID Apple.
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES ('SAM', 7, 'SAM')")
    conn.commit()
    conn.close()
    itunes.albums = {7: []}
    itunes.search = {"SAM": [{"artistId": 7, "artistName": "SAM"}, {"artistId": 8, "artistName": "Sam"}]}
    mb.artists = {"SAM": [{"id": "mb-sam", "name": "SAM"}]}
    mb.apple_links = {"mb-sam": [7]}
    mb.recordings = {"mb-sam": [recording("Feu", [("Autre", "mb-x"), ("SAM", "mb-sam")], ago(3))]}
    itunes.songs = {"Autre Feu": [song(7001, "Feu (feat. SAM)", "Autre", "Projet", 70, days_ago=3)]}
    assert 7001 in {r.album_id for r in scan.catchup_batch(14).releases}


def test_unknown_artist_falls_back_to_apple_search(itunes, mb):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES ('Josman', 1, 'Josman')")
    conn.commit()
    conn.close()
    itunes.albums = {1: []}
    itunes.songs = {"Josman": [song(500, "Sensible (feat. Josman)", "Ronisia", "Sensation", 50, days_ago=6)]}
    assert 500 in {r.album_id for r in scan.catchup_batch(14).releases}


def test_same_name_without_apple_link_is_not_guessed(itunes, mb):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES ('Luv', 33, 'Luv')")
    conn.commit()
    conn.close()
    itunes.search = {"Luv": [{"artistId": 33, "artistName": "Luv"}, {"artistId": 34, "artistName": "Luv"}]}
    mb.artists = {"Luv": [{"id": "mb-luv", "name": "Luv"}]}
    assert musicbrainz.mbid_for(33, "Luv") is None


def test_mbid_lookup_is_cached(quavo):
    _, mb = quavo
    musicbrainz.mbid_for(10, "Quavo")
    n = len(mb.calls)
    musicbrainz.mbid_for(10, "Quavo")
    assert len(mb.calls) == n


def test_musicbrainz_can_be_disabled(quavo, monkeypatch):
    _, mb = quavo
    monkeypatch.setattr(config, "MUSICBRAINZ_ENABLED", False)
    scan.catchup_batch(14)
    assert mb.calls == []


def test_old_name_based_features_of_an_ambiguous_artist_are_replaced(itunes, mb):
    from conftest import release
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES ('SAM', 7, 'SAM')")
    conn.commit()
    conn.close()
    itunes.albums = {7: []}
    itunes.search = {"SAM": [{"artistId": 7, "artistName": "SAM"}, {"artistId": 8, "artistName": "Sam"}]}
    mb.artists = {"SAM": [{"id": "mb-sam", "name": "SAM"}]}
    mb.apple_links = {"mb-sam": [7]}
    storage.save_releases([release(6001, "Faux (feat. Sam)", models.KIND_FEATURING, artist_id=7)])
    scan.catchup_batch(14)
    assert storage.get_release(6001) is None


def test_feature_already_out_as_a_duo_single_is_not_repeated(itunes, mb):
    # « Run The World » : single de « Loe Shimmy & Brent Faiyaz », que MusicBrainz
    # range aussi comme featuring de Brent Faiyaz. Une seule carte suffit.
    conn = storage.connect()
    conn.executemany("INSERT INTO artists VALUES (?, ?, ?)", [("Loe Shimmy", 20, "Loe Shimmy"), ("Brent Faiyaz", 21, "Brent Faiyaz")])
    conn.commit()
    conn.close()
    duo = "Loe Shimmy & Brent Faiyaz"
    itunes.albums = {20: [collection(300, "Run The World - Single", days_ago=4, artist=duo)], 21: []}
    mb.artists = {"Brent Faiyaz": [{"id": "mb-brent", "name": "Brent Faiyaz"}], "Loe Shimmy": []}
    mb.apple_links = {"mb-brent": [21]}
    mb.recordings = {"mb-brent": [recording("Run The World", [("Loe Shimmy", "mb-loe"), ("Brent Faiyaz", "mb-brent")], ago(4))]}
    # Chez Apple, le titre remonte via une autre fiche (version clean).
    itunes.songs = {"Loe Shimmy Run The World": [song(3001, "Run The World", duo, "Run The World - Single", 301, days_ago=4)]}
    rels = scan.catchup_batch(14).releases
    assert [r.kind for r in rels if r.title.startswith("Run The World")] == ["Single"]


def test_featuring_twins_already_waiting_are_removed_at_startup():
    from conftest import release
    duo = "Loe Shimmy & Brent Faiyaz"
    storage.save_releases([
        release(300, "Run The World", "Single", artist=duo, artist_id=20),
        release(3001, "Run The World", models.KIND_FEATURING, artist=duo, artist_id=21),
        release(3002, "Autre titre", models.KIND_FEATURING, artist=duo, artist_id=21),
    ])
    storage.migrate()
    assert storage.get_release(300) and storage.get_release(3002)
    assert storage.get_release(3001) is None


# --- MusicBrainz interrogé en parallèle des catalogues Apple --------------------

def test_musicbrainz_is_queried_while_apple_catalogs_are_read(quavo, monkeypatch):
    import threading

    from musicradar.sources import itunes as itunes_api

    itunes, mb = quavo
    mb_started = threading.Event()
    order = []
    real_mb, real_apple = mb.__call__, itunes.__call__

    def mb_get(path, params):
        order.append("musicbrainz")
        mb_started.set()
        return real_mb(path, params)

    def apple_get(url, params):
        if params.get("entity") == "album":
            # Le catalogue ne répond qu'une fois MusicBrainz interrogé : sans
            # thread à part, ce test resterait bloqué jusqu'au délai.
            assert mb_started.wait(timeout=5)
            order.append("catalogue")
        return real_apple(url, params)

    monkeypatch.setattr(musicbrainz, "get", mb_get)
    monkeypatch.setattr(itunes_api, "get_json", apple_get)
    assert 9001 in {r.album_id for r in scan.catchup_batch(14).releases}
    assert order[0] == "musicbrainz" and "catalogue" in order


def test_interrupted_scan_leaves_no_musicbrainz_thread_behind(itunes, mb, monkeypatch):
    import threading

    import requests

    from musicradar.sources import itunes as itunes_api

    conn = storage.connect()
    conn.executemany("INSERT INTO artists VALUES (?, ?, ?)", [(f"A{i}", i, f"A{i}") for i in range(1, 30)])
    conn.commit()
    conn.close()

    def down(url, params):
        raise requests.ConnectionError("Apple down")

    monkeypatch.setattr(itunes_api, "get_json", down)
    with pytest.raises(RuntimeError, match="Apple ne répond plus"):
        scan.catchup_batch(14)
    assert not [t for t in threading.enumerate() if t.name.startswith("musicbrainz")]


def test_a_bug_while_reading_musicbrainz_is_not_swallowed(quavo, monkeypatch):
    def broken(artist_id, name):
        raise KeyError("id")

    monkeypatch.setattr(musicbrainz, "mbid_for", broken)
    with pytest.raises(KeyError):
        scan.catchup_batch(14)
