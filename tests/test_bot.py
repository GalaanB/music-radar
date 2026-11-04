"""Bot Telegram : envoi des récaps, boutons, file /trier, commandes, erreurs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import Application, filters

import bot
import radar
from conftest import release

CHAT = int(bot.CHAT_ID)


@pytest.fixture(autouse=True)
def fast_bot(monkeypatch):
    monkeypatch.setattr(bot, "SEND_PAUSE_S", 0)
    monkeypatch.setattr(bot, "CARDS_MAX", 3)


def run(coro):
    return asyncio.run(coro)


def sent(fake_bot):
    return [
        (c.kwargs["text"], c.kwargs.get("reply_markup"), c.kwargs.get("link_preview_options"))
        for c in fake_bot.send_message.call_args_list
    ]


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def query(data, chat=CHAT):
    q = MagicMock()
    q.data = data
    q.message = SimpleNamespace(chat=SimpleNamespace(id=chat))
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    return SimpleNamespace(callback_query=q), q


def ctx(**kw):
    return SimpleNamespace(bot=AsyncMock(), bot_data={}, args=kw.get("args", []))


def message_update():
    return SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))


# --- récaps ------------------------------------------------------------------

def test_few_releases_are_sent_as_cards_with_buttons():
    b = AsyncMock()
    p = radar.prepare(radar.Batch(
        [release(1, "A"), release(2, "B", days_ago=1), release(3, "C", "Single", 2)],
        [release(9, "Bientôt", days_ago=-4)], {},
    ))
    run(bot.deliver(b, "🎧 Sorties du jour", p, "vide", always=False))
    msgs = sent(b)
    assert len(msgs) == 5  # en-tête + 3 cartes + à venir
    assert callbacks(msgs[1][1]) == ["r:l:1", "r:i:1"]
    assert msgs[1][2].url.startswith("https://img/1/") and msgs[1][2].prefer_small_media
    assert "À venir" in msgs[4][0]
    assert all(len(c.encode()) <= 64 for _, m, _ in msgs if m for c in callbacks(m))


def test_many_releases_become_a_compact_list_pointing_to_trier():
    b = AsyncMock()
    many = [release(100 + i, f"T{i}", artist=f"Art{i}", artist_id=10 + i) for i in range(6)]
    run(bot.deliver(b, "⏪ Rattrapage", radar.prepare(radar.Batch(many, [], {})), "vide", always=True))
    msgs = sent(b)
    assert all(m[1] is None for m in msgs)
    assert "/trier" in msgs[-1][0] and "6 sorties" in msgs[-1][0]


def test_empty_daily_is_silent_but_empty_weekly_says_so():
    b = AsyncMock()
    empty = radar.prepare(radar.Batch([], [], {}))
    run(bot.deliver(b, "🎧", empty, "vide", always=False))
    assert not b.send_message.called
    run(bot.deliver(b, "📆", empty, "Rien cette semaine.", always=True))
    assert "Rien cette semaine." in sent(b)[0][0]


def test_releases_are_marked_notified_only_after_a_successful_send():
    ok = AsyncMock()
    run(bot._run_recap(ok, lambda: radar.Batch([release(700, "Neuf")], [], {}), "t", "vide", False))
    failing = AsyncMock()
    failing.send_message.side_effect = RuntimeError("telegram down")
    with pytest.raises(RuntimeError):
        run(bot._run_recap(failing, lambda: radar.Batch([release(701, "Neuf2")], [], {}), "t", "vide", False))
    conn = radar._db()
    seen = {r[0] for r in conn.execute("SELECT album_id FROM seen")}
    conn.close()
    assert 700 in seen and 701 not in seen


# --- boutons -----------------------------------------------------------------

def test_card_buttons_added_undo_ignore():
    radar.save_releases([release(1, "A"), release(2, "B")])
    c = ctx()
    u, q = query("r:l:1")
    run(bot.on_card_button(u, c))
    assert radar.get_release(1).state == radar.STATE_ADDED
    call = q.edit_message_text.call_args
    assert "Ajoutée" in call.args[0] and callbacks(call.kwargs["reply_markup"]) == ["r:u:1"]

    u, q = query("r:u:1")
    run(bot.on_card_button(u, c))
    assert radar.get_release(1).state == radar.STATE_NEW

    u, q = query("r:i:2")
    run(bot.on_card_button(u, c))
    assert radar.get_release(2).state == radar.STATE_IGNORED


def test_buttons_from_another_chat_or_garbage_do_nothing():
    radar.save_releases([release(3, "C")])
    u, q = query("r:l:3", chat=999)
    run(bot.on_card_button(u, ctx()))
    assert radar.get_release(3).state == radar.STATE_NEW and not q.edit_message_text.called
    u, q = query("r:zzz:abc")
    run(bot.on_card_button(u, ctx()))
    assert not q.edit_message_text.called


def test_trier_walks_the_whole_queue():
    radar.save_releases([release(i, f"T{i}", days_ago=i % 4) for i in range(1, 8)])
    upd = message_update()
    run(bot.cmd_trier(upd, ctx()))
    first = upd.message.reply_text.call_args
    assert "à trier" in first.args[0]
    ids = [b.text for row in first.kwargs["reply_markup"].inline_keyboard for b in row]
    assert ids == ["➕ Ajoutée", "🙈 Ignorer", "⏭ Plus tard"]

    cur = int(callbacks(first.kwargs["reply_markup"])[0].split(":")[3])
    visited, final = [cur], ""
    for step in range(20):
        action = "LIS"[step % 3]
        u, q = query(f"q:n:{action}:{cur}")
        run(bot.on_queue_button(u, ctx()))
        call = q.edit_message_text.call_args
        if call.kwargs["reply_markup"] is None:
            final = call.args[0]
            break
        cur = int(callbacks(call.kwargs["reply_markup"])[0].split(":")[3])
        visited.append(cur)
    assert len(visited) == len(set(visited)) == 7
    assert "File terminée" in final and "Il en reste" in final
    assert radar.count_state(radar.STATE_ADDED) >= 2 and radar.count_state(radar.STATE_IGNORED) >= 2


def test_old_aecouter_buttons_are_ignored():
    u, q = query("q:l:D:1")
    run(bot.on_queue_button(u, ctx()))
    assert not q.edit_message_text.called
    assert "aecouter" not in bot.COMMANDS


def test_button_answer_refused_by_telegram_is_harmless():
    q = MagicMock()
    q.answer = AsyncMock(side_effect=BadRequest("Query is too old and response timeout expired"))
    run(bot._answer(q))


# --- gestion des artistes -----------------------------------------------------

def test_ajouter_then_undo(itunes):
    itunes.search = {"Lomi": [{"artistId": 11, "artistName": "Lomi", "primaryGenreName": "Hip-Hop/Rap", "artistLinkUrl": "u"}]}
    c = ctx(args=["Lomi"])
    run(bot.cmd_ajouter(message_update(), c))
    text, markup, _ = sent(c.bot)[-1]
    assert radar.artist_in_file("Lomi") == "Lomi" and "ajouté" in text
    token = callbacks(markup)[0].split(":")[1]
    u, _ = query(f"ad:{token}:u")
    run(bot.on_add_button(u, c))
    assert radar.artist_in_file("Lomi") is None


def test_ajouter_homonyms_then_pick_one(itunes):
    itunes.search = {"Muse": [
        {"artistId": 21, "artistName": "Muse", "primaryGenreName": "Alternative"},
        {"artistId": 22, "artistName": "Muse", "primaryGenreName": "Hip-Hop/Rap", "artistLinkUrl": "u"},
    ]}
    c = ctx(args=["Muse"])
    run(bot.cmd_ajouter(message_update(), c))
    text, markup, _ = sent(c.bot)[-1]
    assert "Plusieurs résultats" in text and len(callbacks(markup)) == 3
    token = callbacks(markup)[0].split(":")[1]
    u, _ = query(f"ad:{token}:1")
    run(bot.on_add_button(u, c))
    assert "Muse | 22" in radar.ARTISTS_FILE.read_text(encoding="utf-8").splitlines()


def test_expired_add_button_shows_an_alert():
    u, q = query("ad:zzzzzz:0")
    run(bot.on_add_button(u, ctx()))
    assert q.answer.call_args.kwargs.get("show_alert") is True


def test_retirer_and_artistes():
    c = ctx(args=["drake"])
    run(bot.cmd_retirer(message_update(), c))
    assert "Retiré" in sent(c.bot)[-1][0] and radar.artist_in_file("Drake") is None
    c = ctx(args=["lil"])
    run(bot.cmd_retirer(message_update(), c))
    assert "Lil Baby" in sent(c.bot)[-1][0]
    c = ctx(args=["tyler"])
    run(bot.cmd_artistes(message_update(), c))
    assert "1 artiste" in sent(c.bot)[-1][0]


# --- commandes collées avec une mise en forme ---------------------------------

def tg_update(text, entity_type):
    return Update.de_json({"update_id": 1, "message": {
        "message_id": 1, "date": 0, "chat": {"id": CHAT, "type": "private"},
        "from": {"id": CHAT, "is_bot": False, "first_name": "G"}, "text": text,
        "entities": [{"type": entity_type, "offset": 0, "length": len(text)}],
    }}, None)


def test_formatted_command_is_caught_once():
    fallback = filters.TEXT & filters.Chat(chat_id=CHAT) & ~filters.COMMAND & filters.Regex(r"^\s*/\w")
    assert fallback.check_update(tg_update("/retirer SAM", "code"))
    assert not fallback.check_update(tg_update("/retirer SAM", "bot_command"))
    assert not fallback.check_update(tg_update("salut", "bold"))


def test_plain_command_dispatch(monkeypatch):
    seen = {}

    async def fake(update, context):
        seen["args"] = context.args

    monkeypatch.setitem(bot.COMMANDS, "retirer", fake)
    upd = SimpleNamespace(message=SimpleNamespace(text="/retirer SAM", reply_text=AsyncMock()))
    run(bot.on_plain_command(upd, SimpleNamespace(args=None)))
    assert seen["args"] == ["SAM"]
    upd = SimpleNamespace(message=SimpleNamespace(text="/nimp", reply_text=AsyncMock()))
    run(bot.on_plain_command(upd, SimpleNamespace(args=None)))
    assert "inconnue" in upd.message.reply_text.call_args.args[0]


# --- erreurs -----------------------------------------------------------------

def test_unexpected_error_is_reported_with_token_masked():
    try:
        raise RuntimeError(f"boom avec {bot.TOKEN} dedans")
    except RuntimeError as exc:
        err = exc
    c = SimpleNamespace(error=err, bot=AsyncMock())
    run(bot._on_error(None, c))
    text = c.bot.send_message.call_args.kwargs["text"]
    assert "Erreur imprévue" in text and bot.TOKEN not in text


@pytest.mark.parametrize("err", [NetworkError("httpx.ReadError: "), TimedOut()])
def test_network_hiccups_are_not_reported(err):
    c = SimpleNamespace(error=err, bot=AsyncMock())
    run(bot._on_error(None, c))
    assert not c.bot.send_message.called


def test_application_builds():
    with patch.object(Application, "run_polling") as polling:
        bot.main()
    assert polling.called
