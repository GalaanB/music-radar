"""Récap du vendredi : nouvelles sorties seulement, rappel de /trier, sorties à venir."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from conftest import collection, release
from musicradar import models, pipeline, scan, storage
from musicradar.bot import recaps, settings


@pytest.fixture(autouse=True)
def fast_bot(monkeypatch):
    monkeypatch.setattr(settings, "SEND_PAUSE_S", 0)


def follow(name, artist_id):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES (?, ?, ?)", (name, artist_id, name))
    conn.commit()
    conn.close()


def test_friday_only_sends_what_was_not_notified_yet_plus_upcoming(itunes):
    follow("Josman", 1)
    itunes.albums = {1: [collection(10, "Lundi - Single", days_ago=4, artist="Josman")]}
    pipeline.finalize(pipeline.prepare(scan.daily_batch()))  # reçue lundi
    itunes.albums[1] += [
        collection(11, "Vendredi - Single", days_ago=0, artist="Josman"),
        collection(12, "Bientôt", days_ago=-3, artist="Josman"),
    ]
    batch = scan.weekly_batch()
    assert [r.album_id for r in batch.releases] == [11]
    assert [r.album_id for r in batch.upcoming] == [12]


def sent_texts(fake_bot):
    return [c.kwargs["text"] for c in fake_bot.send_message.call_args_list]


def test_reminder_counts_older_pending_releases_only():
    storage.save_releases([release(1, "Ancienne A", days_ago=5), release(2, "Ancienne B", days_ago=4)])
    p = pipeline.prepare(models.Batch([release(3, "Nouvelle")], [], {}))
    b = AsyncMock()
    asyncio.run(recaps.deliver(b, "📆 Récap du vendredi", p, "vide", always=True, reminder=True))
    texts = sent_texts(b)
    assert any("2 sortie(s) reçue(s) plus tôt" in t for t in texts)


def test_no_reminder_when_nothing_else_is_waiting():
    p = pipeline.prepare(models.Batch([release(3, "Nouvelle")], [], {}))
    b = AsyncMock()
    asyncio.run(recaps.deliver(b, "📆", p, "vide", always=True, reminder=True))
    assert not any("plus tôt" in t for t in sent_texts(b))


def test_empty_friday_still_sends_reminder_and_upcoming():
    storage.save_releases([release(1, "Ancienne", days_ago=5)])
    p = pipeline.prepare(models.Batch([], [release(9, "Bientôt", days_ago=-2)], {}))
    b = AsyncMock()
    asyncio.run(recaps.deliver(b, "📆", p, "Rien de nouveau depuis le dernier récap.", always=True, reminder=True))
    texts = sent_texts(b)
    assert "Rien de nouveau" in texts[0]
    assert any("1 sortie(s) reçue(s) plus tôt" in t for t in texts)
    assert any("À venir" in t for t in texts)


def test_daily_has_no_reminder():
    storage.save_releases([release(1, "Ancienne", days_ago=5)])
    p = pipeline.prepare(models.Batch([release(3, "Nouvelle")], [], {}))
    b = AsyncMock()
    asyncio.run(recaps.deliver(b, "🎧", p, "vide", always=False))
    assert not any("plus tôt" in t for t in sent_texts(b))
