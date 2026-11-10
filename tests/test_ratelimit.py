"""Limiteur de débit : délai minimal entre deux appels, sans attente inutile."""

import threading
import time

import requests

from conftest import collection
from musicradar import config, scan, storage
from musicradar.ratelimit import RateLimiter
from musicradar.sources import itunes as itunes_api
from musicradar.sources import musicbrainz

REAL_GET_JSON = itunes_api.get_json
REAL_MB_GET = musicbrainz.get


class FakeTime:
    """Horloge simulée : dormir avance l'heure."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def limiter(t: FakeTime, delay: float = 3.2) -> RateLimiter:
    return RateLimiter(lambda: delay, clock=t.clock, sleep=t.sleep)


def test_first_call_does_not_wait_and_the_next_waits_the_full_delay():
    t = FakeTime()
    lim = limiter(t)
    lim.wait()
    lim.wait()
    assert t.slept == [3.2]


def test_only_the_remaining_time_is_waited():
    t = FakeTime()
    lim = limiter(t)
    lim.wait()
    t.now += 1.2   # durée de la requête + travail entre deux appels
    lim.wait()
    t.now += 5.0   # plus long que le délai : aucun temps perdu
    lim.wait()
    assert t.slept == [2.0]


def test_calls_from_several_threads_are_spaced_too():
    t = FakeTime()
    lim = limiter(t, delay=1.0)
    starts: list[float] = []

    def call() -> None:
        lim.wait()
        starts.append(t.now)

    threads = [threading.Thread(target=call) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(starts) == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_delay_is_read_at_each_call():
    t = FakeTime()
    delays = [1.0, 4.0]
    lim = RateLimiter(lambda: delays[0], clock=t.clock, sleep=t.sleep)
    lim.wait()
    delays[0] = 4.0   # ne s'applique qu'au créneau suivant
    lim.wait()
    lim.wait()
    assert t.slept == [1.0, 4.0]


class Counter:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1


class Ok:
    status_code = 200

    def json(self):
        return {"results": []}

    def raise_for_status(self):
        return None


def test_every_apple_and_musicbrainz_attempt_goes_through_its_limiter(monkeypatch):
    apple, mb = Counter(), Counter()
    monkeypatch.setattr(itunes_api, "_limiter", apple)
    monkeypatch.setattr(musicbrainz, "_limiter", mb)
    answers = [requests.ConnectionError("coupure"), Ok(), Ok()]

    def fake_get(url, **kwargs):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(requests, "get", fake_get)
    REAL_GET_JSON(config.ITUNES_LOOKUP, {"id": 1})   # 1 échec + 1 nouvel essai
    REAL_MB_GET("artist", {})
    assert (apple.waits, mb.waits) == (2, 1)


def test_scan_itself_never_sleeps(itunes, monkeypatch):
    conn = storage.connect()
    conn.executemany("INSERT INTO artists VALUES (?, ?, ?)", [(f"A{i}", i, f"A{i}") for i in range(1, 6)])
    conn.commit()
    conn.close()
    itunes.albums = {1: [collection(10, "Neuf - Single", artist="A1")]}
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    assert [r.album_id for r in scan.daily_batch().releases] == [10]
    assert slept == []   # l'attente est faite par get_json (remplacé ici), nulle part ailleurs
