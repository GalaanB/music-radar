"""artists.txt : lecture, ajout, retrait, résolution des ID et commande /ajouter."""

import radar
from conftest import collection


def artist_lines():
    return [ln for ln in radar.ARTISTS_FILE.read_text(encoding="utf-8").splitlines() if radar._is_artist_line(ln)]


def db_artists():
    conn = radar._db()
    rows = dict(conn.execute("SELECT name, itunes_id FROM artists").fetchall())
    conn.close()
    return rows


def test_sample_file_is_sorted_by_sort_key():
    lines = artist_lines()
    assert lines == sorted(lines, key=radar._sort_key)


def test_add_inserts_at_alphabetical_position_and_keeps_header():
    assert radar.add_artist("Aya Nakamura", 111, "Aya Nakamura", forced=False)
    lines = artist_lines()
    i = lines.index("Aya Nakamura")
    assert (lines[i - 1], lines[i + 1]) == ("21 Savage", "Damso")
    assert radar.ARTISTS_FILE.read_text(encoding="utf-8").startswith("# Un artiste par ligne.")
    assert db_artists()["Aya Nakamura"] == 111


def test_add_refuses_duplicates_ignoring_case_and_punctuation():
    assert not radar.add_artist("drake", 1, "Drake", forced=False)
    assert radar.artist_in_file("Tyler The Creator") == "Tyler, The Creator"


def test_add_forced_id_writes_name_pipe_id():
    radar.add_artist("ZZ Top", 222, "ZZ Top", forced=True)
    assert artist_lines()[-1] == "ZZ Top | 222"


def test_add_forced_id_updates_existing_line():
    assert radar.add_artist("Drake", 999, "Drake", forced=True)
    assert "Drake | 999" in artist_lines()
    assert "Drake" not in artist_lines()


def test_remove_exact_name_also_removes_from_db():
    radar.add_artist("Aya Nakamura", 111, "Aya Nakamura", forced=False)
    assert radar.remove_artist("aya nakamura") == {"status": "removed", "names": ["Aya Nakamura"]}
    assert "Aya Nakamura" not in db_artists()


def test_remove_exact_match_wins_over_partial():
    res = radar.remove_artist("Selug")
    assert res["names"] == ["Selug"]
    assert radar.artist_in_file("Selug Archives")


def test_remove_partial_unique_and_ambiguous():
    assert radar.remove_artist("lil")["status"] == "ambiguous"
    assert radar.remove_artist("tecca") == {"status": "removed", "names": ["Lil Tecca"]}
    assert radar.remove_artist("introuvable") == {"status": "none"}


def test_resolve_purges_artists_removed_from_file(itunes):
    itunes.search = {
        n: [{"artistId": i, "artistName": n, "primaryGenreName": "Hip-Hop/Rap"}]
        for i, n in enumerate(radar.list_artist_names(), 1)
    }
    radar.resolve_artists()
    radar.ARTISTS_FILE.write_text(radar.ARTISTS_FILE.read_text().replace("Damso\n", ""), encoding="utf-8")
    bilan = radar.resolve_artists()
    assert bilan["removed"] == 1
    assert "Damso" not in db_artists()


def test_resolve_never_purges_when_file_looks_empty(itunes):
    itunes.search = {"Drake": [{"artistId": 1, "artistName": "Drake", "primaryGenreName": "Rap"}]}
    radar.ARTISTS_FILE.write_text("Drake\n", encoding="utf-8")
    radar.resolve_artists()
    radar.ARTISTS_FILE.write_text("# vide\n", encoding="utf-8")
    assert radar.resolve_artists()["removed"] == 0
    assert "Drake" in db_artists()


def test_resolve_does_not_flag_same_id_returned_twice_as_homonym(itunes):
    radar.ARTISTS_FILE.write_text("6LACK\n", encoding="utf-8")
    hit = {"artistId": 5, "artistName": "6LACK", "primaryGenreName": "R&B/Soul"}
    itunes.search = {"6LACK": [hit, dict(hit)]}
    assert radar.resolve_artists()["to_check"] == []


# --- /ajouter ---------------------------------------------------------------

def test_prepare_add_existing_and_unknown(itunes):
    assert radar.prepare_add("DRAKE") == {"kind": "exists", "name": "Drake"}
    assert radar.prepare_add("Inconnu") == {"kind": "none"}


def test_prepare_add_single_exact_match_is_ready(itunes):
    itunes.search = {"Lomi": [{"artistId": 11, "artistName": "Lomi", "primaryGenreName": "Rap", "artistLinkUrl": "u"}]}
    res = radar.prepare_add("Lomi")
    assert res["kind"] == "ready" and res["id"] == 11 and not res["forced"]


def test_prepare_add_homonyms_offer_choice_with_last_release(itunes):
    itunes.search = {"Muse": [
        {"artistId": 21, "artistName": "Muse", "primaryGenreName": "Alternative"},
        {"artistId": 22, "artistName": "Muse", "primaryGenreName": "Hip-Hop/Rap"},
    ]}
    itunes.albums = {21: [collection(1, "Dernier - Single", days_ago=30)], 22: []}
    res = radar.prepare_add("Muse")
    assert res["kind"] == "choose" and len(res["candidates"]) == 2
    assert res["candidates"][0]["last"].startswith("Dernier")
    assert res["candidates"][1]["last"] == "aucune sortie"


def test_prepare_add_accepts_apple_music_url_and_name_pipe_id(itunes):
    itunes.artists = {99887: {"wrapperType": "artist", "artistId": 99887, "artistName": "Sam",
                              "primaryGenreName": "Rap", "artistLinkUrl": "u"}}
    for text in ("https://music.apple.com/fr/artist/sam/99887", "https://music.apple.com/us/artist/99887?l=fr"):
        res = radar.prepare_add(text)
        assert (res["kind"], res["id"], res["forced"]) == ("ready", 99887, True)
    res = radar.prepare_add("Mon Sam | 99887")
    assert res["name"] == "Mon Sam" and res["forced"]

