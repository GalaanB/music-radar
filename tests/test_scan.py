"""Scan des artistes : sorties apparues en retard chez Apple, ordre de l'API."""

import radar
from conftest import collection


def follow(name, artist_id):
    conn = radar._db()
    conn.execute("INSERT INTO artists VALUES (?, ?, ?)", (name, artist_id, name))
    conn.commit()
    conn.close()


def test_daily_catches_a_release_that_appears_late_at_apple(itunes):
    follow("Josman", 1)
    itunes.albums = {1: [collection(1, "Ancien - Single", days_ago=40, artist="Josman")]}
    radar.finalize(radar.prepare(radar.daily_batch()))
    # Apple n'affiche « TOUS MES DIAMANTS », sorti il y a 4 jours, qu'aujourd'hui.
    itunes.albums[1].append(collection(2, "TOUS MES DIAMANTS - Single", days_ago=4, artist="Josman"))
    assert [r.title for r in radar.daily_batch().releases] == ["TOUS MES DIAMANTS"]


def test_daily_never_resends_what_was_already_notified(itunes):
    follow("Josman", 1)
    itunes.albums = {1: [collection(2, "TOUS MES DIAMANTS - Single", days_ago=4, artist="Josman")]}
    radar.finalize(radar.prepare(radar.daily_batch()))
    assert radar.daily_batch().releases == []


def test_recent_release_far_down_the_api_list_is_read(itunes):
    follow("Josman", 1)
    old = [collection(100 + i, f"Vieux {i} - Single", days_ago=400 + i, artist="Josman") for i in range(30)]
    itunes.albums = {1: [*old, collection(2, "TOUS MES DIAMANTS - Single", days_ago=1, artist="Josman")]}
    assert [r.title for r in radar.daily_batch().releases] == ["TOUS MES DIAMANTS"]
    assert itunes.calls[0][1]["limit"] >= 200
