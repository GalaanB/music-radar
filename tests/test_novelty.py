"""Analyse de nouveauté : singles déjà sortis, rééditions, versions alternatives."""

import pytest
import requests

from conftest import release
from musicradar import analysis, storage, text
from musicradar.sources import itunes as itunes_api


@pytest.mark.parametrize(("a", "b"), [
    ("Song (feat. X & Y)", "Song"),
    ("Song feat. X", "Song"),
    ("Déjà Vu!", "deja vu"),
    ("Song [Explicit]", "Song"),
])
def test_norm_title_ignores_featuring_and_accents(a, b):
    assert text.norm_title(a) == text.norm_title(b)


def test_norm_title_keeps_remix_distinct():
    assert text.norm_title("Song (Remix)") != text.norm_title("Song")


@pytest.mark.parametrize(("title", "expected"), [
    ("Album (Deluxe)", ("Album", True)),
    ("Album (Deluxe) [Expanded Edition]", ("Album", True)),
    ("Album - Deluxe Edition", ("Album", True)),
    ("Astroworld (5th Anniversary Edition)", ("Astroworld", True)),
    ("Special Delivery", ("Special Delivery", False)),
    ("Complete Me", ("Complete Me", False)),
])
def test_split_edition(title, expected):
    assert text.split_edition(title) == expected


@pytest.mark.parametrize("title", [
    "QRÖMELIFE (Slowed + Reverb)", "QRÖMELIFE slowed", "Projet - Sped Up",
    "Album (Instrumentals)", "Song (Remix)", "Song - Live",
])
def test_is_variant(title):
    assert text.is_variant(title)


@pytest.mark.parametrize("title", ["QRÖMELIFE", "Live Forever", "Versions of Me", "Song (feat. X)", "Deluxe Life"])
def test_is_not_variant(title):
    assert not text.is_variant(title)


def test_variant_base_and_label():
    assert text.variant_base("QRÖMELIFE slowed + reverb") == "QRÖMELIFE"
    assert text.variant_base("Song - Sped Up") == "Song"
    assert text.variant_base("Song (feat. X) [Remix]") == "Song (feat. X)"
    labels = [text.variant_label(t) for t in ["A (Sped Up)", "A slowed", "A (Instrumentals)", "A (Remix)", "A - Live"]]
    assert labels == ["sped up", "slowed", "instru", "remix", "live"]


@pytest.mark.parametrize(("name", "expected"), [
    ("Maverick “Almost Forever” Ep", ("EP", "Maverick “Almost Forever” Ep")),
    ("Titre (EP)", ("EP", "Titre (EP)")),
    ("SWAGGER - Single", ("Single", "SWAGGER")),
    ("Titre - EP", ("EP", "Titre")),
    ("Deep", ("Album", "Deep")),
])
def test_kind_and_title(name, expected):
    assert text.kind_and_title(name) == expected


# --- analyze_release ---------------------------------------------------------

def test_album_after_two_singles(itunes):
    itunes.tracks = {100: ["Intro", "Hit One (feat. Guest)", "Hit Two", "Deep Cut", "Outro"]}
    album = release(100, "Great Album", days_ago=3)
    catalog = {1: [release(10, "Hit One", "Single", 30), release(11, "Hit Two", "Single", 20), album]}
    analysis.analyze(album, catalog)
    assert (album.new_tracks, album.total_tracks) == (3, 5)
    assert album.note == "🆕 3 inédits sur 5 · 2 déjà sortis en single"


def test_single_released_same_day_as_album_is_not_counted(itunes):
    itunes.tracks = {100: ["Focus", "Other"]}
    album = release(100, "Album", days_ago=0)
    analysis.analyze(album, {1: [release(10, "Focus", "Single", 0), album]})
    assert album.note == "" and album.new_tracks is None


def test_deluxe_counts_only_new_tracks(itunes):
    itunes.tracks = {100: ["A", "B", "C"], 200: ["A", "B", "C", "D", "E"]}
    base, deluxe = release(100, "Album", days_ago=30), release(200, "Album (Deluxe)", days_ago=1)
    analysis.analyze(deluxe, {1: [base, deluxe]})
    assert deluxe.note == "♻️ réédition · 2 inédits sur 5"


def test_deluxe_with_nothing_new_is_set_aside(itunes):
    itunes.tracks = {100: ["A", "B"], 200: ["A", "B"]}
    base, deluxe = release(100, "Album", days_ago=30), release(200, "Album (Expanded Edition)", days_ago=1)
    analysis.analyze(deluxe, {1: [base, deluxe]})
    assert deluxe.note == "♻️ réédition · aucun inédit" and deluxe.nothing_new


def test_reissue_dated_same_day_as_original(itunes):
    itunes.tracks = {1: ["Intro", "A", "B", "C", "D", "E"], 2: ["Intro", "A", "B", "C", "D", "E", "F", "G"]}
    base, deluxe = release(1, "QRÖMELIFE", days_ago=10), release(2, "QRÖMELIFE (Deluxe)", days_ago=10)
    analysis.analyze(deluxe, {1: [base, deluxe]})
    assert deluxe.note == "♻️ réédition · 2 inédits sur 8"


def test_reissue_without_keyword_is_detected_by_shared_tracks(itunes):
    itunes.tracks = {1: ["Intro", "A", "B", "C", "D", "E"], 3: ["Intro", "A", "B", "C", "D", "E", "X"]}
    base, v2 = release(1, "QRÖMELIFE", days_ago=10), release(3, "QRÖMELIFE 2.0", days_ago=2)
    analysis.analyze(v2, {1: [base, v2]})
    assert v2.note == "♻️ réédition · 1 inédit sur 7"


def test_sequel_album_is_not_a_reissue(itunes):
    itunes.tracks = {1: ["Intro", "A", "B"], 4: ["Totally", "Different", "Songs"]}
    base, sequel = release(1, "QRÖMELIFE", days_ago=10), release(4, "QRÖMELIFE II", days_ago=1)
    analysis.analyze(sequel, {1: [base, sequel]})
    assert not sequel.note.startswith("♻️")


def test_slowed_album_points_to_original_without_fetching_tracks(itunes):
    base, slowed = release(1, "QRÖMELIFE", days_ago=10), release(5, "QRÖMELIFE (Slowed + Reverb)", days_ago=1)
    analysis.analyze(slowed, {1: [base, slowed]})
    assert slowed.note.startswith("🔁 version slowed de « QRÖMELIFE » (album du")
    assert slowed.total_tracks is None and itunes.calls == []


def test_remix_of_an_album_track_is_found_through_analyzed_tracks(itunes):
    itunes.tracks = {1: ["Intro", "E"]}
    album = release(1, "QRÖMELIFE", days_ago=10)
    analysis.analyze(album, {1: [album]})
    storage.save_releases([album])
    remix = release(6, "E (Remix)", "Single")
    analysis.analyze(remix, {1: [remix]})
    assert remix.note == "🔁 version remix de « E » (album « QRÖMELIFE »)"


def test_variant_without_known_original_only_shows_its_type(itunes):
    remix = release(7, "Inconnu (Sped Up)", "Single")
    analysis.analyze(remix, {1: [remix]})
    assert remix.note == "🔁 version sped up"


def test_no_reference_means_no_claim(itunes):
    itunes.tracks = {300: ["Same", "Songs"]}
    ep = release(300, "Lone EP", "EP")
    analysis.analyze(ep, {1: [ep]})
    assert ep.note == "" and ep.new_tracks is None


def test_track_list_is_cached(itunes):
    itunes.tracks = {100: ["A", "B"]}
    itunes_api.tracks_of(100)
    itunes_api.tracks_of(100)
    assert len(itunes.calls) == 1


def test_network_failure_leaves_release_to_be_analyzed_later(monkeypatch):
    def boom(url, params):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(itunes_api, "get_json", boom)
    album = release(100, "Album")
    analysis.analyze(album, {1: [album]})
    assert album.analyzed is False and album.note == ""


def test_album_lists_the_tracks_already_out(itunes):
    itunes.tracks = {100: ["Intro", "Hit One (feat. Guest)", "Hit Two", "Deep Cut", "Outro"]}
    album = release(100, "Great Album", days_ago=3)
    catalog = {1: [release(10, "Hit One", "Single", 30), release(11, "Hit Two", "Single", 20), album]}
    analysis.analyze(album, catalog)
    assert album.detail == "Déjà sortis : Hit One · Hit Two"
    storage.save_releases([album])
    assert storage.get_release(100).detail == album.detail


def test_reissue_lists_its_new_tracks(itunes):
    itunes.tracks = {
        200: ["A", "B", "C"],
        201: ["A", "B", "C", "Bonus (feat. X)"],
    }
    base = release(200, "Album", days_ago=60)
    deluxe = release(201, "Album (Deluxe)", days_ago=1)
    analysis.analyze(deluxe, {1: [base, deluxe]})
    assert deluxe.note == "♻️ réédition · 1 inédit sur 4"
    assert deluxe.detail == "Inédit : Bonus"


def test_track_list_is_capped():
    titles = [f"T{i}" for i in range(11)]
    assert text.track_list("Déjà sortis", titles) == "Déjà sortis : T0 · T1 · T2 · T3 · T4 · T5 · T6 · T7 … +3"


# --- « (Deluxe Version) » : une réédition, pas une version alternative -------------

@pytest.mark.parametrize("title", [
    "Album (Deluxe Version)", "Album (Version Deluxe)", "Album (Bonus Track Version)",
    "Album (Extended Version)", "Album - Deluxe Version",
])
def test_edition_named_version_is_not_a_variant_for_an_album(title):
    assert not text.is_variant(title, album=True)
    assert text.split_edition(title) == ("Album", True)
    assert not release(1, title).is_variant and not release(1, title, "EP").is_variant


@pytest.mark.parametrize("title", [
    "Album (Live Version)", "Album (Deluxe Live Version)", "Album (Deluxe) [Slowed]",
    "Album (Acoustic Version)", "Album (Deluxe Edition) - Remix",
])
def test_real_variants_of_an_album_are_still_variants(title):
    assert text.is_variant(title, album=True)


def test_single_named_extended_version_keeps_its_variant_note(itunes):
    single = release(8, "Hit (Extended Version)", "Single")
    analysis.analyze(single, {1: [release(7, "Hit", "Single", 30), single]})
    assert single.is_variant and single.note.startswith("🔁 version alternative de « Hit »")


def test_deluxe_version_counts_its_new_tracks(itunes):
    itunes.tracks = {100: ["A", "B", "C"], 200: ["A", "B", "C", "D", "E"]}
    base, deluxe = release(100, "Album", days_ago=30), release(200, "Album (Deluxe Version)", days_ago=1)
    analysis.analyze(deluxe, {1: [base, deluxe]})
    assert deluxe.note == "♻️ réédition · 2 inédits sur 5"
    assert deluxe.detail == "Inédits : D · E"


def test_deluxe_version_can_be_the_original_of_a_later_edition(itunes):
    itunes.tracks = {200: ["A", "B", "C"], 300: ["A", "B", "C", "D"]}
    deluxe = release(200, "Album (Deluxe Version)", days_ago=30)
    complete = release(300, "Album (Complete Edition)", days_ago=1)
    analysis.analyze(complete, {1: [deluxe, complete]})
    assert complete.note == "♻️ réédition · 1 inédit sur 4"
