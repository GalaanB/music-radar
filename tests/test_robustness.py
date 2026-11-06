"""Robustesse : envois ratés, pannes d'Apple / MusicBrainz, sauvegardes, démarrage."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

from conftest import collection, recording, release, song
from musicradar import artists, clock, config, models, pipeline, scan, storage
from musicradar.bot import recaps, settings, ui
from musicradar.sources import itunes as itunes_api
from musicradar.sources import musicbrainz


def follow(*pairs):
    conn = storage.connect()
    conn.executemany("INSERT INTO artists VALUES (?, ?, ?)", [(n, i, n) for n, i in pairs])
    conn.commit()
    conn.close()


def run(coro):
    return asyncio.run(coro)


# --- notifications perdues ---------------------------------------------------------

def test_release_whose_delivery_failed_is_sent_again_next_day(itunes):
    follow(("Josman", 1))
    itunes.albums = {1: [collection(10, "TOUS MES DIAMANTS - Single", days_ago=1, artist="Josman")]}
    pipeline.prepare(scan.daily_batch())  # enregistrée… puis l'envoi Telegram échoue : pas de finalize
    assert [r.album_id for r in scan.daily_batch().releases] == [10]


# --- pannes ---------------------------------------------------------------------

def test_scan_stops_quickly_when_apple_is_down(itunes, monkeypatch):
    follow(*[(f"A{i}", i) for i in range(1, 30)])

    def down(url, params):
        raise requests.ConnectionError("Apple down")

    monkeypatch.setattr(itunes_api, "get_json", down)
    with pytest.raises(RuntimeError, match="Apple ne répond plus"):
        scan.catchup_batch(14)


def test_a_few_apple_errors_do_not_stop_the_scan(itunes, monkeypatch):
    follow(*[(f"A{i}", i) for i in range(1, 8)])
    itunes.albums = {7: [collection(70, "Ok - Single", days_ago=1)]}
    real = itunes.__call__

    def flaky(url, params):
        if params.get("entity") == "album" and params["id"] in (1, 2, 3, 4):
            raise requests.ConnectionError("rate limit")
        return real(url, params)

    monkeypatch.setattr(itunes_api, "get_json", flaky)
    assert [r.album_id for r in scan.catchup_batch(14).releases] == [70]


def test_musicbrainz_outage_falls_back_to_apple_search(itunes, mb, monkeypatch):
    follow(*[(f"A{i}", i) for i in range(1, 7)])
    itunes.albums = {i: [] for i in range(1, 7)}
    itunes.songs = {"A6": [song(600, "Titre (feat. A6)", "Autre", "Album", 60, days_ago=2)]}

    def mb_down(path, params):
        raise requests.ConnectionError("MusicBrainz down")

    monkeypatch.setattr(musicbrainz, "get", mb_down)
    calls = []
    monkeypatch.setattr(musicbrainz, "mbid_for", lambda aid, name: calls.append(aid) or mb_down("x", {}))
    rels = scan.catchup_batch(14).releases
    assert len(calls) == config.MB_MAX_FAILURES  # on arrête d'insister après 3 échecs
    assert 600 in [r.album_id for r in rels]   # la recherche Apple a pris le relais


def test_name_check_failure_does_not_block_musicbrainz(itunes, mb, monkeypatch):
    follow(("Quavo", 10))
    itunes.albums = {10: []}
    mb.artists = {"Quavo": [{"id": "mb-q", "name": "Quavo"}]}
    mb.apple_links = {"mb-q": [10]}
    ago = (clock.today() - timedelta(days=3)).isoformat()
    mb.recordings = {"mb-q": [recording("TMTC", [("Yung Miami", "mb-y"), ("Quavo", "mb-q")], ago)]}
    itunes.songs = {"Yung Miami TMTC": [song(9001, "TMTC (feat. Quavo)", "Yung Miami", "Album", 900, days_ago=3)]}

    def boom(aid, name):
        raise requests.ConnectionError("search down")

    monkeypatch.setattr(itunes_api, "name_is_unique", boom)
    assert 9001 in [r.album_id for r in scan.catchup_batch(14).releases]


def test_old_apple_version_of_a_musicbrainz_feature_is_skipped(itunes, mb):
    follow(("Quavo", 10))
    itunes.albums = {10: []}
    mb.artists = {"Quavo": [{"id": "mb-q", "name": "Quavo"}]}
    mb.apple_links = {"mb-q": [10]}
    ago = (clock.today() - timedelta(days=3)).isoformat()
    mb.recordings = {"mb-q": [recording("Vieux", [("Autre", "mb-a"), ("Quavo", "mb-q")], ago)]}
    itunes.songs = {"Autre Vieux": [song(9100, "Vieux (feat. Quavo)", "Autre", "Album", 91, days_ago=400)]}
    assert 9100 not in [r.album_id for r in scan.catchup_batch(14).releases]


# --- bilan, sauvegardes ---------------------------------------------------------------

def test_unchanged_forced_line_counts_as_known():
    config.ARTISTS_FILE.write_text("SAM | 8\n", encoding="utf-8")
    assert artists.resolve()["resolved"] == 1
    bilan = artists.resolve()
    assert (bilan["resolved"], bilan["cached"]) == (0, 1)


def test_backup_keeps_the_last_seven(monkeypatch):
    storage.save_releases([release(1, "A", state=models.STATE_ADDED)])
    folder = config.DATA_DIR / "backups"
    folder.mkdir(parents=True)
    for d in range(1, 10):
        (folder / f"radar-2026-01-0{d}.db").write_bytes(b"old")
    target = storage.backup()
    files = sorted(folder.glob("radar-*.db"))
    assert len(files) == config.BACKUPS_KEPT and target in files
    import sqlite3
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT state FROM releases WHERE album_id = 1").fetchone()[0] == "added"


# --- bot ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fast_bot(monkeypatch):
    monkeypatch.setattr(settings, "SEND_PAUSE_S", 0)
    monkeypatch.setattr(settings, "CARDS_MAX", 3)


def silent_flags(fake_bot):
    return [c.kwargs.get("disable_notification") for c in fake_bot.send_message.call_args_list]


def test_only_the_header_of_a_recap_makes_a_sound():
    b = AsyncMock()
    p = pipeline.prepare(models.Batch([release(1, "A"), release(2, "B")], [release(9, "Soon", days_ago=-3)], {}))
    run(recaps.deliver(b, "t", p, "vide", always=False))
    assert silent_flags(b) == [False, True, True, True]


def test_compact_list_also_rings_once():
    b = AsyncMock()
    many = [release(100 + i, f"T{i}", artist=f"A{i}", artist_id=10 + i) for i in range(6)]
    run(recaps.deliver(b, "t", pipeline.prepare(models.Batch(many, [], {})), "vide", always=True))
    flags = silent_flags(b)
    assert flags[0] is False and all(flags[1:])


def test_error_messages_never_contain_the_token():
    b = AsyncMock()
    run(ui.notify_error(b, "x", RuntimeError(f"https://api.telegram.org/bot{settings.TOKEN}/getUpdates")))
    assert settings.TOKEN not in b.send_message.call_args.kwargs["text"]


def scheduled_ctx(retry=False):
    jq = MagicMock()
    return SimpleNamespace(bot=AsyncMock(), job=SimpleNamespace(data={"retry": True} if retry else None), job_queue=jq)


def test_failed_scheduled_recap_is_retried_once(monkeypatch):
    async def boom():
        raise RuntimeError("Apple ne répond plus")

    ctx = scheduled_ctx()
    run(recaps.scheduled(ctx, "le récap quotidien", boom, recaps.job_daily))
    assert ctx.job_queue.run_once.call_args.kwargs == {"when": settings.RETRY_DELAY_S, "data": {"retry": True}}
    assert "Nouvel essai automatique" in ctx.bot.send_message.call_args.kwargs["text"]

    ctx = scheduled_ctx(retry=True)
    run(recaps.scheduled(ctx, "le récap quotidien", boom, recaps.job_daily))
    assert not ctx.job_queue.run_once.called
    assert "Deuxième échec" in ctx.bot.send_message.call_args.kwargs["text"]


def test_successful_recap_makes_a_backup(monkeypatch):
    done = []
    monkeypatch.setattr(storage, "backup", lambda: done.append(1))

    async def ok():
        return None

    run(recaps.scheduled(scheduled_ctx(), "x", ok, recaps.job_daily))
    assert done == [1]


def test_startup_is_one_silent_message_when_nothing_changed(monkeypatch):
    monkeypatch.setattr(artists, "resolve", lambda: {
        "resolved": 0, "cached": 185, "removed": 0, "not_found": [], "to_check": []})
    ctx = SimpleNamespace(bot=AsyncMock())
    run(recaps.job_startup(ctx))
    calls = ctx.bot.send_message.call_args_list
    assert len(calls) == 1 and calls[0].kwargs["disable_notification"] is True
    assert "185 artistes suivis" in calls[0].kwargs["text"]


def test_startup_reports_problems(monkeypatch):
    monkeypatch.setattr(artists, "resolve", lambda: {
        "resolved": 1, "cached": 184, "removed": 0, "not_found": ["Stroko"], "to_check": []})
    ctx = SimpleNamespace(bot=AsyncMock())
    run(recaps.job_startup(ctx))
    assert any("Stroko" in c.kwargs["text"] for c in ctx.bot.send_message.call_args_list)


def test_user_is_told_when_a_scan_is_already_running():
    async def scenario():
        upd = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        async with ui.scan_lock:
            await ui.reply_wait(upd)
        return upd.message.reply_text.call_args.args[0]

    assert "déjà en cours" in run(scenario())
