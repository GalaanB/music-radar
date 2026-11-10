"""
Mesure, pour les artistes dont le catalogue Apple atteint le plafond de 200
fiches, si des sorties récentes échappent au scan (lecture seule).

    python tools/measure_capped_artists.py [jours] [rapport.json]

Trois sources sont recoupées sur la période (90 jours par défaut) :
  1. les 200 fiches du catalogue (ce que le bot voit aujourd'hui) ;
  2. les titres récents de l'artiste chez Apple (lookup entity=song) : albums
     datés dans la période et absents des 200 fiches ;
  3. les sorties de l'artiste connues de MusicBrainz dans la période : celles
     dont le titre n'est dans aucune des 200 fiches, recherchées chez Apple.

N'envoie rien sur Telegram et ne modifie rien dans le bot. La base utilisée est
celle de RADAR_DATA_DIR (ID des artistes, cache des identifiants MusicBrainz).
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musicradar import artists, clock, config, storage, text
from musicradar.sources import itunes, musicbrainz

MAX_APPLE_SEARCHES = 8   # par artiste, pour les sorties MusicBrainz introuvables dans les 200 fiches


def album_blocks(ids: list[int]) -> dict[int, list[dict]]:
    """Fiches « collection » brutes de plusieurs artistes (un bloc par artiste)."""
    data = itunes.get_json(config.ITUNES_LOOKUP, {
        "id": ",".join(map(str, ids)), "entity": "album", "sort": "recent",
        "limit": config.ALBUMS_PER_ARTIST, "country": config.COUNTRY,
    })
    out: dict[int, list[dict]] = {}
    current: list[dict] | None = None
    for item in data.get("results", []):
        if item.get("wrapperType") == "artist":
            current = out.setdefault(item.get("artistId"), []) if item.get("artistId") in ids else None
        elif item.get("wrapperType") == "collection" and current is not None:
            current.append(item)
    return out


def day(item: dict) -> str:
    return (item.get("releaseDate") or "")[:10]


def title_key(collection_name: str) -> str:
    return text.norm_title(text.kind_and_title(collection_name)[1])


def brief(item: dict) -> dict:
    return {"date": day(item), "id": item.get("collectionId"), "titre": item.get("collectionName"),
            "artiste": item.get("artistName")}


def from_recent_songs(aid: int, known_ids: set[int], since: str) -> dict:
    """Albums vus via les titres récents de l'artiste, datés dans la période, absents des 200 fiches."""
    data = itunes.get_json(config.ITUNES_LOOKUP, {
        "id": aid, "entity": "song", "sort": "recent", "limit": 200, "country": config.COUNTRY})
    tracks = [t for t in data.get("results", []) if t.get("wrapperType") == "track"]
    recent = [t for t in tracks if day(t) >= since]
    unknown = sorted({t["collectionId"] for t in recent if t.get("collectionId") and t["collectionId"] not in known_ids})
    own: list[dict] = []
    guest: list[dict] = []
    for start in range(0, len(unknown), 50):
        chunk = unknown[start : start + 50]
        looked = itunes.get_json(config.ITUNES_LOOKUP, {"id": ",".join(map(str, chunk)), "country": config.COUNTRY})
        for c in looked.get("results", []):
            if c.get("wrapperType") != "collection" or day(c) < since:
                continue
            (own if c.get("artistId") == aid else guest).append(brief(c))
    return {
        "titres_lus": len(tracks),
        "titres_dans_la_periode": len(recent),
        "plus_ancien_titre_lu": min((day(t) for t in tracks if day(t)), default=""),
        "albums_absents_credites_a_l_artiste": sorted(own, key=lambda x: x["date"], reverse=True),
        "albums_absents_ou_il_est_invite": sorted(guest, key=lambda x: x["date"], reverse=True),
    }


def from_musicbrainz(aid: int, name: str, known_ids: set[int], known_titles: set[str], since: str, until: str) -> dict:
    """Sorties de l'artiste datées dans la période d'après MusicBrainz, comparées aux 200 fiches."""
    mbid = musicbrainz.mbid_for(aid, name)
    if not mbid:
        return {"mbid": None}
    data = musicbrainz.get("release", {"query": f"arid:{mbid} AND date:[{since} TO {until}]", "limit": 100})
    titles: dict[str, dict] = {}
    for rel in data.get("releases", []):
        credits = rel.get("artist-credit", [])
        if not credits or credits[0].get("artist", {}).get("id") != mbid:
            continue  # il n'est qu'invité : hors sujet ici
        key = text.norm_title(rel.get("title", ""))
        date = rel.get("date", "")
        if key and (key not in titles or date < titles[key]["date"]):
            titles[key] = {"titre": rel.get("title"), "date": date}
    missing = [v for k, v in titles.items() if k not in known_titles]
    checked: list[dict] = []
    for m in missing[:MAX_APPLE_SEARCHES]:
        want = text.norm_title(m["titre"])
        found = itunes.get_json(config.ITUNES_SEARCH, {
            "term": f"{name} {m['titre']}", "entity": "album", "limit": 15, "country": config.COUNTRY})
        hits = [c for c in found.get("results", []) if c.get("wrapperType") == "collection"
                and title_key(c.get("collectionName", "")) == want and c.get("artistId") == aid]
        if not hits:
            verdict = "introuvable chez Apple sous ce titre"
        elif any(c["collectionId"] in known_ids for c in hits):
            verdict = "présente dans les 200 fiches sous un titre voisin"
        elif any(day(c) >= since for c in hits):
            verdict = "RATÉE : chez Apple, dans la période, hors des 200 fiches"
        else:
            verdict = "ancienne sortie chez Apple (réédition côté MusicBrainz)"
        checked.append({**m, "verdict": verdict, "apple": [brief(c) for c in hits[:3]]})
    return {
        "mbid": mbid,
        "sorties_dans_la_periode": len(titles),
        "titres_absents_des_200_fiches": len(missing),
        "verifiees_chez_apple": checked,
        "non_verifiees": [m["titre"] for m in missing[MAX_APPLE_SEARCHES:]],
    }


def main(argv: list[str]) -> None:
    days = int(argv[1]) if len(argv) > 1 else 90
    report_path = Path(argv[2]) if len(argv) > 2 else None
    since = (clock.today() - timedelta(days=days)).isoformat()
    until = clock.today().isoformat()

    names = storage.followed_artists()
    if not names:
        print("Base vide : résolution de artists.txt…", flush=True)
        artists.resolve()
        names = storage.followed_artists()
    ids = list(names)

    capped: dict[int, list[dict]] = {}
    for start in range(0, len(ids), 5):
        chunk = ids[start : start + 5]
        for aid, block in album_blocks(chunk).items():
            if len(block) >= config.ALBUMS_PER_ARTIST:
                capped[aid] = block
    print(f"{len(ids)} artistes, {len(capped)} au plafond : {', '.join(names[a] for a in capped)}", flush=True)

    report: dict = {"jours": days, "depuis": since, "artistes": {}}
    for aid, block in capped.items():
        name = names[aid]
        known_ids = {c["collectionId"] for c in block}
        known_titles = {title_key(c.get("collectionName", "")) for c in block}
        entry: dict = {
            "fiches_dans_la_periode": sum(1 for c in block if day(c) >= since),
            "fiche_la_plus_ancienne": min(day(c) for c in block),
        }
        try:
            entry["titres_recents_apple"] = from_recent_songs(aid, known_ids, since)
        except Exception as exc:  # noqa: BLE001
            entry["titres_recents_apple"] = {"erreur": str(exc)}
        try:
            entry["musicbrainz"] = from_musicbrainz(aid, name, known_ids, known_titles, since, until)
        except Exception as exc:  # noqa: BLE001
            entry["musicbrainz"] = {"erreur": str(exc)}
        report["artistes"][name] = entry
        songs, mb = entry["titres_recents_apple"], entry["musicbrainz"]
        print(
            f"{name} : {entry['fiches_dans_la_periode']} fiches récentes sur 200"
            f" · via titres : {len(songs.get('albums_absents_credites_a_l_artiste', []))} à lui,"
            f" {len(songs.get('albums_absents_ou_il_est_invite', []))} en invité"
            f" · MusicBrainz : {mb.get('titres_absents_des_200_fiches', '—')} titre(s) hors des 200",
            flush=True,
        )

    if report_path:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nRAPPORT", json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
