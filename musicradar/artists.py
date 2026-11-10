"""
Artistes suivis : le fichier artists.txt, la résolution des noms en ID Apple
Music, l'ajout / le retrait depuis Telegram et la gestion des homonymes.

artists.txt : un artiste par ligne, `#` pour un commentaire. En cas d'homonyme,
`Nom | 123456789` force l'ID Apple Music (le nombre à la fin de l'URL
music.apple.com/.../artist/nom/123456789).
"""

from __future__ import annotations

import logging
import re
import threading

from . import config, storage, text
from .sources import itunes

logger = logging.getLogger("radar.artists")

# artists.txt est monté dans le conteneur en fichier unique : on ne le remplace
# jamais (pas de fichier temporaire + rename, ça casserait le montage), on le
# réécrit sur place. Le verrou évite qu'un scan lise un fichier à moitié écrit.
_lock = threading.Lock()


def is_artist_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _read_lines() -> list[str]:
    if not config.ARTISTS_FILE.exists():
        raise FileNotFoundError(f"Fichier introuvable : {config.ARTISTS_FILE}")
    return config.ARTISTS_FILE.read_text(encoding="utf-8").splitlines()


def _write_lines(lines: list[str]) -> None:
    # write_text ouvre en écriture et tronque : même fichier, même inode.
    config.ARTISTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_lines() -> list[str]:
    """Lignes d'artistes (sans commentaires ni lignes vides), `Nom | id` compris."""
    with _lock:
        lines = _read_lines()
    return [ln.strip() for ln in lines if is_artist_line(ln)]


def names() -> list[str]:
    """Noms seuls (sans les ` | id`), dans l'ordre du fichier."""
    return [ln.partition("|")[0].strip() for ln in read_lines()]


def matching(needle: str = "") -> list[str]:
    """Artistes suivis dont le nom contient `needle` (tous si vide)."""
    all_names = names()
    key = text.cmp_key(needle)
    return [n for n in all_names if key in text.cmp_key(n)] if key else all_names


def in_file(name: str) -> str | None:
    """Retourne le nom tel qu'écrit dans le fichier s'il y est déjà."""
    key = text.cmp_key(name)
    for existing in names():
        if text.cmp_key(existing) == key:
            return existing
    return None


def add(name: str, itunes_id: int, matched_name: str, forced: bool) -> bool:
    """
    Ajoute l'artiste à artists.txt (à sa place alphabétique) et mémorise son ID
    en base pour qu'il n'ait pas à être recherché. `forced` écrit `Nom | id`.
    Retourne False si le nom était déjà dans le fichier.
    """
    name = name.strip()
    if forced:
        storage.mark_verified(itunes_id)  # ID choisi explicitement : rien à revérifier
    new_line = f"{name} | {itunes_id}" if forced else name
    with _lock:
        lines = _read_lines()
        key = text.cmp_key(name)
        for i, line in enumerate(lines):
            if is_artist_line(line) and text.cmp_key(line.partition("|")[0]) == key:
                # Déjà là : si on force un ID, on met la ligne à jour.
                if forced and line.strip() != new_line:
                    lines[i] = new_line
                    _write_lines(lines)
                    storage.set_artist(name, itunes_id, matched_name)
                    return True
                return False
        # Insertion avant le premier artiste qui vient après alphabétiquement.
        new_key = text.sort_key(new_line)
        insert_at = None
        last_artist = -1
        for i, line in enumerate(lines):
            if not is_artist_line(line):
                continue
            last_artist = i
            if text.sort_key(line) > new_key:
                insert_at = i
                break
        if insert_at is None:
            insert_at = last_artist + 1 if last_artist >= 0 else len(lines)
        lines.insert(insert_at, new_line)
        _write_lines(lines)
    storage.set_artist(name, itunes_id, matched_name)
    return True


def remove(query: str) -> dict:
    """
    Retire un artiste de artists.txt ET de la base (sinon il resterait scanné).
    Nom exact d'abord, sinon un nom qui contient la recherche s'il est unique.
    Retourne {"status": "removed", "names": [...]} | {"status": "ambiguous",
    "candidates": [...]} | {"status": "none"}.
    """
    key = text.cmp_key(query)
    if not key:
        return {"status": "none"}
    with _lock:
        lines = _read_lines()
        entries = [(i, ln.partition("|")[0].strip()) for i, ln in enumerate(lines) if is_artist_line(ln)]
        targets = [(i, n) for i, n in entries if text.cmp_key(n) == key]
        if not targets:
            partial = [(i, n) for i, n in entries if key in text.cmp_key(n)]
            if len(partial) > 1:
                return {"status": "ambiguous", "candidates": [n for _, n in partial]}
            targets = partial
        if not targets:
            return {"status": "none"}
        drop = {i for i, _ in targets}
        _write_lines([ln for i, ln in enumerate(lines) if i not in drop])
    removed = [n for _, n in targets]
    storage.delete_artists(removed)
    return {"status": "removed", "names": removed}


# --------------------------------------------------------------------------- #
# Résolution des noms en ID Apple Music
# --------------------------------------------------------------------------- #

def resolve() -> dict:
    """
    Associe chaque nom de artists.txt à un ID Apple Music (mis en cache en
    base : un artiste déjà résolu n'est jamais re-cherché). Retourne un bilan
    avec les artistes introuvables et ceux à vérifier à la main (nom différent,
    homonymes, genre inattendu). Les artistes retirés du fichier sont aussi
    retirés de la base : ils ne sont plus scannés.
    """
    bilan: dict = {"resolved": 0, "cached": 0, "removed": 0, "not_found": [], "to_check": []}
    lines = read_lines()
    file_names = {ln.partition("|")[0].strip() for ln in lines}
    known = storage.artist_rows()

    # Purge. Garde-fou : si le fichier paraît vide, on ne touche à rien.
    if file_names:
        gone = [n for n in known if n not in file_names]
        storage.delete_artists(gone)
        bilan["removed"] = len(gone)

    for line in lines:
        name, _, forced_id = (p.strip() for p in line.partition("|"))
        current_id = known.get(name, (None, None))[0]

        if forced_id.isdigit():
            if current_id == int(forced_id):
                bilan["cached"] += 1
            else:
                storage.set_artist(name, int(forced_id), name)
                known[name] = (int(forced_id), name)
                bilan["resolved"] += 1
            continue

        if current_id:
            bilan["cached"] += 1
            continue

        try:
            found = itunes.search_artist(name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Recherche de '%s' échouée : %s", name, exc)
            bilan["not_found"].append(f"{name} (erreur réseau)")
            continue

        if not found:
            bilan["not_found"].append(name)
            continue

        itunes_id, matched, genre, ambiguous = found
        storage.set_artist(name, itunes_id, matched)
        known[name] = (itunes_id, matched)
        bilan["resolved"] += 1

        reasons = []
        if matched.casefold() != name.casefold():
            reasons.append("nom différent")
        if ambiguous:
            reasons.append("plusieurs artistes portent ce nom")
        if not any(hint in genre.casefold() for hint in config.EXPECTED_GENRE_HINTS):
            reasons.append("genre inattendu")
        if reasons:
            bilan["to_check"].append(f"{name} → {matched} [{genre}] (id {itunes_id}) : {', '.join(reasons)}")
    return bilan


# --------------------------------------------------------------------------- #
# Ajout depuis Telegram (/ajouter)
# --------------------------------------------------------------------------- #

_URL_ID_RE = re.compile(r"music\.apple\.com/\S*?/artist/(?:[^/\s?#]+/)?(\d+)")


def prepare_add(query: str) -> dict:
    """
    Interprète `/ajouter ...` et dit quoi faire :
      {"kind": "exists", "name"}                       déjà dans le fichier
      {"kind": "ready", "name", "id", "matched", "genre", "url", "forced"}
      {"kind": "choose", "name", "candidates": [{id,name,genre,url,last}]}
      {"kind": "none"}                                 rien trouvé
    Formes acceptées : `Nom`, `Nom | id`, ou l'URL d'une page artiste Apple Music.
    """
    query = query.strip()
    forced_id: int | None = None
    name = query

    m = _URL_ID_RE.search(query)
    if m:
        forced_id, name = int(m.group(1)), ""
    elif "|" in query:
        left, _, right = query.partition("|")
        if right.strip().isdigit():
            forced_id, name = int(right.strip()), left.strip()

    if forced_id is not None:
        info = itunes.lookup_artist(forced_id)
        if not info:
            return {"kind": "none"}
        name = name or info["name"]
        existing = in_file(name)
        if existing:
            return {"kind": "exists", "name": existing}
        return {
            "kind": "ready", "name": name, "id": forced_id, "matched": info["name"],
            "genre": info["genre"], "url": info["url"], "forced": True,
        }

    if not name:
        return {"kind": "none"}
    existing = in_file(name)
    if existing:
        return {"kind": "exists", "name": existing}

    cands = itunes.search_artists(name)
    if not cands:
        return {"kind": "none"}
    key = text.cmp_key(name)
    exact = [c for c in cands if text.cmp_key(c["name"]) == key]
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
            last = itunes.recent_releases(c["id"], 1)
            c["last"] = f"{last[0].title} ({last[0].release_date:%d/%m/%Y})" if last else "aucune sortie"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Aperçu de %s impossible : %s", c["name"], exc)
            c["last"] = "?"
    return {"kind": "choose", "name": name, "candidates": shortlist}


# --------------------------------------------------------------------------- #
# Homonymes (/verifier)
# --------------------------------------------------------------------------- #

def ambiguous() -> list[dict]:
    """
    Artistes suivis dont le nom est porté par plusieurs artistes chez Apple et
    que tu n'as pas encore confirmés. Les lignes `Nom | id` de artists.txt et
    les artistes ajoutés par lien ou par choix d'homonyme comptent comme confirmés.
    """
    forced = {ln.partition("|")[0].strip() for ln in read_lines() if ln.partition("|")[2].strip().isdigit()}
    out: list[dict] = []
    seen_ids: set[int] = set()
    for name, aid, matched in storage.unverified_artists():
        if aid in seen_ids or name in forced:
            continue
        seen_ids.add(aid)
        try:
            if itunes.name_is_unique(aid, matched or name):
                continue
            overview = itunes.artist_overview(aid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vérification de %s impossible : %s", name, exc)
            continue
        out.append({**overview, "followed": name})
    return out


def homonyms(artist_id: int) -> list[dict]:
    """Les autres artistes du même nom chez Apple, avec leur dernière sortie."""
    name = storage.followed_name(artist_id) or ""
    key = text.cmp_key(name)
    cands = [c for c in itunes.search_artists(name, limit=6) if c["id"] != artist_id and text.cmp_key(c["name"]) == key]
    out = []
    for c in cands[:4]:
        try:
            out.append({**itunes.artist_overview(c["id"]), "id": c["id"]})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Aperçu de %s impossible : %s", c["name"], exc)
    return out


def switch(old_id: int, new_id: int, new_name: str) -> str | None:
    """
    Remplace l'artiste suivi par son homonyme : `Nom | nouvel_id` dans
    artists.txt, et les sorties non triées de l'ancien quittent /trier.
    """
    name = storage.followed_name(old_id)
    if name is None:
        return None
    storage.delete_artists([name])
    storage.drop_pending_of(old_id)
    storage.forget_artist_caches(old_id)
    add(name, new_id, new_name, forced=True)
    return name
