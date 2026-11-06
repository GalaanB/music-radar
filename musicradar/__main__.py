"""
Outil de diagnostic en ligne de commande (lecture seule, aucune notification) :

    python -m musicradar resolve        # résout artists.txt -> IDs Apple Music
    python -m musicradar daily          # sorties que le récap quotidien enverrait
    python -m musicradar weekly         # idem pour le vendredi, avec les sorties à venir
    python -m musicradar catchup 30     # sorties des 30 derniers jours
"""

from __future__ import annotations

import json
import logging
import sys

from . import artists, formatting, scan


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO)
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "resolve":
        print(json.dumps(artists.resolve(), indent=2, ensure_ascii=False))
    elif cmd == "daily":
        print("\n".join(formatting.compact_list("Nouvelles sorties", scan.daily_batch().releases, "Rien de nouveau.")))
    elif cmd == "weekly":
        b = scan.weekly_batch()
        print("\n".join(formatting.compact_list("Récap du vendredi", b.releases, "Rien de nouveau.")))
        print("\n".join(formatting.upcoming(b.upcoming)))
    elif cmd == "catchup":
        n = int(argv[2]) if len(argv) > 2 else 30
        print("\n".join(formatting.compact_list(f"Rattrapage ({n} jours)", scan.catchup_batch(n).releases, "Rien trouvé.")))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
