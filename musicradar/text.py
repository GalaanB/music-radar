"""
Traitement du texte : comparer des noms d'artistes et des titres malgré les
accents, la casse, la ponctuation et les mentions de featuring ; reconnaître
les éditions (deluxe…), les versions alternatives (slowed, remix…) et les
artistes crédités sur un titre. Fonctions pures, sans dépendance.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# --------------------------------------------------------------------------- #
# Noms
# --------------------------------------------------------------------------- #

def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def sort_key(line: str) -> str:
    """Ordre alphabétique insensible à la casse et aux accents (comme artists.txt)."""
    return strip_accents(line.partition("|")[0].strip()).casefold()


def cmp_key(name: str) -> str:
    """Clé de comparaison de noms : 'Tyler The Creator' == 'Tyler, The Creator'."""
    plain = strip_accents(name).casefold()
    alnum = re.sub(r"[^0-9a-z]+", "", plain)
    return alnum or plain.strip()


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n <= 1 else many}"


# --------------------------------------------------------------------------- #
# Titres
# --------------------------------------------------------------------------- #

_FEAT_PAREN_RE = re.compile(r"[\(\[]\s*(?:feat|ft|featuring|with|prod|produced)\b[^\)\]]*[\)\]]", re.I)
_NOISE_PAREN_RE = re.compile(r"[\(\[]\s*(?:explicit|clean)\s*[\)\]]", re.I)
_FEAT_TAIL_RE = re.compile(r"\s+(?:feat|ft|featuring)\.?\s.*$", re.I)


def norm_title(title: str) -> str:
    """
    Titre normalisé pour comparer 'Song (feat. X)' et 'Song'. On ne retire que
    les mentions de featuring/explicit : 'Song (Remix)' reste différent de 'Song'.
    """
    t = strip_accents(title).casefold()
    t = _FEAT_PAREN_RE.sub(" ", t)
    t = _NOISE_PAREN_RE.sub(" ", t)
    t = _FEAT_TAIL_RE.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_EP_TAIL_RE = re.compile(r"(?:\s|[\(\[])E\.?P\.?[\)\]]?$", re.I)


def kind_and_title(collection_name: str) -> tuple[str, str]:
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


# --------------------------------------------------------------------------- #
# Éditions (deluxe, expanded…)
# --------------------------------------------------------------------------- #

_EDITION_WORDS = (
    r"(?:deluxe|expanded|extended|anniversary|edition|édition|reissue|re-issue|"
    r"remaster(?:ed)?|complete|platinum|collector'?s?|special|bonus)"
)
_EDITION_PAREN_RE = re.compile(rf"\s*[\(\[][^\)\]]*\b{_EDITION_WORDS}\b[^\)\]]*[\)\]]\s*$", re.I)
_EDITION_DASH_RE = re.compile(rf"\s+[-–—]\s+[^-–—]*\b{_EDITION_WORDS}\b[^-–—]*$", re.I)


def split_edition(title: str) -> tuple[str, bool]:
    """'Album (Deluxe)' -> ('Album', True). Retire les suffixes d'édition en chaîne."""
    base, found = title, False
    while True:
        new = _EDITION_DASH_RE.sub("", _EDITION_PAREN_RE.sub("", base))
        if new == base:
            break
        base, found = new, True
    return base.strip(), found


# --------------------------------------------------------------------------- #
# Versions alternatives (slowed, sped up, instru, remix…)
# --------------------------------------------------------------------------- #

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


def is_variant(title: str) -> bool:
    """Slowed, sped up, instru… n'importe où ; remix, live, version… entre parenthèses ou après un tiret."""
    if _VARIANT_STRONG_RE.search(title):
        return True
    for m in _EXTRAS_RE.finditer(title):
        extra = m.group(1) or m.group(2) or ""
        if _VARIANT_RE.search(extra):
            return True
    return False


def variant_label(title: str) -> str:
    for rx, label in _VARIANT_LABELS:
        if rx.search(title):
            return label
    return "alternative"


def variant_base(title: str) -> str:
    """'QRÖMELIFE (Slowed + Reverb)' -> 'QRÖMELIFE' ; 'Song - Sped Up' -> 'Song'."""
    def drop_if_variant(m: re.Match) -> str:
        part = m.group(0)
        return "" if (_VARIANT_RE.search(part) or _VARIANT_STRONG_RE.search(part)) else part

    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", drop_if_variant, title)
    t = re.sub(r"\s[-–—]\s.*$", drop_if_variant, t)
    t = _VARIANT_STRONG_RE.sub(" ", t)
    return re.sub(r"[\s+&,-]+$", "", t).strip() or title


# --------------------------------------------------------------------------- #
# Crédits d'un titre (interprète + featurings)
# --------------------------------------------------------------------------- #

_FEAT_CREDITS_RE = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]", re.I)
_FEAT_TAIL_CREDITS_RE = re.compile(r"\s(?:feat\.?|ft\.?|featuring)\s+(.+)$", re.I)
_CREDIT_SPLIT_RE = re.compile(r"\s*(?:,|&|/|\+|\bx\b|\bet\b|\band\b)\s*", re.I)


def credited(track_name: str, artist_name: str) -> set[str]:
    """Clés de tous les artistes crédités sur un titre (interprète + feat.)."""
    parts = [artist_name]
    parts += _FEAT_CREDITS_RE.findall(track_name)
    parts += _FEAT_TAIL_CREDITS_RE.findall(_FEAT_CREDITS_RE.sub("", track_name))
    names: set[str] = set()
    for part in parts:
        names.add(cmp_key(part))  # crédit entier : « Tyler, The Creator », « PUSH & GIO »
        for name in _CREDIT_SPLIT_RE.split(part):
            if name.strip():
                names.add(cmp_key(name))
    return names


TitleIndex = dict[str, list[set[str]]]


def title_index(pairs: Iterable[tuple[str, str]]) -> TitleIndex:
    """Titre normalisé -> artistes crédités, à partir de couples (titre, interprète)."""
    index: TitleIndex = {}
    for title, artist in pairs:
        index.setdefault(norm_title(title), []).append(credited(title, artist))
    return index


def already_out(index: TitleIndex, artist: str, title: str) -> bool:
    """
    Le titre est-il déjà dans l'index, crédité à au moins un de ces artistes ?
    « Run The World » de Loe Shimmy est déjà connu comme single de
    « Loe Shimmy & Brent Faiyaz » : pas besoin d'une carte « Featuring » en plus.
    """
    keys = credited("", artist)
    return any(keys & creds for creds in index.get(norm_title(title), []))
