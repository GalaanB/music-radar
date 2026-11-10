"""Plusieurs fiches Apple pour une même sortie (explicite / clean, doublons)."""


from conftest import collection, release
from musicradar import models, pipeline, scan, storage

MAVERICK = "Maverick “Almost Forever” Ep"


def resolve(*pairs):
    conn = storage.connect()
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
    ids = sorted(r.album_id for r in scan.catchup_batch(10).releases)
    assert ids == [10, 11, 6797312643]  # le single et l'album homonymes restent deux sorties


def test_same_release_seen_through_two_followed_artists(itunes):
    resolve(("Lil Uzi Vert", 1), ("Autre", 2))
    item = collection(6797312643, MAVERICK, artist="Lil Uzi Vert")
    itunes.albums = {1: [item], 2: [dict(item)]}
    assert [r.album_id for r in scan.catchup_batch(10).releases] == [6797312643]


def test_db_twin_is_absorbed_and_your_choice_kept(itunes):
    resolve(("Lil Uzi Vert", 1))
    itunes.albums = {1: [collection(6797312643, MAVERICK, artist="Lil Uzi Vert")]}
    clean = release(6796859137, MAVERICK, "EP", artist="Lil Uzi Vert", state=models.STATE_ADDED)
    storage.save_releases([clean])
    p = pipeline.prepare(scan.catchup_batch(10))
    assert storage.get_release(6796859137) is None
    assert storage.get_release(6797312643).state == models.STATE_ADDED
    assert p.to_show == []


def test_daily_does_not_notify_a_new_card_of_an_already_notified_release(itunes):
    resolve(("Lil Uzi Vert", 1))
    itunes.albums = {1: [collection(1, MAVERICK, artist="Lil Uzi Vert")]}
    pipeline.finalize(pipeline.prepare(scan.daily_batch()))
    itunes.albums = {1: [collection(999, MAVERICK, artist="Lil Uzi Vert")]}
    assert scan.daily_batch().releases == []


# --- sorties homonymes : jumelles seulement si leurs dates sont proches ------------

def test_two_singles_with_the_same_title_months_apart_are_two_releases(itunes):
    resolve(("Josman", 1))
    itunes.albums = {1: [
        collection(1, "Freestyle - Single", days_ago=80, artist="Josman"),
        collection(2, "Freestyle - Single", days_ago=2, artist="Josman"),
    ]}
    assert sorted(r.album_id for r in scan.catchup_batch(90).releases) == [1, 2]


def test_cards_up_to_thirty_days_apart_are_still_one_release(itunes):
    resolve(("Josman", 1))
    itunes.albums = {1: [
        collection(1, "Projet", days_ago=30, artist="Josman"),
        collection(2, "Projet", days_ago=0, artist="Josman"),
        collection(3, "Autre", days_ago=31, artist="Josman"),
        collection(4, "Autre", days_ago=0, artist="Josman"),
    ]}
    assert sorted(r.album_id for r in scan.catchup_batch(90).releases) == [1, 3, 4]


def test_new_release_named_like_an_old_one_is_notified_and_keeps_its_own_sorting(itunes):
    resolve(("Josman", 1))
    old = release(1, "Freestyle", "Single", days_ago=700, artist="Josman", state=models.STATE_ADDED)
    storage.save_releases([old])
    storage.mark_seen([old])
    itunes.albums = {1: [collection(2, "Freestyle - Single", days_ago=1, artist="Josman")]}
    batch = scan.daily_batch()
    assert [r.album_id for r in batch.releases] == [2]
    p = pipeline.prepare(batch)
    assert [r.album_id for r in p.to_show] == [2] and p.already_sorted == 0
    assert storage.get_release(1).state == models.STATE_ADDED   # l'ancienne n'est pas absorbée
    assert storage.get_release(2).state == models.STATE_NEW


def test_featured_version_released_months_after_the_solo_one_is_notified(itunes):
    resolve(("Josman", 1))
    solo = release(1, "Song", "Single", days_ago=100, artist="Josman")
    storage.save_releases([solo])
    storage.mark_seen([solo])
    itunes.albums = {1: [collection(2, "Song (feat. Guest) - Single", days_ago=1, artist="Josman")]}
    assert [r.album_id for r in scan.daily_batch().releases] == [2]


def test_recent_twin_in_the_database_is_still_absorbed_in_catchup(itunes):
    resolve(("Josman", 1))
    storage.save_releases([release(1, "Projet", days_ago=20, artist="Josman", state=models.STATE_IGNORED)])
    itunes.albums = {1: [collection(2, "Projet", days_ago=3, artist="Josman")]}
    p = pipeline.prepare(scan.catchup_batch(10))
    assert storage.get_release(1) is None and storage.get_release(2).state == models.STATE_IGNORED
    assert p.to_show == []


def test_titles_without_any_letter_are_compared_as_written():
    from musicradar import text

    assert text.norm_title("?") == "?" and text.norm_title("!!!") == "!!!"
    assert text.norm_title("🖤") != text.norm_title("💔")
    assert text.norm_title("Déjà Vu!") == "deja vu" and text.norm_title("") == ""
    question, bang, twin = release(1, "?", "Single"), release(2, "!!!", "Single"), release(3, "?", "Single")
    assert len(scan.dedupe([question, bang])) == 2
    assert len(scan.dedupe([question, twin])) == 1


def test_symbol_track_is_only_known_from_the_single_of_the_same_title(itunes):
    from musicradar import analysis

    itunes.tracks = {100: ["?", "Titre"], 101: ["?", "Titre"]}
    album = release(100, "Album", days_ago=1)
    analysis.analyze(album, {1: [release(10, "!!!", "Single", 30), album]})
    assert album.note == "" and album.new_tracks is None
    other = release(101, "Album bis", days_ago=1)
    analysis.analyze(other, {1: [release(11, "?", "Single", 30), other]})
    assert other.note == "🆕 1 inédit sur 2 · 1 déjà sorti en single"
