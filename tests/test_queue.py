"""Préparation d'un envoi, tri (états), file /trier et migrations de la base."""


from conftest import release
from musicradar import clock, models, pipeline, storage


def prepared_sample(itunes):
    itunes.tracks = {100: ["Intro", "Hit One", "Hit Two", "Outro"], 400: ["Intro", "Hit One", "Hit Two", "Outro"]}
    album = release(100, "Great Album", days_ago=3)
    empty_deluxe = release(400, "Great Album (Expanded Edition)", days_ago=2)
    remix = release(12, "Hit One (Remix)", "Single", days_ago=1)
    other = release(500, "Fresh", "Single", days_ago=1, artist_id=2, artist="Other")
    catalog = {1: [release(10, "Hit One", "Single", 30), album, empty_deluxe, remix], 2: [other]}
    batch = models.Batch([remix, other, empty_deluxe, album], [release(900, "Soon", days_ago=-4)], catalog)
    return pipeline.prepare(batch)


def test_prepare_splits_cards_and_empty_reissues(itunes):
    p = prepared_sample(itunes)
    assert {r.album_id for r in p.to_show} == {100, 12, 500}
    assert [r.album_id for r in p.nothing_new] == [400]
    assert len(p.upcoming) == 1 and p.already_sorted == 0


def test_finalize_marks_notified_and_sets_empty_reissues_aside(itunes):
    p = prepared_sample(itunes)
    pipeline.finalize(p)
    assert storage.get_release(400).state == models.STATE_IGNORED
    assert storage.get_release(12).state == models.STATE_NEW  # les variantes restent à trier
    conn = storage.connect()
    assert conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 4
    conn.close()


def test_sorted_release_is_never_shown_again(itunes):
    prepared_sample(itunes)
    storage.set_state([100], models.STATE_ADDED)
    p = pipeline.prepare(models.Batch([release(100, "Great Album", days_ago=3)], [], {}))
    assert p.to_show == [] and p.already_sorted == 1
    assert storage.get_release(100).state == models.STATE_ADDED


def test_analysis_is_not_redone(itunes):
    prepared_sample(itunes)
    itunes.calls.clear()
    pipeline.prepare(models.Batch([release(100, "Great Album", days_ago=3)], [], {}))
    assert itunes.calls == []


def test_queue_walks_newest_first_without_looping(itunes):
    pipeline.finalize(prepared_sample(itunes))
    seen, cur = [], 0
    while True:
        nxt, total = storage.next_in_queue(models.STATE_NEW, cur)
        if nxt is None:
            break
        seen.append(nxt.album_id)
        cur = nxt.album_id
    assert sorted(seen) == [12, 100, 500] and len(set(seen)) == 3
    dates = [storage.get_release(i).release_date for i in seen]
    assert dates == sorted(dates, reverse=True)


def insert_rows(*rows):
    conn = storage.connect()
    conn.executemany(
        "INSERT INTO releases (album_id, artist, artist_id, title, kind, release_date, url, cover,"
        " track_count, total_tracks, new_tracks, note, state, analyzed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i, "Q", 1, t, "Album", clock.today().isoformat(), "u", "", 0, None, None, n, s, 1) for i, t, n, s in rows],
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
    assert storage.migrate() == 1
    assert storage.get_release(90).state == models.STATE_NEW
    assert storage.get_release(91).note == "🆕 8 inédits sur 14 · 6 déjà sortis"
    assert storage.get_release(92).analyzed is False
    assert storage.get_release(93).state == models.STATE_IGNORED
    assert storage.get_release(94).state == models.STATE_ADDED


def test_migrate_restores_variants_only_once():
    insert_rows((90, "Proj (Slowed)", "🔁 version alternative", "ignored"))
    storage.migrate()
    storage.set_state([90], models.STATE_IGNORED)
    assert storage.migrate() == 0
    assert storage.get_release(90).state == models.STATE_IGNORED
