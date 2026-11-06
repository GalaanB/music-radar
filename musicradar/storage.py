"""
Base SQLite (data/radar.db) : tout le SQL du projet est ici.

Tables :
    artists        nom dans artists.txt -> ID Apple Music (+ nom trouvé)
    seen           sorties déjà notifiées (anti-doublon)
    releases       sorties notifiées, ton tri et le résultat de l'analyse
    tracks         titres des albums analysés (cache + base de comparaison)
    feature_names  le nom de l'artiste est-il porté par un seul artiste chez Apple ?
    mb_artists     identifiant MusicBrainz de chaque artiste suivi
    verified       artistes au nom ambigu confirmés via /verifier
    meta           drapeaux des migrations faites une seule fois
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import clock, config, text
from .models import KIND_FEATURING, STATE_ADDED, STATE_IGNORED, STATE_NEW, STATE_TO_LISTEN, Release

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS artists (name TEXT PRIMARY KEY, itunes_id INTEGER, matched_name TEXT)",
    "CREATE TABLE IF NOT EXISTS seen (album_id INTEGER PRIMARY KEY, notified_on TEXT)",
    "CREATE TABLE IF NOT EXISTS releases ("
    " album_id INTEGER PRIMARY KEY, artist TEXT, artist_id INTEGER,"
    " title TEXT, kind TEXT, release_date TEXT, url TEXT, cover TEXT,"
    " track_count INTEGER, total_tracks INTEGER, new_tracks INTEGER,"
    " note TEXT, state TEXT NOT NULL DEFAULT 'new', analyzed INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS tracks (album_id INTEGER, pos INTEGER, title TEXT, PRIMARY KEY (album_id, pos))",
    "CREATE TABLE IF NOT EXISTS feature_names (itunes_id INTEGER PRIMARY KEY, reliable INTEGER, checked_on TEXT)",
    "CREATE TABLE IF NOT EXISTS mb_artists (itunes_id INTEGER PRIMARY KEY, mbid TEXT, checked_on TEXT)",
    "CREATE TABLE IF NOT EXISTS verified (itunes_id INTEGER PRIMARY KEY, verified_on TEXT)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
)


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATA_DIR / "radar.db")
    for statement in _SCHEMA:
        conn.execute(statement)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(releases)")}
    if "detail" not in columns:  # base créée avant la liste des titres déjà sortis
        conn.execute("ALTER TABLE releases ADD COLUMN detail TEXT")
    return conn


def _write(sql: str, params: tuple | list = ()) -> None:
    conn = connect()
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _write_many(sql: str, rows: list[tuple]) -> None:
    if not rows:
        return
    conn = connect()
    conn.executemany(sql, rows)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Artistes suivis
# --------------------------------------------------------------------------- #

def artist_rows() -> dict[str, tuple[int | None, str | None]]:
    """{nom dans artists.txt: (ID Apple, nom trouvé chez Apple)}."""
    conn = connect()
    rows = conn.execute("SELECT name, itunes_id, matched_name FROM artists").fetchall()
    conn.close()
    return {name: (aid, matched) for name, aid, matched in rows}


def set_artist(name: str, itunes_id: int, matched_name: str) -> None:
    _write("INSERT OR REPLACE INTO artists VALUES (?, ?, ?)", (name, itunes_id, matched_name))


def delete_artists(names: list[str]) -> None:
    _write_many("DELETE FROM artists WHERE name = ?", [(n,) for n in names])


def followed_artists() -> dict[int, str]:
    """
    {ID Apple: nom trouvé chez Apple}. Deux lignes d'artists.txt peuvent pointer
    vers le même artiste (ex. « Kanye West » et « Ye ») : il n'apparaît qu'une fois.
    """
    conn = connect()
    rows = conn.execute(
        "SELECT itunes_id, matched_name FROM artists WHERE itunes_id IS NOT NULL ORDER BY name"
    ).fetchall()
    conn.close()
    names: dict[int, str] = {}
    for aid, matched in rows:
        names.setdefault(aid, matched)
    return names


def followed_name(itunes_id: int) -> str | None:
    """Nom tel qu'écrit dans artists.txt pour cet ID."""
    conn = connect()
    row = conn.execute("SELECT name FROM artists WHERE itunes_id = ? ORDER BY name", (itunes_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def unverified_artists() -> list[tuple[str, int, str]]:
    """(nom, ID Apple, nom trouvé) des artistes pas encore confirmés via /verifier."""
    conn = connect()
    rows = conn.execute(
        "SELECT a.name, a.itunes_id, a.matched_name FROM artists a"
        " LEFT JOIN verified v ON v.itunes_id = a.itunes_id"
        " WHERE a.itunes_id IS NOT NULL AND v.itunes_id IS NULL ORDER BY a.name"
    ).fetchall()
    conn.close()
    return rows


def mark_verified(itunes_id: int) -> None:
    _write("INSERT OR REPLACE INTO verified VALUES (?, ?)", (itunes_id, clock.today().isoformat()))


def forget_artist_caches(itunes_id: int) -> None:
    _write("DELETE FROM feature_names WHERE itunes_id = ?", (itunes_id,))


# --------------------------------------------------------------------------- #
# Caches : nom unique chez Apple, identifiant MusicBrainz
# --------------------------------------------------------------------------- #

def _fresh(checked_on: str) -> bool:
    return date.fromisoformat(checked_on) > clock.today() - timedelta(days=config.NAME_RECHECK_DAYS)


def cached_name_reliability(itunes_id: int) -> bool | None:
    """True/False si vérifié récemment, None s'il faut (re)vérifier."""
    conn = connect()
    row = conn.execute("SELECT reliable, checked_on FROM feature_names WHERE itunes_id = ?", (itunes_id,)).fetchone()
    conn.close()
    return bool(row[0]) if row and _fresh(row[1]) else None


def save_name_reliability(itunes_id: int, reliable: bool) -> None:
    _write(
        "INSERT OR REPLACE INTO feature_names VALUES (?, ?, ?)",
        (itunes_id, int(reliable), clock.today().isoformat()),
    )


def cached_mbid(itunes_id: int) -> tuple[bool, str | None]:
    """(trouvé en cache ?, MBID). Un MBID trouvé est gardé ; un échec est retenté après 30 jours."""
    conn = connect()
    row = conn.execute("SELECT mbid, checked_on FROM mb_artists WHERE itunes_id = ?", (itunes_id,)).fetchone()
    conn.close()
    if row and (row[0] or _fresh(row[1])):
        return True, row[0]
    return False, None


def save_mbid(itunes_id: int, mbid: str | None) -> None:
    _write("INSERT OR REPLACE INTO mb_artists VALUES (?, ?, ?)", (itunes_id, mbid, clock.today().isoformat()))


# --------------------------------------------------------------------------- #
# Titres des albums analysés
# --------------------------------------------------------------------------- #

def cached_tracks(album_id: int) -> list[str]:
    conn = connect()
    rows = conn.execute("SELECT title FROM tracks WHERE album_id = ? ORDER BY pos", (album_id,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def save_tracks(album_id: int, titles: list[str]) -> None:
    _write_many("INSERT OR REPLACE INTO tracks VALUES (?, ?, ?)", [(album_id, i, t) for i, t in enumerate(titles)])


def tracks_of_artist_before(artist_id: int, album_id: int, before: date) -> list[str]:
    """Titres de tout autre album déjà analysé du même artiste, sorti avant."""
    conn = connect()
    rows = conn.execute(
        "SELECT t.title FROM tracks t JOIN releases r ON r.album_id = t.album_id"
        " WHERE r.artist_id = ? AND t.album_id != ? AND r.release_date < ?",
        (artist_id, album_id, before.isoformat()),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def tracks_with_album(artist_id: int, excluding: int) -> list[tuple[str, str]]:
    """(titre, album) des albums déjà analysés de l'artiste, du plus ancien au plus récent."""
    conn = connect()
    rows = conn.execute(
        "SELECT t.title, rl.title FROM tracks t JOIN releases rl ON rl.album_id = t.album_id"
        " WHERE rl.artist_id = ? AND t.album_id != ? ORDER BY rl.release_date",
        (artist_id, excluding),
    ).fetchall()
    conn.close()
    return rows


# --------------------------------------------------------------------------- #
# Sorties : enregistrement, tri, file /trier
# --------------------------------------------------------------------------- #

_COLS = (
    "album_id, artist, artist_id, title, kind, release_date, url, cover,"
    " track_count, total_tracks, new_tracks, note, state, analyzed, detail"
)


def _row_to_release(row: tuple) -> Release:
    (album_id, artist, artist_id, title, kind, release_date, url, cover,
     track_count, total_tracks, new_tracks, note, state, analyzed, detail) = row
    return Release(
        album_id=album_id, artist=artist or "?", artist_id=artist_id or 0, title=title or "?",
        kind=kind or "Album", release_date=date.fromisoformat(release_date), url=url or "",
        cover=cover or "", track_count=track_count or 0, total_tracks=total_tracks,
        new_tracks=new_tracks, note=note or "", state=state or STATE_NEW, analyzed=bool(analyzed),
        detail=detail or "",
    )


def load_releases(ids: list[int]) -> dict[int, Release]:
    out: dict[int, Release] = {}
    conn = connect()
    for i in range(0, len(ids), 400):  # limite SQLite de 999 variables
        chunk = ids[i : i + 400]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT {_COLS} FROM releases WHERE album_id IN ({marks})", chunk):
            out[row[0]] = _row_to_release(row)
    conn.close()
    return out


def get_release(album_id: int) -> Release | None:
    return load_releases([album_id]).get(album_id)


def all_releases() -> list[Release]:
    conn = connect()
    rows = conn.execute(f"SELECT {_COLS} FROM releases").fetchall()
    conn.close()
    return [_row_to_release(r) for r in rows]


def save_releases(releases: list[Release]) -> None:
    """Enregistre/actualise les sorties. L'état de tri existant n'est jamais écrasé."""
    _write_many(
        "INSERT INTO releases (" + _COLS + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(album_id) DO UPDATE SET"
        " artist=excluded.artist, artist_id=excluded.artist_id, title=excluded.title,"
        " kind=excluded.kind, release_date=excluded.release_date, url=excluded.url,"
        " cover=excluded.cover, track_count=excluded.track_count,"
        " total_tracks=excluded.total_tracks, new_tracks=excluded.new_tracks,"
        " note=excluded.note, analyzed=excluded.analyzed, detail=excluded.detail",
        [
            (r.album_id, r.artist, r.artist_id, r.title, r.kind, r.release_date.isoformat(), r.url,
             r.cover, r.track_count, r.total_tracks, r.new_tracks, r.note, r.state, int(r.analyzed), r.detail)
            for r in releases
        ],
    )


def delete_releases(ids: list[int]) -> None:
    _write_many("DELETE FROM releases WHERE album_id = ?", [(i,) for i in ids])


def set_state(ids: list[int], state: str) -> None:
    _write_many("UPDATE releases SET state = ? WHERE album_id = ?", [(state, i) for i in ids])


def drop_pending_of(artist_id: int) -> None:
    """Sorties non triées d'un artiste qu'on ne suit plus (ou plus le même) : hors de /trier."""
    _write("DELETE FROM releases WHERE artist_id = ? AND state = ?", (artist_id, STATE_NEW))


def drop_pending_features(artist_ids: list[int]) -> None:
    """Retire de la file les featurings non triés de ces artistes."""
    _write_many(
        "DELETE FROM releases WHERE kind = ? AND artist_id = ? AND state = ?",
        [(KIND_FEATURING, aid, STATE_NEW) for aid in artist_ids],
    )


def drop_feature_twins() -> int:
    """
    Retire de la file les featurings non triés qui doublonnent un titre déjà
    connu en single ou sur un projet crédité au même artiste. Retourne leur nombre.
    """
    releases = all_releases()
    known = text.title_index((r.title, r.artist) for r in releases if r.kind != KIND_FEATURING)
    twins = [
        r.album_id
        for r in releases
        if r.kind == KIND_FEATURING and r.state == STATE_NEW and text.already_out(known, r.artist, r.title)
    ]
    delete_releases(twins)
    return len(twins)


def seen_ids() -> set[int]:
    conn = connect()
    ids = {r[0] for r in conn.execute("SELECT album_id FROM seen")}
    conn.close()
    return ids


def mark_seen(releases: list[Release]) -> None:
    _write_many(
        "INSERT OR IGNORE INTO seen VALUES (?, ?)",
        [(r.album_id, clock.today().isoformat()) for r in releases],
    )


def count_state(state: str) -> int:
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM releases WHERE state = ?", (state,)).fetchone()[0]
    conn.close()
    return n


def count_pending(excluding: list[int]) -> int:
    """Sorties encore à trier, hors celles qu'on vient d'envoyer."""
    conn = connect()
    pending = {r[0] for r in conn.execute("SELECT album_id FROM releases WHERE state = ?", (STATE_NEW,))}
    conn.close()
    return len(pending - set(excluding))


def next_in_queue(state: str, after_id: int = 0) -> tuple[Release | None, int]:
    """
    Prochaine sortie de la file `state`, plus ancienne que `after_id` (0 = depuis
    le début), triée de la plus récente à la plus ancienne. Retourne aussi le
    nombre total de sorties dans cet état. L'ordre ne dépend d'aucune position
    mémorisée : « Plus tard » avance sans jamais boucler.
    """
    conn = connect()
    total = conn.execute("SELECT COUNT(*) FROM releases WHERE state = ?", (state,)).fetchone()[0]
    ref = None
    if after_id:
        ref = conn.execute("SELECT release_date FROM releases WHERE album_id = ?", (after_id,)).fetchone()
    if ref:
        row = conn.execute(
            f"SELECT {_COLS} FROM releases WHERE state = ?"
            " AND (release_date < ? OR (release_date = ? AND album_id < ?))"
            " ORDER BY release_date DESC, album_id DESC LIMIT 1",
            (state, ref[0], ref[0], after_id),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {_COLS} FROM releases WHERE state = ? ORDER BY release_date DESC, album_id DESC LIMIT 1",
            (state,),
        ).fetchone()
    conn.close()
    return (_row_to_release(row) if row else None), total


# --------------------------------------------------------------------------- #
# Migrations et sauvegardes
# --------------------------------------------------------------------------- #

_OLD_NOTE_RE = re.compile(r"🆕 (\d+) nouveaux? / (\d+) · (\d+) déjà sortis?")


def _s(n: str) -> str:
    return "s" if int(n) > 1 else ""


def migrate() -> int:
    """
    Remise à niveau de la base après une mise à jour (sans appel réseau) :
    anciens libellés réécrits, ancien « À écouter » converti en « Ajoutée »,
    et une seule fois : les versions alternatives rangées automatiquement par
    une ancienne version sont remises dans /trier. Retourne leur nombre.
    """
    conn = connect()
    restored = 0
    if not conn.execute("SELECT 1 FROM meta WHERE key = 'variants_restored'").fetchone():
        cur = conn.execute(
            "UPDATE releases SET state = ?, analyzed = 0 WHERE state = ? AND note LIKE '🔁 version alternative%'",
            (STATE_NEW, STATE_IGNORED),
        )
        restored = cur.rowcount
        conn.execute("INSERT INTO meta VALUES ('variants_restored', '1')")
    if not conn.execute("SELECT 1 FROM meta WHERE key = 'details_backfill'").fetchone():
        # Albums non triés analysés avant la liste des titres déjà sortis : à
        # réanalyser (la liste des titres est en cache, sans appel réseau).
        conn.execute(
            "UPDATE releases SET analyzed = 0 WHERE state = ? AND new_tracks IS NOT NULL"
            " AND (detail IS NULL OR detail = '')",
            (STATE_NEW,),
        )
        conn.execute("INSERT INTO meta VALUES ('details_backfill', '1')")
    conn.execute("UPDATE releases SET state = ? WHERE state = ?", (STATE_ADDED, STATE_TO_LISTEN))
    for album_id, note in conn.execute("SELECT album_id, note FROM releases WHERE state = ?", (STATE_NEW,)).fetchall():
        new_note = _OLD_NOTE_RE.sub(
            lambda m: f"🆕 {m[1]} inédit{_s(m[1])} sur {m[2]} · {m[3]} déjà sorti{_s(m[3])}", note or ""
        )
        new_note = new_note.replace("♻️ réédition · rien de nouveau", "♻️ réédition · aucun inédit")
        new_note = re.sub(
            r"♻️ réédition · (\d+) nouveaux? / (\d+)",
            lambda m: f"♻️ réédition · {m[1]} inédit{_s(m[1])} sur {m[2]}",
            new_note,
        )
        if new_note != (note or ""):
            conn.execute("UPDATE releases SET note = ? WHERE album_id = ?", (new_note, album_id))
    # Albums/EP non triés restés sans verdict : on les réanalysera au prochain scan.
    conn.execute(
        "UPDATE releases SET analyzed = 0 WHERE state = ? AND kind != 'Single' AND (note IS NULL OR note = '')",
        (STATE_NEW,),
    )
    conn.commit()
    conn.close()
    drop_feature_twins()
    return restored


def backup() -> Path:
    """
    Copie cohérente de la base (API de sauvegarde SQLite) dans data/backups/,
    une par jour, les 7 dernières gardées. Protège ton tri d'une fausse manip
    ou d'un bug ; pas d'une panne de disque (même disque que la base).
    """
    folder = config.DATA_DIR / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"radar-{clock.today().isoformat()}.db"
    src = connect()
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    for old in sorted(folder.glob("radar-*.db"))[: -config.BACKUPS_KEPT]:
        old.unlink()
    return target
