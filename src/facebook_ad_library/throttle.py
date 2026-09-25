"""Whether Meta is currently withholding the ad payload from this address.

While it lasts the throttle is total, not sampled: measured 24 Sep 2026, 24 of the 24 searches in
one sourcing cycle that had ads to give came back with a correct total and an empty list. Checking
each one on the direct path first therefore buys nothing - it costs ~3 s and ~1 MB of bandwidth to
be told what the previous search already established - so a recent observation suppresses that
check and the search goes straight to the proxied path.

This deliberately EXPIRES rather than latching. While it is set every search pays the proxy, so
the cost of being wrong is real money; one direct GET every `throttle_memory_s` is what it costs
to notice Meta has stopped. That is why the window is minutes and not hours, and why any direct
page that does carry ads clears it at once.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .config import settings


class Throttle:
    """One observation with an expiry, shared across requests."""

    def __init__(self, ttl_s: float | None = None, clock: Callable[[], float] = time.time) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._seen_at: float | None = None
        self.observations = 0
        self.skipped = 0

    @property
    def ttl(self) -> float:
        return settings.throttle_memory_s if self._ttl is None else self._ttl

    def seen(self) -> None:
        """A page arrived with a total above zero and no ads."""
        with self._lock:
            self._seen_at = self._clock()
            self.observations += 1

    def clear(self) -> None:
        """A direct page carried ads, so whatever was happening has stopped."""
        with self._lock:
            self._seen_at = None

    def active(self) -> bool:
        """True while a recent observation stands. Expiry is checked here rather than on a timer,
        so nothing has to run in the background for the direct path to be re-probed."""
        ttl = self.ttl
        if ttl <= 0:
            return False
        with self._lock:
            if self._seen_at is None:
                return False
            if self._clock() - self._seen_at >= ttl:
                self._seen_at = None
                return False
            self.skipped += 1
            return True

    def snapshot(self) -> dict:
        with self._lock:
            since = None if self._seen_at is None else round(self._clock() - self._seen_at, 1)
        return {
            "active": since is not None and since < self.ttl,
            "seconds_since_seen": since,
            "ttl_s": self.ttl,
            "observations": self.observations,
            "direct_gets_skipped": self.skipped,
        }


# Until 0.4.0 one `throttle` lived here for the whole process, because there was one address.
# A withheld page is a fact about one exit IP, so each lane now holds its own Throttle.
