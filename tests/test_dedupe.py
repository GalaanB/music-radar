"""Plusieurs fiches Apple pour une même sortie (explicite / clean, doublons)."""

import radar
from conftest import collection, release

MAVERICK = "Maverick “Almost Forever” Ep"


def resolve(*pairs):
    conn = radar._db()
    conn.executemany("INSERT INTO artists VALUES (?, ?, ?)", [(n, i, n) for n, i in pairs])
    conn.commit()
    conn.close()


def test_keeps_one_card_per_release_preferring_explicit(itunes):
    resolve(("Lil Uzi Vert", 1))
    itunes.albums = {1: [
        collection(6796859137, MAVERICK, explicit=False, artist="Lil Uzi Vert"),
        collection(6797312643, MAVERICK, explicit=True, artist="Lil Uzi Vert"),
        collection(10, "Solo - Single", tracks=1, artist="Lil Uzi Vert"),
        collection(11, "Solo", tracks=12, artist="Lil Uzi Vert"),
    ]}
    ids = sorted(r.album_id for r in radar.catchup_batch(10).releases)
    assert ids == [10, 11, 6797312643]  # le single et l'album homonymes restent deux sorties


def test_same_release_seen_through_two_followed_artists(itunes):
    resolve(("Lil Uzi Vert", 1), ("Autre", 2))
    item = collection(6797312643, MAVERICK, artist="Lil Uzi Vert")
    itunes.albums = {1: [item], 2: [dict(item)]}
    assert [r.album_id for r in radar.catchup_batch(10).releases] == [6797312643]


def test_db_twin_is_absorbed_and_your_choice_kept(itunes):
    resolve(("Lil Uzi Vert", 1))
    itunes.albums = {1: [collection(6797312643, MAVERICK, artist="Lil Uzi Vert")]}
    clean = release(6796859137, MAVERICK, "EP", artist="Lil Uzi Vert", state=radar.STATE_ADDED)
    radar.save_releases([clean])
    p = radar.prepare(radar.catchup_batch(10))
    assert radar.get_release(6796859137) is None
    assert radar.get_release(6797312643).state == radar.STATE_ADDED
    assert p.to_show == []


def test_daily_does_not_notify_a_new_card_of_an_already_notified_release(itunes):
    resolve(("Lil Uzi Vert", 1))
    itunes.albums = {1: [collection(1, MAVERICK, artist="Lil Uzi Vert")]}
    radar.finalize(radar.prepare(radar.daily_batch()))
    itunes.albums = {1: [collection(999, MAVERICK, artist="Lil Uzi Vert")]}
    assert radar.daily_batch().releases == []
