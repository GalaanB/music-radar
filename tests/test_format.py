"""Mise en forme des messages Telegram."""

import radar
from conftest import release


def test_card_is_two_lines_with_note():
    r = release(1, "Big", total_tracks=5, note="🆕 3 inédits sur 5")
    text = radar.card_text(r)
    assert text.count("\n") == 1 and "Album · 5 titres" in text and "🆕 3 inédits sur 5" in text


def test_html_is_escaped():
    assert "&lt;b&gt;" in radar.card_text(release(1, "<b>x"))


def test_compact_list_tags():
    msgs = radar.format_messages("T", [
        release(1, "Big", new_tracks=3, total_tracks=5, note="🆕"),
        release(2, "Big (Deluxe)", new_tracks=0, total_tracks=5, note="♻️ réédition · aucun inédit"),
        release(3, "Song (Remix)", "Single"),
    ], "vide")
    assert "🆕 3/5" in msgs[0] and "♻️ rien de neuf" in msgs[0] and "🔁 remix" in msgs[0]


def test_long_lists_are_split_under_telegram_limit():
    big = [release(2000 + i, "T" * 40, days_ago=i % 5, artist=f"Artiste {i}") for i in range(200)]
    parts = radar.format_messages("Gros", big, "")
    assert len(parts) > 1 and all(len(m) < 4096 for m in parts)


def test_upcoming_is_capped():
    up = [release(900 + i, f"Soon {i}", days_ago=-3) for i in range(12)]
    text = radar.format_upcoming(up)[0]
    assert text.count("•") == radar.UPCOMING_MAX_LINES and "4 autre" in text
