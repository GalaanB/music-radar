"""
radar.py
========

Logique du "Music Radar" : détecte les nouvelles sorties d'une liste
d'artistes via l'API iTunes/Apple Music (publique, sans clé), analyse ce
qu'elles apportent de nouveau, mémorise ton tri (ajoutée / ignorée) et formate
les messages Telegram.

Aucune dépendance Telegram ici : ce module est testable seul.
    python radar.py resolve        # résout artists.txt -> IDs Apple Music
    python radar.py daily          # affiche le récap quotidien (sans écrire en base)
    python radar.py weekly         # affiche le récap hebdo
    python radar.py catchup 30     # affiche les sorties des 30 derniers jours
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("radar")

DATA_DIR = Path(os.getenv("RADAR_DATA_DIR", "./data"))
ARTISTS_FILE = Path(os.getenv("ARTISTS_FILE", "./artists.txt"))
COUNTRY = os.getenv("ITUNES_COUNTRY", "fr")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Paris"))

ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"

# L'API iTunes tolère environ 20 requêtes/minute (limite non officielle).
# 3,2 s entre deux appels reste sous ce seuil.
REQUEST_DELAY_S = float(os.getenv("ITUNES_DELAY", "3.2"))
HTTP_TIMEOUT_S = 20
ALBUMS_PER_ARTIST = 25  # on trie nous-mêmes par date : on prend de la marge

# Analyse de nouveauté (1 appel iTunes de plus par album/EP à analyser).
# NOVELTY=0 la désactive si jamais elle se comportait mal.
NOVELTY_ENABLED = os.getenv("NOVELTY", "1") == "1"
# Sorties à venir annoncées dans le récap du vendredi.
UPCOMING_DAYS = int(os.getenv("UPCOMING_DAYS", "7"))
UPCOMING_MAX_LINES = int(os.getenv("UPCOMING_MAX", "8"))

TELEGRAM_MAX_LEN = 4000  # limite Telegram = 4096, petite marge de sécurité

# États possibles d'une sortie dans ton tri.
STATE_NEW = "new"              # notifiée, pas encore triée
STATE_ADDED = "added"          # « Ajoutée » : rangée dans ta playlist Apple Music
STATE_TO_LISTEN = "to_listen"  # ancien bouton « À écouter » (converti en "added")
STATE_LISTENED = "listened"    # écoutée
STATE_IGNORED = "ignored"      # ignorée


# --------------------------------------------------------------------------- #
# Modèle
# --------------------------------------------------------------------------- #

@dataclass
class Release:
    album_id: int
    artist: str
    title: str
    kind: str          # "Album", "EP" ou "Single"
    release_date: date
    url: str
    artist_id: int = 0
    cover: str = ""
    track_count: int = 0
    # Résultat de l'analyse de nouveauté (None = pas de comparaison possible).
    total_tracks: int | None = None
    new_tracks: int | None = None
    note: str = ""
    state: str = STATE_NEW
    analyzed: bool = False
    explicit: bool = False  # version explicite (préférée à la version "clean")

    @property
    def dup_key(self) -> tuple[str, str, str]:
        """Même artiste + même type + même titre = même sortie, quel que soit l'ID."""
        return (_cmp_key(self.artist), self.kind, _norm_title(self.title))

    @property
    def is_edition(self) -> bool:
        return _split_edition(self.title)[1]

    @property
    def nothing_new(self) -> bool:
        """Réédition dont tous les titres existent déjà dans l'album d'origine."""
        return self.new_tracks == 0 and self.note.startswith("♻️")

    @property
    def is_variant(self) -> bool:
        """Version slowed, sped up, instrumentale, remix… d'une autre sortie."""
        return _is_variant(self.title)


@dataclass
class Batch:
    """Résultat brut d'un scan."""
    releases: list[Release]                  # sorties datées d'aujourd'hui ou avant
    upcoming: list[Release]                  # sorties datées dans le futur proche
    catalog: dict[int, list[Release]] = field(default_factory=dict)  # par artiste


@dataclass
class Prepared:
    """Batch analysé et trié, prêt à être envoyé."""
    to_show: list[Release]       # à afficher (pas encore triées, avec du neuf)
    nothing_new: list[Release]   # rééditions sans nouveauté (signalées en une ligne)
    already_sorted: int          # déjà triées par toi : on ne les remontre pas
    upcoming: list[Release]
    everything: list[Release]    # toutes, pour les marquer comme notifiées


# --------------------------------------------------------------------------- #
# Stockage local (SQLite)
# --------------------------------------------------------------------------- #

def _db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "radar.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artists ("
        " name TEXT PRIMARY KEY, itunes_id INTEGER, matched_name TEXT)"
    )
    # Anti-doublon des notifications (inchangé depuis la v1).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen ("
        " album_id INTEGER PRIMARY KEY, notified_on TEXT)"
    )
    # Sorties notifiées + ton tri + résultat de l'analyse de nouveauté.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS releases ("
        " album_id INTEGER PRIMARY KEY, artist TEXT, artist_id INTEGER,"
        " title TEXT, kind TEXT, release_date TEXT, url TEXT, cover TEXT,"
        " track_count INTEGER, total_tracks INTEGER, new_tracks INTEGER,"
        " note TEXT, state TEXT NOT NULL DEFAULT 'new', analyzed INTEGER NOT NULL DEFAULT 0)"
    )
    # Titres des albums déjà analysés (sert de cache et de base de comparaison).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tracks ("
        " album_id INTEGER, pos INTEGER, title TEXT, PRIMARY KEY (album_id, pos))"
    )
    return conn


# --------------------------------------------------------------------------- #
# artists.txt
# --------------------------------------------------------------------------- #

# artists.txt est monté dans le conteneur en fichier unique : on ne le remplace
# jamais (pas de fichier temporaire + rename, ça casserait le montage), on le
# réécrit sur place. Le verrou évite qu'un scan lise un fichier à moitié écrit.
_artists_lock = threading.Lock()


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _sort_key(line: str) -> str:
    """Ordre alphabétique insensible à la casse et aux accents (comme le fichier)."""
    return _strip_accents(line.partition("|")[0].strip()).casefold()


def _cmp_key(name: str) -> str:
    """Clé de comparaison de noms : 'Tyler The Creator' == 'Tyler, The Creator'."""
    plain = _strip_accents(name).casefold()
    alnum = re.sub(r"[^0-9a-z]+", "", plain)
    return alnum or plain.strip()


def _is_artist_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _read_lines() -> list[str]:
    if not ARTISTS_FILE.exists():
        raise FileNotFoundError(f"Fichier introuvable : {ARTISTS_FILE}")
    return ARTISTS_FILE.read_text(encoding="utf-8").splitlines()


def _write_lines(lines: list[str]) -> None:
    # write_text ouvre en écriture et tronque : même fichier, même inode.
    ARTISTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_artist_names() -> list[str]:
    """
    Lit artists.txt : un artiste par ligne. Les lignes vides et celles qui
    commencent par # sont ignorées. Pour forcer un artiste précis en cas
    d'homonymes, écris `Nom | 123456789` (ID Apple Music de l'artiste, visible
    à la fin de son URL music.apple.com/.../artist/nom/123456789).
    """
    with _artists_lock:
        lines = _read_lines()
    return [ln.strip() for ln in lines if _is_artist_line(ln)]


def list_artist_names() -> list[str]:
    """Noms seuls (sans les ` | id`), dans l'ordre du fichier."""
    return [ln.partition("|")[0].strip() for ln in read_artist_names()]


def artists_matching(needle: str = "") -> list[str]:
    """Artistes suivis dont le nom contient `needle` (tous si vide)."""
    names = list_artist_names()
    key = _cmp_key(needle)
    return [n for n in names if key in _cmp_key(n)] if key else names


def artist_in_file(name: str) -> str | None:
    """Retourne le nom tel qu'écrit dans le fichier s'il y est déjà."""
    key = _cmp_key(name)
    for existing in list_artist_names():
        if _cmp_key(existing) == key:
            return existing
    return None


def add_artist(name: str, itunes_id: int, matched_name: str, forced: bool) -> bool:
    """
    Ajoute l'artiste à artists.txt (à sa place alphabétique) et mémorise son ID
    en base pour qu'il n'ait pas à être recherché. `forced` écrit `Nom | id`.
    Retourne False si le nom était déjà dans le fichier.
    """
    name = name.strip()
    new_line = f"{name} | {itunes_id}" if forced else name
    with _artists_lock:
        lines = _read_lines()
        key = _cmp_key(name)
        for i, line in enumerate(lines):
            if _is_artist_line(line) and _cmp_key(line.partition("|")[0]) == key:
                # Déjà là : si on force un ID, on met la ligne à jour.
                if forced and line.strip() != new_line:
                    lines[i] = new_line
                    _write_lines(lines)
                    _db_set_artist(name, itunes_id, matched_name)
                    return True
                return False
        # Insertion avant le premier artiste qui vient après alphabétiquement.
        new_key = _sort_key(new_line)
        insert_at = None
        last_artist = -1
        for i, line in enumerate(lines):
            if not _is_artist_line(line):
                continue
            last_artist = i
            if _sort_key(line) > new_key:
                insert_at = i
                break
        if insert_at is None:
            insert_at = last_artist + 1 if last_artist >= 0 else len(lines)
        lines.insert(insert_at, new_line)
        _write_lines(lines)
    _db_set_artist(name, itunes_id, matched_name)
    return True


def _db_set_artist(name: str, itunes_id: int, matched_name: str) -> None:
    conn = _db()
    conn.execute("INSERT OR REPLACE INTO artists VALUES (?, ?, ?)", (name, itunes_id, matched_name))
    conn.commit()
    conn.close()


def remove_artist(query: str) -> dict:
    """
    Retire un artiste de artists.txt ET de la base (sinon il resterait scanné).
    Nom exact d'abord, sinon un nom qui contient la recherche s'il est unique.
    Retourne {"status": "removed", "names": [...]} | {"status": "ambiguous",
    "candidates": [...]} | {"status": "none"}.
    """
    key = _cmp_key(query)
    if not key:
        return {"status": "none"}
    with _artists_lock:
        lines = _read_lines()
        entries = [
            (i, ln.partition("|")[0].strip()) for i, ln in enumerate(lines) if _is_artist_line(ln)
        ]
        targets = [(i, n) for i, n in entries if _cmp_key(n) == key]
        if not targets:
            partial = [(i, n) for i, n in entries if key in _cmp_key(n)]
            if len(partial) > 1:
                return {"status": "ambiguous", "candidates": [n for _, n in partial]}
            targets = partial
        if not targets:
            return {"status": "none"}
        drop = {i for i, _ in targets}
        _write_lines([ln for i, ln in enumerate(lines) if i not in drop])
    names = [n for _, n in targets]
    conn = _db()
    conn.executemany("DELETE FROM artists WHERE name = ?", [(n,) for n in names])
    conn.commit()
    conn.close()
    return {"status": "removed", "names": names}


# --------------------------------------------------------------------------- #
# Appels iTunes
# --------------------------------------------------------------------------- #

def _get_json(url: str, params: dict) -> dict:
    """GET avec 1 retry en cas d'erreur (403/429/réseau) après une pause."""
    for attempt in (1, 2):
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_S)
            if resp.status_code in (403, 429):
                raise requests.HTTPError(f"HTTP {resp.status_code} (rate limit ?)")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise
            logger.warning("Appel iTunes échoué (%s), nouvelle tentative dans 30 s", exc)
            time.sleep(30)
    return {}  # inatteignable, pour le typage


# Un artiste dont le genre Apple Music ne contient aucun de ces mots est
# signalé "à vérifier" (ex. un groupe de rock qui porte le même nom qu'un
# rappeur). Adapte la liste à tes goûts ; ça ne bloque rien, ça alerte juste.
EXPECTED_GENRE_HINTS = ("hip", "rap", "r&b", "soul", "pop", "trap", "urban")


def _search_artist(name: str) -> tuple[int, str, str, bool] | None:
    """
    Retourne (id, nom trouvé, genre principal, ambigu) ou None.
    `ambigu` = plusieurs artistes distincts du catalogue portent exactement ce nom.
    """
    data = _get_json(
        ITUNES_SEARCH,
        {"term": name, "entity": "musicArtist", "limit": 5, "country": COUNTRY},
    )
    results = data.get("results", [])
    if not results:
        return None
    # Priorité à la correspondance exacte (insensible à la casse), sinon au 1er.
    exact = [r for r in results if r.get("artistName", "").casefold() == name.casefold()]
    chosen = exact[0] if exact else results[0]
    # Le même artiste peut remonter plusieurs fois avec le même ID : on compte
    # les ID distincts pour ne pas signaler un faux homonyme.
    distinct_exact = {r.get("artistId") for r in exact}
    return (
        chosen["artistId"],
        chosen["artistName"],
        chosen.get("primaryGenreName", "?"),
        len(distinct_exact) > 1,
    )


def resolve_artists() -> dict:
    """
    Associe chaque nom de artists.txt à un ID Apple Music (mis en cache en
    base : un artiste déjà résolu n'est jamais re-cherché). Retourne un bilan
    avec les artistes introuvables et ceux dont le nom trouvé diffère du nom
    demandé (à vérifier à la main, ex. homonymes). Les artistes retirés du
    fichier sont aussi retirés de la base : ils ne sont plus scannés.
    """
    conn = _db()
    bilan: dict = {"resolved": 0, "cached": 0, "removed": 0, "not_found": [], "to_check": []}

    lines = read_artist_names()
    file_names = {ln.partition("|")[0].strip() for ln in lines}

    # Purge. Garde-fou : si le fichier paraît vide, on ne touche à rien.
    if file_names:
        for (db_name,) in conn.execute("SELECT name FROM artists").fetchall():
            if db_name not in file_names:
                conn.execute("DELETE FROM artists WHERE name = ?", (db_name,))
                bilan["removed"] += 1
        conn.commit()

    for line in lines:
        name, _, forced_id = (p.strip() for p in line.partition("|"))

        if forced_id.isdigit():
            conn.execute(
                "INSERT OR REPLACE INTO artists VALUES (?, ?, ?)",
                (name, int(forced_id), name),
            )
            conn.commit()
            bilan["resolved"] += 1
            continue

        row = conn.execute("SELECT itunes_id FROM artists WHERE name = ?", (name,)).fetchone()
        if row and row[0]:
            bilan["cached"] += 1
            continue

        time.sleep(REQUEST_DELAY_S)
        try:
            found = _search_artist(name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Recherche de '%s' échouée : %s", name, exc)
            bilan["not_found"].append(f"{name} (erreur réseau)")
            continue

        if not found:
            bilan["not_found"].append(name)
            continue

        itunes_id, matched, genre, ambiguous = found
        conn.execute("INSERT OR REPLACE INTO artists VALUES (?, ?, ?)", (name, itunes_id, matched))
        conn.commit()
        bilan["resolved"] += 1

        reasons = []
        if matched.casefold() != name.casefold():
            reasons.append("nom différent")
        if ambiguous:
            reasons.append("plusieurs artistes portent ce nom")
        if not any(hint in genre.casefold() for hint in EXPECTED_GENRE_HINTS):
            reasons.append("genre inattendu")
        if reasons:
            bilan["to_check"].append(
                f"{name} → {matched} [{genre}] (id {itunes_id}) : {', '.join(reasons)}"
            )

    conn.close()
    return bilan


# --------------------------------------------------------------------------- #
# Ajout d'un artiste depuis Telegram
# --------------------------------------------------------------------------- #

_URL_ID_RE = re.compile(r"music\.apple\.com/\S*?/artist/(?:[^/\s?#]+/)?(\d+)")


def search_artists(term: str, limit: int = 5) -> list[dict]:
    """Candidats d'une recherche, dédoublonnés par ID : [{id, name, genre, url}]."""
    time.sleep(REQUEST_DELAY_S)
    data = _get_json(
        ITUNES_SEARCH,
        {"term": term, "entity": "musicArtist", "limit": 10, "country": COUNTRY},
    )
    out: list[dict] = []
    seen_ids: set[int] = set()
    for r in data.get("results", []):
        aid = r.get("artistId")
        if not aid or aid in seen_ids:
            continue
        seen_ids.add(aid)
        out.append(
            {
                "id": aid,
                "name": r.get("artistName", "?"),
                "genre": r.get("primaryGenreName", "?"),
                "url": r.get("artistLinkUrl", ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def lookup_artist(artist_id: int) -> dict | None:
    time.sleep(REQUEST_DELAY_S)
    data = _get_json(ITUNES_LOOKUP, {"id": artist_id, "country": COUNTRY})
    for r in data.get("results", []):
        if r.get("wrapperType") == "artist":
            return {
                "id": r.get("artistId", artist_id),
                "name": r.get("artistName", "?"),
                "genre": r.get("primaryGenreName", "?"),
                "url": r.get("artistLinkUrl", ""),
            }
    return None


def recent_releases(artist_id: int, n: int = 3) -> list[Release]:
    """Les n sorties les plus récentes d'un artiste (pour vérifier que c'est le bon)."""
    time.sleep(REQUEST_DELAY_S)
    rels = _fetch_artist_releases(artist_id)
    return sorted(rels, key=lambda r: r.release_date, reverse=True)[:n]


def prepare_add(text: str) -> dict:
    """
    Interprète `/ajouter ...` et dit quoi faire :
      {"kind": "exists", "name"}                       déjà dans le fichier
      {"kind": "ready", "name", "id", "matched", "genre", "url", "forced"}
      {"kind": "choose", "name", "candidates": [{id,name,genre,url,last}]}
      {"kind": "none"}                                 rien trouvé
    Formes acceptées : `Nom`, `Nom | id`, ou l'URL d'une page artiste Apple Music.
    """
    text = text.strip()
    forced_id: int | None = None
    name = text

    m = _URL_ID_RE.search(text)
    if m:
        forced_id, name = int(m.group(1)), ""
    elif "|" in text:
        left, _, right = text.partition("|")
        if right.strip().isdigit():
            forced_id, name = int(right.strip()), left.strip()

    if forced_id is not None:
        info = lookup_artist(forced_id)
        if not info:
            return {"kind": "none"}
        name = name or info["name"]
        if artist_in_file(name):
            return {"kind": "exists", "name": artist_in_file(name)}
        return {
            "kind": "ready", "name": name, "id": forced_id, "matched": info["name"],
            "genre": info["genre"], "url": info["url"], "forced": True,
        }

    if not name:
        return {"kind": "none"}
    existing = artist_in_file(name)
    if existing:
        return {"kind": "exists", "name": existing}

    cands = search_artists(name)
    if not cands:
        return {"kind": "none"}
    key = _cmp_key(name)
    exact = [c for c in cands if _cmp_key(c["name"]) == key]
    if len(exact) == 1:
        c = exact[0]
        return {
            "kind": "ready", "name": name, "id": c["id"], "matched": c["name"],
            "genre": c["genre"], "url": c["url"], "forced": False,
        }
    # Homonymes ou nom approximatif : on te laisse choisir, avec la dernière
    # sortie de chacun pour reconnaître le bon.
    shortlist = (exact or cands)[:4]
    for c in shortlist:
        try:
            last = recent_releases(c["id"], 1)
            c["last"] = f"{last[0].title} ({last[0].release_date:%d/%m/%Y})" if last else "aucune sortie"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Aperçu de %s impossible : %s", c["name"], exc)
            c["last"] = "?"
    return {"kind": "choose", "name": name, "candidates": shortlist}


# --------------------------------------------------------------------------- #
# Récupération des sorties
# --------------------------------------------------------------------------- #

_EP_TAIL_RE = re.compile(r"(?:\s|[\(\[])E\.?P\.?[\)\]]?$", re.I)


def _kind_and_title(collection_name: str) -> tuple[str, str]:
    """
    Apple suffixe les titres : 'Titre - Single', 'Titre - EP'. Certains labels
    écrivent l'EP dans le titre ('Titre Ep', 'Titre (EP)') : on le reconnaît aussi.
    """
    for suffix, kind in ((" - Single", "Single"), (" - EP", "EP")):
        if collection_name.endswith(suffix):
            return kind, collection_name[: -len(suffix)]
    if _EP_TAIL_RE.search(collection_name):
        return "EP", collection_name
    return "Album", collection_name


def _rank(r: Release) -> tuple:
    """Entre deux fiches d'une même sortie : l'explicite, puis la plus complète, puis la 1re publiée."""
    return (not r.explicit, -r.track_count, r.release_date, r.album_id)


def _dedupe(releases: list[Release]) -> list[Release]:
    """
    Apple publie souvent plusieurs fiches pour une même sortie (version
    explicite + version "clean", ou doublon de catalogue), avec des ID
    différents. On n'en garde qu'une par (artiste, type, titre).
    """
    best: dict[tuple[str, str, str], Release] = {}
    for r in releases:
        cur = best.get(r.dup_key)
        if cur is None or _rank(r) < _rank(cur):
            best[r.dup_key] = r
    keep = {id(r) for r in best.values()}
    return [r for r in releases if id(r) in keep]


def _fetch_artist_releases(itunes_id: int) -> list[Release]:
    data = _get_json(
        ITUNES_LOOKUP,
        {
            "id": itunes_id,
            "entity": "album",
            "sort": "recent",
            "limit": ALBUMS_PER_ARTIST,
            "country": COUNTRY,
        },
    )
    releases: list[Release] = []
    for item in data.get("results", []):
        if item.get("wrapperType") != "collection":
            continue  # le 1er résultat est l'artiste lui-même
        try:
            kind, title = _kind_and_title(item["collectionName"])
            released = datetime.fromisoformat(
                item["releaseDate"].replace("Z", "+00:00")
            ).astimezone(TZ).date()
            cover = re.sub(r"/\d+x\d+bb", "/300x300bb", item.get("artworkUrl100", ""))
            releases.append(
                Release(
                    album_id=item["collectionId"],
                    artist=item.get("artistName", "?"),
                    title=title,
                    kind=kind,
                    release_date=released,
                    url=item["collectionViewUrl"],
                    artist_id=itunes_id,
                    cover=cover,
                    track_count=int(item.get("trackCount") or 0),
                    explicit=item.get("collectionExplicitness") == "explicit",
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Entrée album ignorée (%s) : %s", exc, item.get("collectionName"))
    return releases


def _scan(since: date) -> tuple[list[Release], dict[int, list[Release]]]:
    """
    Parcourt tous les artistes résolus (~3 s par artiste pour respecter la
    limite : compte 8-9 min pour 160 artistes). Retourne les sorties dont la
    date est >= `since` (futures comprises) ainsi que le catalogue récent de
    chaque artiste, réutilisé pour l'analyse de nouveauté sans appel de plus.
    Dédoublonne par album_id (un feat. peut apparaître chez plusieurs artistes).
    """
    conn = _db()
    # DISTINCT : deux lignes d'artists.txt peuvent pointer vers le même artiste
    # (ex. "Kanye West" et "Ye") ; inutile de l'interroger deux fois.
    artist_ids = [
        r[0] for r in conn.execute("SELECT DISTINCT itunes_id FROM artists WHERE itunes_id IS NOT NULL")
    ]
    conn.close()

    if not artist_ids:
        raise RuntimeError("Aucun artiste résolu : lance d'abord `resolve`.")

    found: dict[int, Release] = {}
    catalog: dict[int, list[Release]] = {}
    errors = 0
    for aid in artist_ids:
        time.sleep(REQUEST_DELAY_S)
        try:
            rels = _fetch_artist_releases(aid)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.error("Artiste %s : échec de récupération (%s)", aid, exc)
            continue
        catalog[aid] = rels
        for rel in rels:
            if rel.release_date >= since:
                found[rel.album_id] = rel

    if errors:
        logger.warning("%d artiste(s) en erreur sur %d", errors, len(artist_ids))
    unique = _dedupe(list(found.values()))
    ordered = sorted(unique, key=lambda r: (r.release_date, r.artist.casefold()), reverse=True)
    return ordered, catalog


def _today() -> date:
    return datetime.now(TZ).date()


def _make_batch(since: date, upcoming_days: int = 0) -> Batch:
    found, catalog = _scan(since)
    today = _today()
    past = [r for r in found if r.release_date <= today]
    future: list[Release] = []
    if upcoming_days:
        limit = today + timedelta(days=upcoming_days)
        future = sorted(
            (r for r in found if today < r.release_date <= limit),
            key=lambda r: (r.release_date, r.artist.casefold()),
        )
    return Batch(releases=past, upcoming=future, catalog=catalog)


def daily_batch() -> Batch:
    """
    Sorties des 2 derniers jours PAS ENCORE notifiées. Les sorties déjà
    envoyées sont mémorisées : pas de doublon d'un jour à l'autre. Les sorties
    à date future (pré-sorties) sont ignorées et seront captées le jour J.
    """
    batch = _make_batch(_today() - timedelta(days=2))
    conn = _db()
    seen = {r[0] for r in conn.execute("SELECT album_id FROM seen")}
    conn.close()
    # Une autre fiche de la même sortie a déjà été notifiée (ex. la version
    # "clean" hier, l'explicite aujourd'hui) : pas de deuxième notification.
    notified_keys = {r.dup_key for r in _all_releases()}
    batch.releases = [
        r for r in batch.releases if r.album_id not in seen and r.dup_key not in notified_keys
    ]
    return batch


def weekly_batch() -> Batch:
    """Sorties du samedi dernier à aujourd'hui, plus les sorties à venir."""
    return _make_batch(_today() - timedelta(days=6), upcoming_days=UPCOMING_DAYS)


def catchup_batch(days: int) -> Batch:
    return _make_batch(_today() - timedelta(days=days))


# --------------------------------------------------------------------------- #
# Analyse de nouveauté : qu'est-ce que cette sortie t'apporte vraiment ?
# --------------------------------------------------------------------------- #

_FEAT_PAREN_RE = re.compile(r"[\(\[]\s*(?:feat|ft|featuring|with|prod|produced)\b[^\)\]]*[\)\]]", re.I)
_NOISE_PAREN_RE = re.compile(r"[\(\[]\s*(?:explicit|clean)\s*[\)\]]", re.I)
_FEAT_TAIL_RE = re.compile(r"\s+(?:feat|ft|featuring)\.?\s.*$", re.I)

_EDITION_WORDS = (
    r"(?:deluxe|expanded|extended|anniversary|edition|édition|reissue|re-issue|"
    r"remaster(?:ed)?|complete|platinum|collector'?s?|special|bonus)"
)
_EDITION_PAREN_RE = re.compile(rf"\s*[\(\[][^\)\]]*\b{_EDITION_WORDS}\b[^\)\]]*[\)\]]\s*$", re.I)
_EDITION_DASH_RE = re.compile(rf"\s+[-–—]\s+[^-–—]*\b{_EDITION_WORDS}\b[^-–—]*$", re.I)

_VARIANT_RE = re.compile(
    r"\b(remix|rmx|instrumental|sped[ -]?up|slowed|reverb|acoustic|live|demo|"
    r"a cappella|acapella|karaoke|edit|version|cover)\b",
    re.I,
)
_EXTRAS_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]|\s[-–—]\s(.*)$")
# Mots assez rares dans un vrai titre pour être cherchés partout (pas seulement
# entre parenthèses) : "Album slowed + reverb", "Projet Sped Up"…
_VARIANT_STRONG_RE = re.compile(
    r"\b(sped[ -]?up|slowed|reverb|nightcore|instrumentals?|a cappella|acapella|karaoke)\b", re.I
)


def _norm_title(title: str) -> str:
    """
    Titre normalisé pour comparer 'Song (feat. X)' et 'Song'. On ne retire que
    les mentions de featuring/explicit : 'Song (Remix)' reste différent de 'Song'.
    """
    t = _strip_accents(title).casefold()
    t = _FEAT_PAREN_RE.sub(" ", t)
    t = _NOISE_PAREN_RE.sub(" ", t)
    t = _FEAT_TAIL_RE.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _split_edition(title: str) -> tuple[str, bool]:
    """'Album (Deluxe)' -> ('Album', True). Retire les suffixes d'édition en chaîne."""
    base, found = title, False
    while True:
        new = _EDITION_DASH_RE.sub("", _EDITION_PAREN_RE.sub("", base))
        if new == base:
            break
        base, found = new, True
    return base.strip(), found


def _is_variant(title: str) -> bool:
    """Slowed, sped up, instru… n'importe où ; remix, live, version… entre parenthèses ou après un tiret."""
    if _VARIANT_STRONG_RE.search(title):
        return True
    for m in _EXTRAS_RE.finditer(title):
        extra = m.group(1) or m.group(2) or ""
        if _VARIANT_RE.search(extra):
            return True
    return False


_VARIANT_LABELS = (
    (re.compile(r"\bsped[ -]?up\b", re.I), "sped up"),
    (re.compile(r"\bslowed\b", re.I), "slowed"),
    (re.compile(r"\bnightcore\b", re.I), "nightcore"),
    (re.compile(r"\binstrumentals?\b", re.I), "instru"),
    (re.compile(r"\b(a cappella|acapella)\b", re.I), "a cappella"),
    (re.compile(r"\bkaraoke\b", re.I), "karaoké"),
    (re.compile(r"\b(remix|rmx)\b", re.I), "remix"),
    (re.compile(r"\bacoustic\b", re.I), "acoustique"),
    (re.compile(r"\blive\b", re.I), "live"),
    (re.compile(r"\bdemo\b", re.I), "démo"),
    (re.compile(r"\bcover\b", re.I), "cover"),
    (re.compile(r"\bedit\b", re.I), "edit"),
)


def _variant_label(title: str) -> str:
    for rx, label in _VARIANT_LABELS:
        if rx.search(title):
            return label
    return "alternative"


def _variant_base(title: str) -> str:
    """'QRÖMELIFE (Slowed + Reverb)' -> 'QRÖMELIFE' ; 'Song - Sped Up' -> 'Song'."""
    def drop_if_variant(m: re.Match) -> str:
        part = m.group(0)
        return "" if (_VARIANT_RE.search(part) or _VARIANT_STRONG_RE.search(part)) else part

    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", drop_if_variant, title)
    t = re.sub(r"\s[-–—]\s.*$", drop_if_variant, t)
    t = _VARIANT_STRONG_RE.sub(" ", t)
    return re.sub(r"[\s+&,-]+$", "", t).strip() or title


def _find_original(r: Release, catalog: dict[int, list[Release]]) -> str:
    """
    Cherche l'original d'une variante chez le même artiste : une sortie du même
    titre (catalogue récent), sinon un titre d'album déjà analysé. Retourne un
    texte prêt à afficher, ou "" si rien n'est trouvé.
    """
    want = _norm_title(_variant_base(r.title))
    if not want:
        return ""
    candidates = sorted(
        (
            x for x in catalog.get(r.artist_id, [])
            if x.album_id != r.album_id and not x.is_variant
            and want in (_norm_title(x.title), _norm_title(_split_edition(x.title)[0]))
        ),
        key=lambda x: x.release_date,
    )
    if candidates:
        x = candidates[0]
        return f"de « {x.title} » ({x.kind.lower()} du {x.release_date:%d/%m/%Y})"
    conn = _db()
    rows = conn.execute(
        "SELECT t.title, rl.title FROM tracks t JOIN releases rl ON rl.album_id = t.album_id"
        " WHERE rl.artist_id = ? AND t.album_id != ? ORDER BY rl.release_date",
        (r.artist_id, r.album_id),
    ).fetchall()
    conn.close()
    for track, album in rows:
        if _norm_title(track) == want and not _is_variant(album):
            return f"de « {track} » (album « {album} »)"
    return ""


def _tracks_of(album_id: int) -> list[str]:
    """Titres d'un album : depuis le cache, sinon 1 appel iTunes."""
    conn = _db()
    rows = conn.execute("SELECT title FROM tracks WHERE album_id = ? ORDER BY pos", (album_id,)).fetchall()
    conn.close()
    if rows:
        return [r[0] for r in rows]

    time.sleep(REQUEST_DELAY_S)
    data = _get_json(ITUNES_LOOKUP, {"id": album_id, "entity": "song", "limit": 200, "country": COUNTRY})
    items = [
        (int(i.get("discNumber") or 1), int(i.get("trackNumber") or 0), i["trackName"])
        for i in data.get("results", [])
        if i.get("wrapperType") == "track" and i.get("trackName")
    ]
    items.sort(key=lambda x: (x[0], x[1]))
    titles = [t for _, _, t in items]
    if titles:
        conn = _db()
        conn.executemany(
            "INSERT OR REPLACE INTO tracks VALUES (?, ?, ?)",
            [(album_id, pos, t) for pos, t in enumerate(titles)],
        )
        conn.commit()
        conn.close()
    return titles


def _known_from_db(artist_id: int, album_id: int, before: date) -> set[str]:
    """Titres de tout autre album déjà analysé du même artiste, sorti avant."""
    conn = _db()
    rows = conn.execute(
        "SELECT t.title FROM tracks t JOIN releases r ON r.album_id = t.album_id"
        " WHERE r.artist_id = ? AND t.album_id != ? AND r.release_date < ?",
        (artist_id, album_id, before.isoformat()),
    ).fetchall()
    conn.close()
    return {_norm_title(r[0]) for r in rows}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n <= 1 else many}"


def analyze_release(r: Release, catalog: dict[int, list[Release]]) -> None:
    """
    Remplit r.note / r.new_tracks / r.total_tracks.

    - Single : repère seulement les versions alternatives (remix, instru…).
    - Album/EP : récupère la liste des titres (1 appel) et la compare avec
        * les singles de l'artiste sortis avant (sans appel : déjà dans le catalogue),
        * l'album d'origine si c'est une réédition (deluxe, expanded…),
        * les autres albums déjà analysés du même artiste.
      Si aucune référence n'existe, aucune affirmation n'est faite.
    """
    r.analyzed = True
    base_title, is_edition = _split_edition(r.title)

    if r.is_variant:
        # Version slowed/sped up/remix… : on dit de quel son elle est la
        # version, sans comparer les titres (ils diffèrent tous par le suffixe).
        origin = _find_original(r, catalog)
        r.note = f"🔁 version {_variant_label(r.title)}" + (f" {origin}" if origin else "")
        return
    if r.kind == "Single":
        return
    if not NOVELTY_ENABLED:
        return

    try:
        tracks = _tracks_of(r.album_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Liste des titres de '%s' indisponible (%s)", r.title, exc)
        r.analyzed = False  # on réessaiera à la prochaine occasion
        return
    if not tracks:
        return
    r.total_tracks = len(tracks)

    prior = [
        x for x in catalog.get(r.artist_id, [])
        if x.album_id != r.album_id and x.release_date < r.release_date
    ]
    from_singles = {_norm_title(x.title) for x in prior if x.kind == "Single"}

    # Album d'origine : Apple date parfois la réédition du même jour que
    # l'original, d'où le <= (et non <) ici.
    same_or_before = [
        x for x in catalog.get(r.artist_id, [])
        if x.album_id != r.album_id and x.release_date <= r.release_date
        and x.kind != "Single" and not x.is_variant
    ]
    base_norms: set[str] = set()
    base_found = False
    want = _norm_title(base_title)
    if is_edition:
        matches = sorted(
            (x for x in same_or_before if _norm_title(_split_edition(x.title)[0]) == want),
            key=lambda x: (x.is_edition, x.release_date),  # l'édition standard d'abord
        )
    else:
        # Réédition sans mot-clé ("Album 2.0", "Album: Reloaded") : un album
        # précédent dont le titre est le début de celui-ci, et qui partage
        # au moins la moitié de ses titres.
        matches = sorted(
            (
                x for x in same_or_before
                if not x.is_edition and want.startswith(_norm_title(x.title) + " ")
            ),
            key=lambda x: x.release_date,
        )
    if matches:
        try:
            base_norms = {_norm_title(t) for t in _tracks_of(matches[0].album_id)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Album d'origine de '%s' indisponible (%s)", r.title, exc)
        overlap = sum(1 for t in tracks if _norm_title(t) in base_norms)
        if is_edition:
            base_found = bool(base_norms)
        elif base_norms and overlap * 2 >= len(base_norms):
            base_found = is_edition = True
        else:
            base_norms = set()

    from_db = _known_from_db(r.artist_id, r.album_id, r.release_date)
    known = from_singles | base_norms | from_db
    norms = [_norm_title(t) for t in tracks]
    known_count = sum(1 for t in norms if t in known)
    new_count = len(tracks) - known_count
    only_singles = all(t in from_singles for t in norms if t in known)
    s = "s" if known_count > 1 else ""
    already = f"{known_count} déjà sorti{s}" + (" en single" if only_singles else "")

    if is_edition and base_found:
        r.new_tracks = new_count
        r.note = (
            "♻️ réédition · aucun inédit"
            if new_count == 0
            else f"♻️ réédition · {_plural(new_count, 'inédit', 'inédits')} sur {len(tracks)}"
        )
    elif known_count:
        r.new_tracks = new_count
        if new_count == 0:
            r.note = f"🔁 aucun inédit · {already}"
        else:
            r.note = f"🆕 {_plural(new_count, 'inédit', 'inédits')} sur {len(tracks)} · {already}"
    elif is_edition:
        r.note = "♻️ réédition"


# --------------------------------------------------------------------------- #
# Préparation d'un envoi, tri et file d'attente
# --------------------------------------------------------------------------- #

def _row_to_release(row: tuple) -> Release:
    (album_id, artist, artist_id, title, kind, release_date, url, cover,
     track_count, total_tracks, new_tracks, note, state, analyzed) = row
    return Release(
        album_id=album_id, artist=artist or "?", artist_id=artist_id or 0, title=title or "?",
        kind=kind or "Album", release_date=date.fromisoformat(release_date), url=url or "",
        cover=cover or "", track_count=track_count or 0, total_tracks=total_tracks,
        new_tracks=new_tracks, note=note or "", state=state or STATE_NEW, analyzed=bool(analyzed),
    )


_COLS = (
    "album_id, artist, artist_id, title, kind, release_date, url, cover,"
    " track_count, total_tracks, new_tracks, note, state, analyzed"
)


def _load_releases(ids: list[int]) -> dict[int, Release]:
    out: dict[int, Release] = {}
    conn = _db()
    for i in range(0, len(ids), 400):  # limite SQLite de 999 variables
        chunk = ids[i : i + 400]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT {_COLS} FROM releases WHERE album_id IN ({marks})", chunk):
            out[row[0]] = _row_to_release(row)
    conn.close()
    return out


def save_releases(releases: list[Release]) -> None:
    """Enregistre/actualise les sorties. L'état de tri existant n'est jamais écrasé."""
    conn = _db()
    conn.executemany(
        "INSERT INTO releases (" + _COLS + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(album_id) DO UPDATE SET"
        " artist=excluded.artist, artist_id=excluded.artist_id, title=excluded.title,"
        " kind=excluded.kind, release_date=excluded.release_date, url=excluded.url,"
        " cover=excluded.cover, track_count=excluded.track_count,"
        " total_tracks=excluded.total_tracks, new_tracks=excluded.new_tracks,"
        " note=excluded.note, analyzed=excluded.analyzed",
        [
            (r.album_id, r.artist, r.artist_id, r.title, r.kind, r.release_date.isoformat(), r.url,
             r.cover, r.track_count, r.total_tracks, r.new_tracks, r.note, r.state, int(r.analyzed))
            for r in releases
        ],
    )
    conn.commit()
    conn.close()


def _all_releases() -> list[Release]:
    conn = _db()
    rows = conn.execute(f"SELECT {_COLS} FROM releases").fetchall()
    conn.close()
    return [_row_to_release(r) for r in rows]


def _absorb_twins(rels: list[Release]) -> dict[int, str]:
    """
    Si la base contient une autre fiche de la même sortie (doublon enregistré
    avant ce correctif, ou version "clean"), on la supprime au profit de celle
    du scan, en lui transmettant ton tri. Retourne {album_id: état hérité}.
    """
    by_key: dict[tuple[str, str, str], list[Release]] = {}
    for row in _all_releases():
        by_key.setdefault(row.dup_key, []).append(row)

    inherited: dict[int, str] = {}
    to_delete: list[int] = []
    for r in rels:
        twins = [t for t in by_key.get(r.dup_key, []) if t.album_id != r.album_id]
        if not twins:
            continue
        decided = next((t.state for t in twins if t.state != STATE_NEW), None)
        if decided:
            inherited[r.album_id] = decided
        to_delete += [t.album_id for t in twins]
    if to_delete:
        conn = _db()
        conn.executemany("DELETE FROM releases WHERE album_id = ?", [(i,) for i in to_delete])
        conn.commit()
        conn.close()
    return inherited


def prepare(batch: Batch) -> Prepared:
    """
    Reprend ton tri existant, analyse ce qui ne l'a pas encore été (seulement
    les sorties non triées), enregistre le tout et sépare ce qui mérite d'être
    montré de ce qui ne t'apporte rien.
    """
    rels = batch.releases
    inherited = _absorb_twins(rels)
    existing = _load_releases([r.album_id for r in rels])
    for r in rels:
        old = existing.get(r.album_id)
        if old:
            r.state = old.state
            if old.analyzed:
                r.total_tracks, r.new_tracks, r.note, r.analyzed = (
                    old.total_tracks, old.new_tracks, old.note, True,
                )
        if r.state == STATE_NEW and r.album_id in inherited:
            r.state = inherited[r.album_id]
    for r in rels:
        if r.state == STATE_NEW and not r.analyzed:
            analyze_release(r, batch.catalog)
    save_releases(rels)
    # L'upsert ne touche jamais à l'état : on applique à part l'état hérité.
    for r in rels:
        if r.album_id in inherited:
            set_state([r.album_id], r.state)

    pending = [r for r in rels if r.state == STATE_NEW]
    return Prepared(
        to_show=[r for r in pending if not r.nothing_new],
        nothing_new=[r for r in pending if r.nothing_new],
        already_sorted=len(rels) - len(pending),
        upcoming=batch.upcoming,
        everything=rels,
    )


def mark_seen(releases: list[Release]) -> None:
    conn = _db()
    conn.executemany(
        "INSERT OR IGNORE INTO seen VALUES (?, ?)",
        [(r.album_id, _today().isoformat()) for r in releases],
    )
    conn.commit()
    conn.close()


def finalize(p: Prepared) -> None:
    """À appeler APRÈS l'envoi réussi : marque notifié, range les rééditions vides."""
    mark_seen(p.everything)
    set_state([r.album_id for r in p.nothing_new], STATE_IGNORED)


_OLD_NOTE_RE = re.compile(r"🆕 (\d+) nouveaux? / (\d+) · (\d+) déjà sortis?")


def migrate() -> int:
    """
    Remise à niveau de la base après une mise à jour (sans appel réseau) :
    anciens libellés réécrits, et une seule fois : les versions alternatives
    rangées automatiquement par la version précédente sont remises dans /trier.
    Retourne le nombre de sorties remises.
    """
    conn = _db()
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    restored = 0
    if not conn.execute("SELECT 1 FROM meta WHERE key = 'variants_restored'").fetchone():
        cur = conn.execute(
            "UPDATE releases SET state = ?, analyzed = 0 WHERE state = ? AND note LIKE '🔁 version alternative%'",
            (STATE_NEW, STATE_IGNORED),
        )
        restored = cur.rowcount
        conn.execute("INSERT INTO meta VALUES ('variants_restored', '1')")
    # L'ancien « À écouter » devient « Ajoutée ».
    conn.execute("UPDATE releases SET state = ? WHERE state = ?", (STATE_ADDED, STATE_TO_LISTEN))
    for album_id, note in conn.execute(
        "SELECT album_id, note FROM releases WHERE state = ?", (STATE_NEW,)
    ).fetchall():
        new_note = note or ""
        new_note = _OLD_NOTE_RE.sub(
            lambda m: f"🆕 {m[1]} inédit{'s' if int(m[1]) > 1 else ''} sur {m[2]} · {m[3]} déjà sorti{'s' if int(m[3]) > 1 else ''}",
            new_note,
        )
        new_note = new_note.replace("♻️ réédition · rien de nouveau", "♻️ réédition · aucun inédit")
        new_note = re.sub(r"♻️ réédition · (\d+) nouveaux? / (\d+)",
                          lambda m: f"♻️ réédition · {m[1]} inédit{'s' if int(m[1]) > 1 else ''} sur {m[2]}", new_note)
        if new_note != (note or ""):
            conn.execute("UPDATE releases SET note = ? WHERE album_id = ?", (new_note, album_id))
    # Albums/EP non triés restés sans verdict (ex. réédition datée du même jour
    # que l'original) : on les réanalysera au prochain scan.
    conn.execute(
        "UPDATE releases SET analyzed = 0 WHERE state = ? AND kind != 'Single'"
        " AND (note IS NULL OR note = '')",
        (STATE_NEW,),
    )
    conn.commit()
    conn.close()
    return restored


def set_state(ids: list[int], state: str) -> None:
    if not ids:
        return
    conn = _db()
    conn.executemany("UPDATE releases SET state = ? WHERE album_id = ?", [(state, i) for i in ids])
    conn.commit()
    conn.close()


def get_release(album_id: int) -> Release | None:
    return _load_releases([album_id]).get(album_id)


def count_state(state: str) -> int:
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM releases WHERE state = ?", (state,)).fetchone()[0]
    conn.close()
    return n


def list_state(state: str) -> list[Release]:
    conn = _db()
    rows = conn.execute(
        f"SELECT {_COLS} FROM releases WHERE state = ? ORDER BY release_date DESC, album_id DESC", (state,)
    ).fetchall()
    conn.close()
    return [_row_to_release(r) for r in rows]


def next_in_queue(state: str, after_id: int = 0) -> tuple[Release | None, int]:
    """
    Prochaine sortie de la file `state`, plus ancienne que `after_id` (0 = depuis
    le début), triée de la plus récente à la plus ancienne. Retourne aussi le
    nombre total de sorties dans cet état. L'ordre ne dépend d'aucune position
    mémorisée : « Plus tard » avance sans jamais boucler.
    """
    conn = _db()
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
            f"SELECT {_COLS} FROM releases WHERE state = ?"
            " ORDER BY release_date DESC, album_id DESC LIMIT 1",
            (state,),
        ).fetchone()
    conn.close()
    return (_row_to_release(row) if row else None), total


# --------------------------------------------------------------------------- #
# Formatage (HTML Telegram)
# --------------------------------------------------------------------------- #

def _link(r: Release) -> str:
    return f'<a href="{html.escape(r.url, quote=True)}">{html.escape(r.title)}</a>'


def card_text(r: Release) -> str:
    """Carte d'une sortie : 2 lignes."""
    kind = r.kind
    if r.kind != "Single" and r.total_tracks:
        kind += f" · {r.total_tracks} titres"
    meta = f"{html.escape(kind)} · {r.release_date:%d/%m/%Y}"
    if r.note:
        meta += f" · {html.escape(r.note)}"
    return f"<b>{html.escape(r.artist)}</b> — {_link(r)}\n<i>{meta}</i>"


def _short_tag(r: Release) -> str:
    """Étiquette courte pour la liste compacte."""
    if r.nothing_new:
        return " · ♻️ rien de neuf"
    if r.new_tracks is not None and r.total_tracks:
        return f" · 🆕 {r.new_tracks}/{r.total_tracks}"
    if r.is_variant:
        return f" · 🔁 {_variant_label(r.title)}"
    return ""


def split_for_telegram(lines: list[str]) -> list[str]:
    """Regroupe des lignes en messages < 4000 caractères, jamais au milieu d'une ligne."""
    messages: list[str] = []
    buffer = ""
    for line in lines:
        if len(buffer) + len(line) + 1 > TELEGRAM_MAX_LEN and buffer:
            messages.append(buffer)
            buffer = ""
        buffer += line + "\n"
    if buffer:
        messages.append(buffer)
    return messages


def format_messages(title: str, releases: list[Release], empty_text: str) -> list[str]:
    """Liste compacte, groupée par date puis triée par artiste."""
    if not releases:
        return [f"<b>{html.escape(title)}</b>\n\n{html.escape(empty_text)}"]

    lines = [f"<b>{html.escape(title)}</b> — {len(releases)} sortie(s)"]
    current_day: date | None = None
    for r in releases:
        if r.release_date != current_day:
            current_day = r.release_date
            lines.append(f"\n📅 <b>{current_day.strftime('%d/%m/%Y')}</b>")
        lines.append(
            f"• <b>{html.escape(r.artist)}</b> — {_link(r)} "
            f"<i>({html.escape(r.kind)})</i>{_short_tag(r)}"
        )
    return split_for_telegram(lines)


def format_nothing_new(releases: list[Release]) -> list[str]:
    if not releases:
        return []
    lines = ["♻️ <b>Rééditions sans nouveauté</b> (mises de côté, déjà dans l'album d'origine)"]
    lines += [f"• <b>{html.escape(r.artist)}</b> — {_link(r)}" for r in releases]
    return split_for_telegram(lines)


def format_upcoming(upcoming: list[Release]) -> list[str]:
    """Une ligne par sortie à venir, plafonnée pour rester discret."""
    if not upcoming:
        return []
    lines = ["🗓 <b>À venir</b>"]
    for r in upcoming[:UPCOMING_MAX_LINES]:
        lines.append(f"• {r.release_date:%d/%m} · <b>{html.escape(r.artist)}</b> — {_link(r)}")
    extra = len(upcoming) - UPCOMING_MAX_LINES
    if extra > 0:
        lines.append(f"<i>… et {extra} autre(s)</i>")
    return split_for_telegram(lines)


def format_list(title: str, releases: list[Release]) -> list[str]:
    """Liste simple de sorties."""
    lines = [f"<b>{html.escape(title)}</b> — {len(releases)}"]
    lines += [
        f"• {r.release_date:%d/%m} · <b>{html.escape(r.artist)}</b> — {_link(r)} "
        f"<i>({html.escape(r.kind)})</i>"
        for r in releases
    ]
    return split_for_telegram(lines)


# --------------------------------------------------------------------------- #
# CLI de debug (lecture seule : aucune écriture en base)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "resolve":
        print(json.dumps(resolve_artists(), indent=2, ensure_ascii=False))
    elif cmd == "daily":
        print("\n".join(format_messages("Sorties du jour", daily_batch().releases, "Rien de nouveau.")))
    elif cmd == "weekly":
        b = weekly_batch()
        print("\n".join(format_messages("Sorties de la semaine", b.releases, "Rien cette semaine.")))
        print("\n".join(format_upcoming(b.upcoming)))
    elif cmd == "catchup":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        print("\n".join(format_messages(f"Rattrapage ({n} jours)", catchup_batch(n).releases, "Rien trouvé.")))
    else:
        print(__doc__)
