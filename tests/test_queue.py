"""Préparation d'un envoi, tri (états), file /trier et migrations de la base."""

import radar
from conftest import release


def prepared_sample(itunes):
    itunes.tracks = {100: ["Intro", "Hit One", "Hit Two", "Outro"], 400: ["Intro", "Hit One", "Hit Two", "Outro"]}
    album = release(100, "Great Album", days_ago=3)
    empty_deluxe = release(400, "Great Album (Expanded Edition)", days_ago=2)
    remix = release(12, "Hit One (Remix)", "Single", days_ago=1)
    other = release(500, "Fresh", "Single", days_ago=1, artist_id=2, artist="Other")
    catalog = {1: [release(10, "Hit One", "Single", 30), album, empty_deluxe, remix], 2: [other]}
    batch = radar.Batch([remix, other, empty_deluxe, album], [release(900, "Soon", days_ago=-4)], catalog)
    return radar.prepare(batch)


def test_prepare_splits_cards_and_empty_reissues(itunes):
    p = prepared_sample(itunes)
    assert {r.album_id for r in p.to_show} == {100, 12, 500}
    assert [r.album_id for r in p.nothing_new] == [400]
    assert len(p.upcoming) == 1 and p.already_sorted == 0


def test_finalize_marks_notified_and_sets_empty_reissues_aside(itunes):
    p = prepared_sample(itunes)
    radar.finalize(p)
    assert radar.get_release(400).state == radar.STATE_IGNORED
    assert radar.get_release(12).state == radar.STATE_NEW  # les variantes restent à trier
    conn = radar._db()
    assert conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 4
    conn.close()


def test_sorted_release_is_never_shown_again(itunes):
    prepared_sample(itunes)
    radar.set_state([100], radar.STATE_ADDED)
    p = radar.prepare(radar.Batch([release(100, "Great Album", days_ago=3)], [], {}))
    assert p.to_show == [] and p.already_sorted == 1
    assert radar.get_release(100).state == radar.STATE_ADDED


def test_analysis_is_not_redone(itunes):
    prepared_sample(itunes)
    itunes.calls.clear()
    radar.prepare(radar.Batch([release(100, "Great Album", days_ago=3)], [], {}))
    assert itunes.calls == []


def test_queue_walks_newest_first_without_looping(itunes):
    radar.finalize(prepared_sample(itunes))
    seen, cur = [], 0
    while True:
        nxt, total = radar.next_in_queue(radar.STATE_NEW, cur)
        if nxt is None:
            break
        seen.append(nxt.album_id)
        cur = nxt.album_id
    assert sorted(seen) == [12, 100, 500] and len(set(seen)) == 3
    dates = [radar.get_release(i).release_date for i in seen]
    assert dates == sorted(dates, reverse=True)


def insert_rows(*rows):
    conn = radar._db()
    conn.executemany(
        "INSERT INTO releases (album_id, artist, artist_id, title, kind, release_date, url, cover,"
        " track_count, total_tracks, new_tracks, note, state, analyzed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i, "Q", 1, t, "Album", radar._today().isoformat(), "u", "", 0, None, None, n, s, 1) for i, t, n, s in rows],
    )
    conn.commit()
    conn.close()


def test_migrate_upgrades_an_old_database():
    insert_rows(
        (90, "Proj (Slowed)", "🔁 version alternative", "ignored"),   # rangée par une ancienne version
        (91, "Big", "🆕 8 nouveaux / 14 · 6 déjà sortis", "new"),     # ancien libellé
        (92, "Big (Deluxe)", "", "new"),                              # sans verdict
        (93, "Other", "", "ignored"),                                 # ignorée par toi
        (94, "Liked", "", "to_listen"),                               # ancien bouton « À écouter »
    )
    assert radar.migrate() == 1
    assert radar.get_release(90).state == radar.STATE_NEW
    assert radar.get_release(91).note == "🆕 8 inédits sur 14 · 6 déjà sortis"
    assert radar.get_release(92).analyzed is False
    assert radar.get_release(93).state == radar.STATE_IGNORED
    assert radar.get_release(94).state == radar.STATE_ADDED


def test_migrate_restores_variants_only_once():
    insert_rows((90, "Proj (Slowed)", "🔁 version alternative", "ignored"))
    radar.migrate()
    radar.set_state([90], radar.STATE_IGNORED)
    assert radar.migrate() == 0
    assert radar.get_release(90).state == radar.STATE_IGNORED
