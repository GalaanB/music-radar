"""Scan des artistes : sorties apparues en retard chez Apple, ordre de l'API."""


import pytest
import requests

from conftest import collection
from musicradar import config, pipeline, scan, storage
from musicradar.sources import itunes as itunes_api


def follow(name, artist_id):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES (?, ?, ?)", (name, artist_id, name))
    conn.commit()
    conn.close()


def test_daily_catches_a_release_that_appears_late_at_apple(itunes):
    follow("Josman", 1)
    itunes.albums = {1: [collection(1, "Ancien - Single", days_ago=40, artist="Josman")]}
    pipeline.finalize(pipeline.prepare(scan.daily_batch()))
    # Apple n'affiche « TOUS MES DIAMANTS », sorti il y a 4 jours, qu'aujourd'hui.
    itunes.albums[1].append(collection(2, "TOUS MES DIAMANTS - Single", days_ago=4, artist="Josman"))
    assert [r.title for r in scan.daily_batch().releases] == ["TOUS MES DIAMANTS"]


def test_daily_never_resends_what_was_already_notified(itunes):
    follow("Josman", 1)
    itunes.albums = {1: [collection(2, "TOUS MES DIAMANTS - Single", days_ago=4, artist="Josman")]}
    pipeline.finalize(pipeline.prepare(scan.daily_batch()))
    assert scan.daily_batch().releases == []


def test_recent_release_far_down_the_api_list_is_read(itunes):
    follow("Josman", 1)
    old = [collection(100 + i, f"Vieux {i} - Single", days_ago=400 + i, artist="Josman") for i in range(30)]
    itunes.albums = {1: [*old, collection(2, "TOUS MES DIAMANTS - Single", days_ago=1, artist="Josman")]}
    assert [r.title for r in scan.daily_batch().releases] == ["TOUS MES DIAMANTS"]
    assert itunes.calls[0][1]["limit"] >= 200


# --- catalogues lus par paquets -------------------------------------------------------

def follow_many(n):
    conn = storage.connect()
    conn.executemany("INSERT INTO artists VALUES (?, ?, ?)", [(f"A{i:02d}", i, f"A{i:02d}") for i in range(1, n + 1)])
    conn.commit()
    conn.close()


def catalog_calls(itunes):
    return [p["id"] for _, p in itunes.calls if p.get("entity") == "album"]


def test_catalogs_are_read_five_artists_per_request(itunes):
    follow_many(12)
    itunes.albums = {i: [collection(100 + i, f"Titre {i} - Single", days_ago=1, artist=f"A{i:02d}")] for i in range(1, 13)}
    batch = scan.catchup_batch(14)
    assert catalog_calls(itunes) == ["1,2,3,4,5", "6,7,8,9,10", "11,12"]
    assert sorted(r.album_id for r in batch.releases) == list(range(101, 113))
    assert {r.album_id: r.artist_id for r in batch.releases} == {100 + i: i for i in range(1, 13)}
    assert sorted(batch.catalog) == list(range(1, 13))


def test_grouped_and_single_requests_give_the_same_releases(itunes, monkeypatch):
    follow_many(7)
    shared = collection(500, "Duo - Single", days_ago=2, artist="A01 & A06")
    itunes.albums = {i: [collection(100 + i, f"Projet {i}", days_ago=i, artist=f"A{i:02d}")] for i in range(1, 8)}
    itunes.albums[1].append(shared)
    itunes.albums[6].append(dict(shared))
    grouped = scan.catchup_batch(14)
    monkeypatch.setattr(config, "LOOKUP_BATCH_SIZE", 1)
    single = scan.catchup_batch(14)
    assert grouped.releases == single.releases and grouped.catalog == single.catalog
    assert next(r for r in grouped.releases if r.album_id == 500).artist_id == 6


def test_batch_size_one_asks_one_artist_per_request(itunes, monkeypatch):
    monkeypatch.setattr(config, "LOOKUP_BATCH_SIZE", 1)
    follow_many(3)
    scan.catchup_batch(14)
    assert catalog_calls(itunes) == [1, 2, 3]


def test_failed_batch_is_read_again_one_artist_at_a_time(itunes, monkeypatch):
    follow_many(7)
    itunes.albums = {i: [collection(100 + i, f"Titre {i} - Single", days_ago=1, artist=f"A{i:02d}")] for i in range(1, 8)}
    real = itunes.__call__

    def first_batch_fails(url, params):
        if params.get("entity") == "album" and params["id"] == "1,2,3,4,5":
            itunes.calls.append((url, dict(params)))
            raise requests.ConnectionError("réponse trop lente")
        return real(url, params)

    monkeypatch.setattr(itunes_api, "get_json", first_batch_fails)
    assert sorted(r.album_id for r in scan.catchup_batch(14).releases) == list(range(101, 108))
    assert catalog_calls(itunes) == ["1,2,3,4,5", 1, 2, 3, 4, 5, "6,7"]


def test_artist_missing_from_a_grouped_answer_is_asked_alone(itunes):
    follow_many(5)
    itunes.albums = {i: [collection(100 + i, f"Titre {i} - Single", days_ago=1, artist=f"A{i:02d}")] for i in range(1, 6)}
    itunes.no_block = {3}
    assert sorted(r.album_id for r in scan.catchup_batch(14).releases) == [101, 102, 103, 104, 105]
    assert catalog_calls(itunes) == ["1,2,3,4,5", 3]


def test_block_of_an_artist_that_was_not_asked_is_ignored(monkeypatch):
    def answer(url, params):
        return {"results": [
            {"wrapperType": "artist", "artistId": 1}, collection(11, "À moi", artist="A1"),
            {"wrapperType": "artist", "artistId": 99}, collection(991, "Intrus", artist="Autre"),
            {"wrapperType": "artist", "artistId": 2},
        ]}

    monkeypatch.setattr(itunes_api, "get_json", answer)
    got = itunes_api.fetch_many_artist_releases([1, 2, 3])
    assert {aid: [r.album_id for r in rels] for aid, rels in got.items()} == {1: [11], 2: []}
    assert got[1][0].artist_id == 1


def test_apple_down_still_stops_the_scan_with_batches(monkeypatch):
    follow_many(29)
    asked = []

    def down(url, params):
        asked.append(params["id"])
        raise requests.ConnectionError("Apple down")

    monkeypatch.setattr(itunes_api, "get_json", down)
    with pytest.raises(RuntimeError, match=r"Apple ne répond plus \(5 échecs d'affilée\)"):
        scan.catchup_batch(14)
    assert asked == ["1,2,3,4,5", 1, 2, 3, 4, 5]   # le premier paquet suffit à conclure
