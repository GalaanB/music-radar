"""
Compare le catalogue Apple obtenu artiste par artiste (comme le scan actuel)
avec celui d'une requête groupée `lookup?id=A,B,C…` (lecture seule).

    python tools/compare_batch_lookup.py [taille_du_paquet] [rapport.json]

N'envoie rien sur Telegram et ne modifie pas la base du bot : les ID sont lus
dans la base (RADAR_DATA_DIR) si elle contient des artistes, sinon résolus
depuis artists.txt. Le rapport dit, pour chaque artiste, si les deux modes
renvoient exactement les mêmes fiches, dans le même ordre.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musicradar import artists, config, storage

DELAY_S = 3.2


def lookup(ids: list[int]) -> tuple[list[dict], float, int]:
    """(résultats, durée en s, taille en octets) d'un lookup d'un ou plusieurs artistes."""
    params = {
        "id": ",".join(map(str, ids)),
        "entity": "album",
        "sort": "recent",
        "limit": config.ALBUMS_PER_ARTIST,
        "country": config.COUNTRY,
    }
    for attempt in (1, 2):
        time.sleep(DELAY_S)
        start = time.monotonic()
        resp = requests.get(config.ITUNES_LOOKUP, params=params, timeout=60)
        took = time.monotonic() - start
        if resp.status_code in (403, 429) and attempt == 1:
            print(f"  HTTP {resp.status_code} pour {ids} : pause de 60 s", flush=True)
            time.sleep(60)
            continue
        resp.raise_for_status()
        return resp.json().get("results", []), took, len(resp.content)
    return [], 0.0, 0


def blocks(results: list[dict]) -> dict[int, list[dict]]:
    """Découpe une réponse groupée : chaque fiche « artist » ouvre le bloc de ses albums."""
    out: dict[int, list[dict]] = {}
    current: list[dict] | None = None
    for item in results:
        if item.get("wrapperType") == "artist":
            current = out.setdefault(item.get("artistId"), [])
        elif item.get("wrapperType") == "collection" and current is not None:
            current.append(item)
    return out


def main(argv: list[str]) -> None:
    size = int(argv[1]) if len(argv) > 1 else 5
    report_path = Path(argv[2]) if len(argv) > 2 else None

    names = storage.followed_artists()
    if not names:
        print("Base vide : résolution de artists.txt…", flush=True)
        bilan = artists.resolve()
        print(f"  {bilan['resolved']} résolus, introuvables : {bilan['not_found']}", flush=True)
        names = storage.followed_artists()
    ids = list(names)
    print(f"{len(ids)} artistes, paquets de {size}", flush=True)

    report: dict = {"batch_size": size, "artists": len(ids), "identical": 0, "capped_at_200": [], "problems": [], "batches": []}
    for start in range(0, len(ids), size):
        chunk = ids[start : start + size]
        try:
            grouped, took, nbytes = lookup(chunk)
        except Exception as exc:  # noqa: BLE001
            report["problems"].append({"batch": chunk, "error": f"groupé : {exc}"})
            print(f"paquet {chunk} : ÉCHEC {exc}", flush=True)
            continue
        by_artist = blocks(grouped)
        report["batches"].append({"ids": chunk, "seconds": round(took, 2), "bytes": nbytes})
        for aid in chunk:
            try:
                single = [r for r in lookup([aid])[0] if r.get("wrapperType") == "collection"]
            except Exception as exc:  # noqa: BLE001
                report["problems"].append({"artist": aid, "name": names[aid], "error": f"unitaire : {exc}"})
                continue
            if len(single) >= config.ALBUMS_PER_ARTIST:
                report["capped_at_200"].append(names[aid])  # catalogue tronqué par Apple
            got = by_artist.get(aid)
            if got == single:
                report["identical"] += 1
                continue
            problem: dict = {"artist": aid, "name": names[aid], "single": len(single)}
            if got is None:
                problem["kind"] = "aucun bloc dans la réponse groupée"
            else:
                a = {r.get("collectionId") for r in single}
                b = {r.get("collectionId") for r in got}
                problem["grouped"] = len(got)
                if a != b:
                    problem["kind"] = "fiches différentes"
                    problem["only_single"] = sorted(a - b)
                    problem["only_grouped"] = sorted(b - a)
                elif [r.get("collectionId") for r in single] != [r.get("collectionId") for r in got]:
                    problem["kind"] = "mêmes fiches, ordre différent"
                else:
                    fields = sorted({k for x, y in zip(single, got, strict=True) for k in x.keys() | y.keys() if x.get(k) != y.get(k)})
                    problem["kind"] = "mêmes fiches, champs différents"
                    problem["fields"] = fields
            report["problems"].append(problem)
        done = min(start + size, len(ids))
        print(f"{done}/{len(ids)} · identiques {report['identical']} · écarts {len(report['problems'])}"
              f" · paquet {took:.1f} s, {nbytes // 1024} Ko", flush=True)

    if report["batches"]:
        report["slowest_batch_s"] = max(b["seconds"] for b in report["batches"])
        report["largest_batch_kb"] = max(b["bytes"] for b in report["batches"]) // 1024
    if report_path:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nBILAN", json.dumps({k: v for k, v in report.items() if k != "batches"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
