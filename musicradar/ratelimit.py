"""
Limiteur de débit : garantit un délai minimal entre deux appels à un même
service, quel que soit le thread qui appelle.

Contrairement à une pause fixe avant chaque appel, on n'attend que ce qu'il
reste du délai : la durée de la requête précédente et le temps passé ailleurs
(base, autre service) ne s'ajoutent plus à l'attente.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        delay: Callable[[], float],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._delay = delay    # lu à chaque appel : le réglage peut changer (tests)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._ready_at = float("-inf")

    def wait(self) -> None:
        """Bloque jusqu'à ce que le prochain appel soit permis, puis réserve le créneau."""
        with self._lock:  # gardé pendant l'attente : les autres threads font la queue
            pause = self._ready_at - self._clock()
            if pause > 0:
                (self._sleep or time.sleep)(pause)
            self._ready_at = self._clock() + self._delay()
