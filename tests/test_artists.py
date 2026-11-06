"""artists.txt : lecture, ajout, retrait, résolution des ID et commande /ajouter."""


from conftest import collection
from musicradar import artists, config, storage, text


def artist_lines():
    return [ln for ln in config.ARTISTS_FILE.read_text(encoding="utf-8").splitlines() if artists.is_artist_line(ln)]


def db_artists():
    conn = storage.connect()
    rows = dict(conn.execute("SELECT name, itunes_id FROM artists").fetchall())
    conn.close()
    return rows


def test_sample_file_is_sorted_by_sort_key():
    lines = artist_lines()
    assert lines == sorted(lines, key=text.sort_key)


def test_add_inserts_at_alphabetical_position_and_keeps_header():
    assert artists.add("Aya Nakamura", 111, "Aya Nakamura", forced=False)
    lines = artist_lines()
    i = lines.index("Aya Nakamura")
    assert (lines[i - 1], lines[i + 1]) == ("21 Savage", "Damso")
    assert config.ARTISTS_FILE.read_text(encoding="utf-8").startswith("# Un artiste par ligne.")
    assert db_artists()["Aya Nakamura"] == 111


def test_add_refuses_duplicates_ignoring_case_and_punctuation():
    assert not artists.add("drake", 1, "Drake", forced=False)
    assert artists.in_file("Tyler The Creator") == "Tyler, The Creator"


def test_add_forced_id_writes_name_pipe_id():
    artists.add("ZZ Top", 222, "ZZ Top", forced=True)
    assert artist_lines()[-1] == "ZZ Top | 222"


def test_add_forced_id_updates_existing_line():
    assert artists.add("Drake", 999, "Drake", forced=True)
    assert "Drake | 999" in artist_lines()
    assert "Drake" not in artist_lines()


def test_remove_exact_name_also_removes_from_db():
    artists.add("Aya Nakamura", 111, "Aya Nakamura", forced=False)
    assert artists.remove("aya nakamura") == {"status": "removed", "names": ["Aya Nakamura"]}
    assert "Aya Nakamura" not in db_artists()


def test_remove_exact_match_wins_over_partial():
    res = artists.remove("Selug")
    assert res["names"] == ["Selug"]
    assert artists.in_file("Selug Archives")


def test_remove_partial_unique_and_ambiguous():
    assert artists.remove("lil")["status"] == "ambiguous"
    assert artists.remove("tecca") == {"status": "removed", "names": ["Lil Tecca"]}
    assert artists.remove("introuvable") == {"status": "none"}


def test_resolve_purges_artists_removed_from_file(itunes):
    itunes.search = {
        n: [{"artistId": i, "artistName": n, "primaryGenreName": "Hip-Hop/Rap"}]
        for i, n in enumerate(artists.names(), 1)
    }
    artists.resolve()
    config.ARTISTS_FILE.write_text(config.ARTISTS_FILE.read_text().replace("Damso\n", ""), encoding="utf-8")
    bilan = artists.resolve()
    assert bilan["removed"] == 1
    assert "Damso" not in db_artists()


def test_resolve_never_purges_when_file_looks_empty(itunes):
    itunes.search = {"Drake": [{"artistId": 1, "artistName": "Drake", "primaryGenreName": "Rap"}]}
    config.ARTISTS_FILE.write_text("Drake\n", encoding="utf-8")
    artists.resolve()
    config.ARTISTS_FILE.write_text("# vide\n", encoding="utf-8")
    assert artists.resolve()["removed"] == 0
    assert "Drake" in db_artists()


def test_resolve_does_not_flag_same_id_returned_twice_as_homonym(itunes):
    config.ARTISTS_FILE.write_text("6LACK\n", encoding="utf-8")
    hit = {"artistId": 5, "artistName": "6LACK", "primaryGenreName": "R&B/Soul"}
    itunes.search = {"6LACK": [hit, dict(hit)]}
    assert artists.resolve()["to_check"] == []


# --- /ajouter ---------------------------------------------------------------

def test_prepare_add_existing_and_unknown(itunes):
    assert artists.prepare_add("DRAKE") == {"kind": "exists", "name": "Drake"}
    assert artists.prepare_add("Inconnu") == {"kind": "none"}


def test_prepare_add_single_exact_match_is_ready(itunes):
    itunes.search = {"Lomi": [{"artistId": 11, "artistName": "Lomi", "primaryGenreName": "Rap", "artistLinkUrl": "u"}]}
    res = artists.prepare_add("Lomi")
    assert res["kind"] == "ready" and res["id"] == 11 and not res["forced"]


def test_prepare_add_homonyms_offer_choice_with_last_release(itunes):
    itunes.search = {"Muse": [
        {"artistId": 21, "artistName": "Muse", "primaryGenreName": "Alternative"},
        {"artistId": 22, "artistName": "Muse", "primaryGenreName": "Hip-Hop/Rap"},
    ]}
    itunes.albums = {21: [collection(1, "Dernier - Single", days_ago=30)], 22: []}
    res = artists.prepare_add("Muse")
    assert res["kind"] == "choose" and len(res["candidates"]) == 2
    assert res["candidates"][0]["last"].startswith("Dernier")
    assert res["candidates"][1]["last"] == "aucune sortie"


def test_prepare_add_accepts_apple_music_url_and_name_pipe_id(itunes):
    itunes.artists = {99887: {"wrapperType": "artist", "artistId": 99887, "artistName": "Sam",
                              "primaryGenreName": "Rap", "artistLinkUrl": "u"}}
    for url in ("https://music.apple.com/fr/artist/sam/99887", "https://music.apple.com/us/artist/99887?l=fr"):
        res = artists.prepare_add(url)
        assert (res["kind"], res["id"], res["forced"]) == ("ready", 99887, True)
    res = artists.prepare_add("Mon Sam | 99887")
    assert res["name"] == "Mon Sam" and res["forced"]

