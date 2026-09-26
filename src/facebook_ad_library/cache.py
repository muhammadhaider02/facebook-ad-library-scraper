"""A small in-memory cache with a TTL.

The sourcing workflow re-searches an exhausted keyword's remaining country slots and retries a pair on error,
so the same (query, country) arrives more than once a day. Each hit saves a page GET of about
1 MB. The whole page is stored and `maxItems` applied on the way out, because one GET answers
every size up to the page's 30. Empty results get a shorter life because a keyword with no ads
today may have some tomorrow. Brand lookups keep a second, shorter-lived instance (the three
calls the research workflow makes per brand share one resolution and one page view).
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

SWEEP_EVERY_S = 30.0

class TTLCache:
    def __init__(
        self,
        ttl_s: float,
        empty_ttl_s: float | None = None,
        max_entries: int = 2000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_s = float(ttl_s)
        self.empty_ttl_s = float(empty_ttl_s if empty_ttl_s is not None else ttl_s)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._items: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expired_dropped = 0
        self._last_sweep = self._clock()

    @staticmethod
    def key(query: str, country: str, active_status: str = "active") -> tuple:
        return (str(query or "").strip().casefold(), str(country or "").strip().upper(), str(active_status or "active").strip().lower())

    def get(self, key: tuple) -> Any | None:
        """The stored value (a copy, so a caller cannot edit the cache), or None. A stored empty
        value (`[]`, `{}`, `""`) comes back as that empty value, not None."""
        now = self._clock()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._items[key]
                self.misses += 1
                return None
            self.hits += 1
            return copy.deepcopy(value)

    def put(self, key: tuple, value: Any, ttl: float | None = None) -> None:
        """Store `value` for `ttl` seconds: the empty TTL when it is empty, the full one otherwise,
        unless `ttl` says."""
        if ttl is None:
            ttl = self.empty_ttl_s if not value else self.ttl_s
        now = self._clock()
        with self._lock:
            self._items[key] = (now + float(ttl), copy.deepcopy(value))
            # Expired entries used to leave only when their own key was read again, and the sourcing
            # workflow never reads a key twice: 2,000 searches and 2,000 counts of dead ads (over 1 GB) OOM-killed the sourcing
            # container on 26 Sep 2026. Sweep them on a put, at most every SWEEP_EVERY_S.
            if now - self._last_sweep >= SWEEP_EVERY_S:
                self._last_sweep = now
                for k in [k for k, (exp, _) in self._items.items() if exp <= now]:
                    del self._items[k]
                    self.expired_dropped += 1
            if len(self._items) > self.max_entries:
                # Drop expired entries first, then the ones expiring soonest.
                expired = [k for k, (exp, _) in self._items.items() if exp <= now]
                for k in expired:
                    del self._items[k]
                while len(self._items) > self.max_entries:
                    oldest = min(self._items, key=lambda k: self._items[k][0])
                    del self._items[oldest]
                    self.evictions += 1

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._items), "hits": self.hits, "misses": self.misses, "evictions": self.evictions, "expired_dropped": self.expired_dropped}

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.hits = self.misses = self.evictions = 0
