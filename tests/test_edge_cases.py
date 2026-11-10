"""Cas limites peu visibles : départages, bornes de dates, pannes partielles, commandes du bot."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

from conftest import collection, recording, release, song
from musicradar import __main__ as cli
from musicradar import analysis, artists, clock, config, formatting, models, scan, storage
from musicradar.bot import artists as bot_artists
from musicradar.bot import recaps, settings
from musicradar.sources import itunes as itunes_api

CHAT = int(settings.CHAT_ID)


@pytest.fixture(autouse=True)
def fast_bot(monkeypatch):
    monkeypatch.setattr(settings, "SEND_PAUSE_S", 0)


def follow(*pairs):
    conn = storage.connect()
    conn.executemany("INSERT INTO artists VALUES (?, ?, ?)", [(n, i, n) for n, i in pairs])
    conn.commit()
    conn.close()


def run(coro):
    return asyncio.run(coro)


def ctx(args=()):
    return SimpleNamespace(bot=AsyncMock(), bot_data={}, args=list(args))


def message_update():
    return SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))


def query(data):
    q = MagicMock()
    q.data = data
    q.message = SimpleNamespace(chat=SimpleNamespace(id=CHAT))
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    return SimpleNamespace(callback_query=q), q


def texts(fake_bot):
    return [c.kwargs["text"] for c in fake_bot.send_message.call_args_list]


# --- dédoublonnage -----------------------------------------------------------------

def test_between_two_cards_the_most_complete_then_the_oldest_wins():
    short = release(1, "Album", track_count=10, explicit=True)
    full = release(2, "Album", track_count=12, explicit=True)
    older = release(3, "Album", days_ago=1, track_count=12, explicit=True)
    assert [r.album_id for r in scan.dedupe([short, full])] == [2]
    assert [r.album_id for r in scan.dedupe([full, older])] == [3]
    clean_but_full = release(4, "Album", track_count=20, explicit=False)
    assert [r.album_id for r in scan.dedupe([clean_but_full, short])] == [1]  # l'explicite d'abord


def test_a_remix_and_another_artist_are_not_twins():
    rels = [
        release(1, "Song", "Single"),
        release(2, "Song (Remix)", "Single"),
        release(3, "Song", "Single", artist="Autre"),
        release(4, "Song", "EP"),
    ]
    assert len(scan.dedupe(rels)) == 4


# --- bornes des lots ---------------------------------------------------------------

def test_daily_and_friday_date_bounds(itunes):
    follow(("Josman", 1))
    days = {10: 0, 11: config.DAILY_LOOKBACK_DAYS, 12: config.DAILY_LOOKBACK_DAYS + 1,
            20: -1, 21: -config.UPCOMING_DAYS, 22: -config.UPCOMING_DAYS - 1}
    itunes.albums = {1: [collection(i, f"T{i} - Single", days_ago=d, artist="Josman") for i, d in days.items()]}
    assert sorted(r.album_id for r in scan.daily_batch().releases) == [10, 11]
    assert scan.daily_batch().upcoming == []
    weekly = scan.weekly_batch()
    assert sorted(r.album_id for r in weekly.releases) == [10, 11]
    assert [r.album_id for r in weekly.upcoming] == [20, 21]  # la plus proche d'abord


def test_scan_without_any_resolved_artist_is_an_explicit_error():
    with pytest.raises(RuntimeError, match="Aucun artiste résolu"):
        scan.daily_batch()


# --- analyse de nouveauté ----------------------------------------------------------

def test_novelty_can_be_disabled(itunes, monkeypatch):
    monkeypatch.setattr(config, "NOVELTY_ENABLED", False)
    album = release(100, "Album", days_ago=1)
    analysis.analyze(album, {1: [release(10, "Hit", "Single", 30), album]})
    assert album.analyzed and album.note == "" and itunes.calls == []


def test_a_single_is_not_analyzed(itunes):
    single = release(100, "Hit", "Single")
    analysis.analyze(single, {1: [single]})
    assert single.analyzed and single.note == "" and itunes.calls == []


def test_album_without_track_list_gets_no_claim(itunes):
    album = release(100, "Album")
    analysis.analyze(album, {1: [album]})
    assert album.analyzed and album.note == "" and album.total_tracks is None


def test_ep_made_only_of_singles_is_still_shown(itunes):
    itunes.tracks = {100: ["A", "B"]}
    ep = release(100, "Compil", "EP", days_ago=1)
    analysis.analyze(ep, {1: [release(10, "A", "Single", 30), release(11, "B", "Single", 20), ep]})
    assert ep.note == "🔁 aucun inédit · 2 déjà sortis en single"
    assert ep.detail == "Déjà sortis : A · B" and not ep.nothing_new


def test_tracks_known_from_a_single_and_from_an_earlier_album(itunes):
    earlier = release(50, "Premier album", days_ago=200)
    storage.save_releases([earlier])
    storage.save_tracks(50, ["B"])
    itunes.tracks = {100: ["A", "B", "C"]}
    album = release(100, "Best of", days_ago=1)
    analysis.analyze(album, {1: [release(10, "A", "Single", 30), earlier, album]})
    assert album.note == "🆕 1 inédit sur 3 · 2 déjà sortis"  # pas « en single » : B vient d'un album


def test_reissue_whose_original_cannot_be_fetched(itunes, monkeypatch):
    itunes.tracks = {200: ["A", "B", "C"]}
    real = itunes.__call__

    def flaky(url, params):
        if params.get("entity") == "song" and params["id"] == 100:
            raise requests.ConnectionError("down")
        return real(url, params)

    monkeypatch.setattr(itunes_api, "get_json", flaky)
    base, deluxe = release(100, "Album", days_ago=30), release(200, "Album (Deluxe)", days_ago=1)
    analysis.analyze(deluxe, {1: [base, deluxe]})
    assert deluxe.note == "♻️ réédition" and deluxe.new_tracks is None and not deluxe.nothing_new
    assert deluxe.analyzed


# --- featurings --------------------------------------------------------------------

@pytest.fixture
def sam(itunes, mb):
    follow(("SAM", 7))
    itunes.albums = {7: []}
    itunes.search = {"SAM": [{"artistId": 7, "artistName": "SAM"}]}
    mb.artists = {"SAM": [{"id": "mb-sam", "name": "SAM"}]}
    mb.apple_links = {"mb-sam": [7]}
    return itunes, mb


def test_apple_track_must_have_the_exact_title(sam):
    itunes, mb = sam
    ago = (clock.today() - timedelta(days=3)).isoformat()
    mb.recordings = {"mb-sam": [recording("Feu", [("Autre", "mb-x"), ("SAM", "mb-sam")], ago)]}
    itunes.songs = {"Autre Feu": [
        song(7001, "Feu Follet (feat. SAM)", "Autre", "Projet", 70, days_ago=3),
        song(7002, "Feu", "Quelqu'un d'autre", "Projet", 71, days_ago=3),
        song(7003, "Feu (feat. SAM)", "Autre", "Projet", 70, days_ago=3),
    ]}
    assert [r.album_id for r in scan.catchup_batch(14).releases] == [7003]


def test_feature_announced_for_next_week_is_listed_as_upcoming(sam):
    itunes, mb = sam
    soon = (clock.today() + timedelta(days=3)).isoformat()
    mb.recordings = {"mb-sam": [recording("Demain", [("Autre", "mb-x"), ("SAM", "mb-sam")], soon)]}
    itunes.songs = {"Autre Demain": [song(7100, "Demain (feat. SAM)", "Autre", "Projet", 71, days_ago=-3)]}
    weekly = scan.weekly_batch()
    assert weekly.releases == [] and [r.album_id for r in weekly.upcoming] == [7100]
    assert scan.daily_batch().releases == []


# --- file /trier, mise en forme ----------------------------------------------------

def test_queue_restarts_from_the_top_when_the_shown_release_is_gone():
    storage.save_releases([release(1, "A", days_ago=2), release(2, "B", days_ago=1)])
    nxt, total = storage.next_in_queue(models.STATE_NEW, after_id=999)
    assert (nxt.album_id, total) == (2, 2)


def test_empty_list_and_set_aside_reissues_messages():
    assert formatting.compact_list("Titre", [], "Rien.") == ["<b>Titre</b>\n\nRien."]
    assert formatting.nothing_new([]) == []
    msg = formatting.nothing_new([release(1, "Album (Deluxe)", artist="A&B")])[0]
    assert "Rééditions sans nouveauté" in msg and "<b>A&amp;B</b>" in msg


# --- artistes ----------------------------------------------------------------------

def test_resolve_reports_network_errors_and_doubtful_matches(itunes, monkeypatch):
    config.ARTISTS_FILE.write_text("En panne\nJosman\n", encoding="utf-8")
    itunes.search = {"Josman": [{"artistId": 5, "artistName": "Jos Man", "primaryGenreName": "Rock"}]}
    real = itunes.__call__

    def flaky(url, params):
        if params.get("term") == "En panne":
            raise requests.ConnectionError("down")
        return real(url, params)

    monkeypatch.setattr(itunes_api, "get_json", flaky)
    bilan = artists.resolve()
    assert bilan["not_found"] == ["En panne (erreur réseau)"] and bilan["resolved"] == 1
    assert bilan["to_check"] == ["Josman → Jos Man [Rock] (id 5) : nom différent, genre inattendu"]


def test_resolve_reports_unknown_artists(itunes):
    config.ARTISTS_FILE.write_text("Introuvable\n", encoding="utf-8")
    assert artists.resolve()["not_found"] == ["Introuvable"]


# --- commandes du bot --------------------------------------------------------------

@pytest.mark.parametrize(("args", "days"), [([], 30), (["7"], 7), (["9999"], 365), (["0"], 1), (["-3"], 1)])
def test_rattrapage_reads_and_clamps_its_argument(monkeypatch, args, days):
    asked = []

    async def fake_catchup(bot, n):
        asked.append(n)

    monkeypatch.setattr(recaps, "do_catchup", fake_catchup)
    run(recaps.cmd_rattrapage(message_update(), ctx(args)))
    assert asked == [days]


def test_rattrapage_with_a_bad_argument_explains_the_usage(monkeypatch):
    monkeypatch.setattr(recaps, "do_catchup", AsyncMock())
    upd = message_update()
    run(recaps.cmd_rattrapage(upd, ctx(["bientôt"])))
    assert "Usage" in upd.message.reply_text.call_args.args[0] and not recaps.do_catchup.called


@pytest.mark.parametrize(("command", "target", "label"), [
    (recaps.cmd_jour, "do_daily", "/jour"),
    (recaps.cmd_semaine, "do_weekly", "/semaine"),
    (recaps.cmd_rattrapage, "do_catchup", "/rattrapage"),
    (recaps.cmd_actualiser, "do_resolve", "/actualiser"),
])
def test_a_failing_command_is_reported_not_raised(monkeypatch, command, target, label):
    monkeypatch.setattr(recaps, target, AsyncMock(side_effect=RuntimeError("Apple ne répond plus")))
    c = ctx()
    run(command(message_update(), c))
    assert f"Erreur pendant {label}" in texts(c.bot)[-1]


def test_actualiser_sends_the_report_in_several_messages_when_long(monkeypatch):
    missing = [f"Artiste introuvable numéro {i}" for i in range(300)]
    monkeypatch.setattr(artists, "resolve", lambda: {
        "resolved": 2, "cached": 180, "removed": 1, "not_found": missing,
        "to_check": ["SAM → Sam [Pop] (id 7) : genre inattendu"]})
    c = ctx()
    run(recaps.cmd_actualiser(message_update(), c))
    sent = texts(c.bot)
    assert len(sent) > 1 and all(len(t) <= 4000 for t in sent)
    whole = "".join(sent)
    assert "2 résolu(s), 180 déjà connu(s)" in whole and "1 retiré(s)" in whole
    assert "numéro 299" in whole and "À vérifier" in whole


def test_recap_interrupted_between_two_cards_marks_nothing_as_notified():
    bot = AsyncMock()
    bot.send_message.side_effect = [None, None, RuntimeError("telegram down")]
    batch = models.Batch([release(1, "A"), release(2, "B"), release(3, "C")], [], {})
    with pytest.raises(RuntimeError):
        run(recaps.run_recap(bot, lambda: batch, "t", "vide", False))
    assert storage.seen_ids() == set()
    assert storage.count_state(models.STATE_NEW) == 3  # enregistrées, donc triables depuis les cartes reçues


def test_verify_change_without_any_homonym_keeps_the_card(itunes):
    follow(("Solo", 5))
    itunes.search = {"Solo": [{"artistId": 5, "artistName": "Solo"}]}
    u, q = query("vf:ch:5")
    run(bot_artists.on_verify_button(u, ctx()))
    assert "Aucun autre « Solo »" in q.edit_message_text.call_args.args[0]
    assert q.edit_message_text.call_args.kwargs["reply_markup"] is not None


def test_verify_buttons_that_no_longer_apply():
    u, q = query("vf:pk:zzzzzz:0")
    run(bot_artists.on_verify_button(u, ctx()))
    assert q.answer.call_args.kwargs.get("show_alert") is True
    u, q = query("vf:ok:12345")
    run(bot_artists.on_verify_button(u, ctx()))
    assert "n'est plus suivi" in q.edit_message_text.call_args.args[0]
    u, q = query("vf:ok:pas-un-id")
    run(bot_artists.on_verify_button(u, ctx()))
    assert not q.edit_message_text.called


def test_ajouter_without_argument_or_result(itunes):
    upd = message_update()
    run(bot_artists.cmd_ajouter(upd, ctx()))
    assert "Usage" in upd.message.reply_text.call_args.args[0]
    c = ctx(["Personne"])
    run(bot_artists.cmd_ajouter(message_update(), c))
    assert "Aucun artiste trouvé" in texts(c.bot)[-1]
    c = ctx(["drake"])
    run(bot_artists.cmd_ajouter(message_update(), c))
    assert "Déjà dans la liste" in texts(c.bot)[-1] and "Drake" in texts(c.bot)[-1]


# --- outil en ligne de commande ----------------------------------------------------

def test_cli_prints_help_and_a_daily_preview(itunes, capsys):
    cli.main(["musicradar"])
    assert "python -m musicradar daily" in capsys.readouterr().out
    follow(("Josman", 1))
    itunes.albums = {1: [collection(10, "Neuf - Single", artist="Josman"), collection(11, "Bientôt", days_ago=-2, artist="Josman")]}
    cli.main(["musicradar", "daily"])
    out = capsys.readouterr().out
    assert "Nouvelles sorties" in out and "Neuf" in out and "Bientôt" not in out
    cli.main(["musicradar", "weekly"])
    assert "À venir" in capsys.readouterr().out
    cli.main(["musicradar", "catchup", "3"])
    assert "Rattrapage (3 jours)" in capsys.readouterr().out
    assert storage.seen_ids() == set()  # lecture seule : rien n'est marqué notifié
