"""
Client MusicBrainz (base musicale ouverte, https://musicbrainz.org).

Chaque artiste crédité sur un enregistrement y est relié par un identifiant
(MBID), pas par son nom : c'est ce qui permet de trouver les featurings sans
confondre les homonymes. Limite d'usage : 1 requête par seconde.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date

import requests

from .. import config, storage, text
from . import itunes

logger = logging.getLogger("radar.musicbrainz")

_APPLE_ID_IN_URL_RE = re.compile(r"(?:music|itunes)\.apple\.com/.*?/(?:id)?(\d+)(?:[/?#]|$)")


def get(path: str, params: dict) -> dict:
    """GET MusicBrainz (JSON), en respectant 1 requête/s ; 1 retry si saturé (503)."""
    for attempt in (1, 2):
        time.sleep(config.MB_DELAY_S)
        resp = requests.get(
            f"{config.MB_API}/{path}",
            params={**params, "fmt": "json"},
            headers=config.MB_HEADERS,
            timeout=config.HTTP_TIMEOUT_S,
        )
        if resp.status_code == 503 and attempt == 1:
            time.sleep(5)
            continue
        resp.raise_for_status()
        return resp.json()
    return {}


def _lucene(value: str) -> str:
    return '"' + value.replace("\\", " ").replace('"', " ") + '"'


def mbid_for(artist_id: int, name: str) -> str | None:
    """
    Identifiant MusicBrainz de l'artiste suivi. On prend la fiche qui renvoie
    vers sa page Apple Music (même ID) : pas de confusion entre homonymes.
    Sinon, un seul artiste de ce nom des deux côtés. Mis en cache (réessai
    après 30 jours si introuvable).
    """
    found, mbid = storage.cached_mbid(artist_id)
    if found:
        return mbid

    data = get("artist", {"query": f"artist:{_lucene(name)}", "limit": 5})
    key = text.cmp_key(name)
    exact = [a for a in data.get("artists", []) if text.cmp_key(a.get("name", "")) == key]
    mbid = None
    for cand in exact[:3]:
        details = get(f"artist/{cand['id']}", {"inc": "url-rels"})
        apple_ids = {
            int(m.group(1))
            for rel in details.get("relations", [])
            if (m := _APPLE_ID_IN_URL_RE.search(rel.get("url", {}).get("resource", "")))
        }
        if artist_id in apple_ids:
            mbid = cand["id"]
            break
    if mbid is None and len(exact) == 1 and itunes.name_is_unique(artist_id, name):
        mbid = exact[0]["id"]
    storage.save_mbid(artist_id, mbid)
    return mbid


def recordings_crediting(mbid: str, since: date, until: date, max_results: int = 300) -> list[dict]:
    """
    Enregistrements où l'artiste est crédité, publiés (même sur une compilation)
    entre `since` et `until`. À filtrer ensuite sur `first-release-date`.
    """
    query = f"arid:{mbid} AND date:[{since.isoformat()} TO {until.isoformat()}]"
    out: list[dict] = []
    while True:
        data = get("recording", {"query": query, "limit": 100, "offset": len(out)})
        page = data.get("recordings", [])
        out += page
        if not page or len(out) >= int(data.get("count", 0)) or len(out) >= max_results:
            return out
