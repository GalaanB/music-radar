"""Mise en forme des messages Telegram."""


from conftest import release
from musicradar import config, formatting


def test_card_is_two_lines_with_note():
    r = release(1, "Big", total_tracks=5, note="🆕 3 inédits sur 5")
    text = formatting.card_text(r)
    assert text.count("\n") == 1 and "Album · 5 titres" in text and "🆕 3 inédits sur 5" in text


def test_html_is_escaped():
    assert "&lt;b&gt;" in formatting.card_text(release(1, "<b>x"))


def test_compact_list_tags():
    msgs = formatting.compact_list("T", [
        release(1, "Big", new_tracks=3, total_tracks=5, note="🆕"),
        release(2, "Big (Deluxe)", new_tracks=0, total_tracks=5, note="♻️ réédition · aucun inédit"),
        release(3, "Song (Remix)", "Single"),
    ], "vide")
    assert "🆕 3/5" in msgs[0] and "♻️ rien de neuf" in msgs[0] and "🔁 remix" in msgs[0]


def test_long_lists_are_split_under_telegram_limit():
    big = [release(2000 + i, "T" * 40, days_ago=i % 5, artist=f"Artiste {i}") for i in range(200)]
    parts = formatting.compact_list("Gros", big, "")
    assert len(parts) > 1 and all(len(m) < 4096 for m in parts)


def test_upcoming_is_capped():
    up = [release(900 + i, f"Soon {i}", days_ago=-3) for i in range(12)]
    text = formatting.upcoming(up)[0]
    assert text.count("•") == config.UPCOMING_MAX_LINES and "4 autre" in text


def test_card_shows_the_tracks_already_out_on_a_third_line():
    r = release(1, "Album", total_tracks=5, new_tracks=3, note="🆕 3 inédits sur 5 · 2 déjà sortis en single",
                detail="Déjà sortis : Hit One · Hit <Two>")
    lines = formatting.card_text(r).split("\n")
    assert len(lines) == 3 and lines[2] == "↳ Déjà sortis : Hit One · Hit &lt;Two&gt;"
