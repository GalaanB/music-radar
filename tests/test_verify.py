"""/verifier : artistes dont le nom est porté par plusieurs artistes chez Apple."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import collection, release
from musicradar import artists, config, storage
from musicradar.bot import artists as bot_artists
from musicradar.bot import settings
from musicradar.sources import itunes as itunes_api

CHAT = int(settings.CHAT_ID)


def follow(name, artist_id):
    conn = storage.connect()
    conn.execute("INSERT INTO artists VALUES (?, ?, ?)", (name, artist_id, name))
    conn.commit()
    conn.close()


@pytest.fixture
def apple(itunes, monkeypatch):
    monkeypatch.setattr(settings, "SEND_PAUSE_S", 0)
    config.ARTISTS_FILE.write_text("Josman\nSAM\nLuv | 33\n", encoding="utf-8")
    follow("Josman", 1)
    follow("SAM", 7)
    follow("Luv", 33)
    itunes.search = {
        "Josman": [{"artistId": 1, "artistName": "Josman"}],
        "SAM": [
            {"artistId": 7, "artistName": "SAM", "primaryGenreName": "Pop"},
            {"artistId": 8, "artistName": "Sam", "primaryGenreName": "Hip-Hop/Rap"},
        ],
        "Luv": [{"artistId": 33, "artistName": "Luv"}, {"artistId": 34, "artistName": "Luv"}],
    }
    itunes.albums = {
        7: [collection(70, "Pop Song - Single", days_ago=30, artist="SAM")],
        8: [collection(80, "Rap Song - Single", days_ago=5, artist="Sam")],
    }
    artist_info = {
        7: {"wrapperType": "artist", "artistId": 7, "artistName": "SAM", "primaryGenreName": "Pop", "artistLinkUrl": "u7"},
        8: {"wrapperType": "artist", "artistId": 8, "artistName": "Sam", "primaryGenreName": "Hip-Hop/Rap", "artistLinkUrl": "u8"},
    }
    real = itunes.__call__

    def with_artist(url, params):
        res = real(url, params)
        if params.get("entity") == "album" and params["id"] in artist_info:
            res["results"][0] = artist_info[params["id"]]
        return res

    monkeypatch.setattr(itunes_api, "get_json", with_artist)
    return itunes


def run(coro):
    return asyncio.run(coro)


def query(data):
    q = MagicMock()
    q.data = data
    q.message = SimpleNamespace(chat=SimpleNamespace(id=CHAT))
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    return SimpleNamespace(callback_query=q), q


def test_only_ambiguous_unconfirmed_artists_are_listed(apple):
    items = artists.ambiguous()
    # Josman : nom unique. Luv : ID forcé dans artists.txt, donc déjà choisi.
    assert [i["followed"] for i in items] == ["SAM"]
    sam = items[0]
    assert (sam["genre"], sam["url"]) == ("Pop", "u7") and sam["last"].startswith("Pop Song")


def test_confirming_removes_the_artist_from_the_list(apple):
    ctx = SimpleNamespace(bot=AsyncMock(), bot_data={})
    u, q = query("vf:ok:7")
    run(bot_artists.on_verify_button(u, ctx))
    assert "confirmé" in q.edit_message_text.call_args.args[0]
    assert artists.ambiguous() == []


def test_switching_to_the_right_homonym(apple):
    storage.save_releases([release(70, "Pop Song", "Single", artist="SAM", artist_id=7)])
    ctx = SimpleNamespace(bot=AsyncMock(), bot_data={})
    u, q = query("vf:ch:7")
    run(bot_artists.on_verify_button(u, ctx))
    text = q.edit_message_text.call_args.args[0]
    markup = q.edit_message_text.call_args.kwargs["reply_markup"]
    assert "Hip-Hop/Rap" in text and "Rap Song" in text
    pick = markup.inline_keyboard[0][0].callback_data
    u, q = query(pick)
    run(bot_artists.on_verify_button(u, ctx))
    assert "SAM | 8" in config.ARTISTS_FILE.read_text(encoding="utf-8").splitlines()
    assert storage.followed_name(8) == "SAM" and storage.followed_name(7) is None
    assert storage.get_release(70) is None          # sorties de l'ancien Sam : hors de /trier
    assert artists.ambiguous() == []        # le choix vaut confirmation


def test_unfollowing_from_the_card(apple):
    ctx = SimpleNamespace(bot=AsyncMock(), bot_data={})
    u, q = query("vf:rm:7")
    run(bot_artists.on_verify_button(u, ctx))
    assert artists.in_file("SAM") is None


def test_command_sends_one_card_per_artist(apple):
    ctx = SimpleNamespace(bot=AsyncMock(), bot_data={}, args=[])
    upd = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    run(bot_artists.cmd_verifier(upd, ctx))
    texts = [c.kwargs["text"] for c in ctx.bot.send_message.call_args_list]
    assert "1 artiste(s) à vérifier" in texts[0] and "SAM" in texts[1]
    markup = ctx.bot.send_message.call_args_list[1].kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["vf:ok:7", "vf:ch:7", "vf:rm:7"]


def test_added_by_picking_a_homonym_counts_as_verified(apple):
    artists.add("Dau", 55, "Dau", forced=True)
    apple.search["Dau"] = [{"artistId": 55, "artistName": "Dau"}, {"artistId": 56, "artistName": "Dau"}]
    assert "Dau" not in [i["followed"] for i in artists.ambiguous()]
