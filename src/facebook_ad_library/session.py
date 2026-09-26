"""One fake browser session against the Ad Library, and the pool that hands them out.

Modelled on a mobile-device pool (Device / _DevicePool). The unit of identity is a session,
not a request: one curl_cffi cookie jar (`datr`, `rd_challenge`), created empty and filled by
the first GET, which is the one Meta challenges. A session is used for many searches and retired
at a request count, an age, the first hard failure, or a run of pages without results; a
retired session is never handed out again.

The transport is a tiny protocol so the whole lifecycle runs offline in tests against scripted
responses. The real one wraps curl_cffi with Chrome TLS impersonation, which is load-bearing:
measured 2026-09-19, a non-Chrome TLS client clears the challenge and is then served a 400 error
page on every request after it.
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol

from . import scraper as wire
from .config import settings
from .proxy import proxy_url

log = logging.getLogger(__name__)

counters = {
    "sessions_minted": 0,
    "sessions_retired": 0,
    "retired_by_reason": {},
    "challenges": 0,
    "calls": 0,
    "bytes": 0,
    "misses": 0,
    "rate_limited": 0,
    "session_dead": 0,
    "blocked": 0,
    "transient": 0,
    # Plain GETs: the page plugin and profile pages a brand lookup uses to resolve a vanity URL.
    # Counted apart so a refusal of those pages is visible separately from the Ad Library page.
    "plain_calls": 0,
    "plain_blocked": 0,
    "plain_dead": 0,
    "busy": 0,
}
_counters_lock = threading.Lock()


def _bump(name: str, by: int = 1) -> None:
    with _counters_lock:
        counters[name] += by


def _bump_reason(reason: str) -> None:
    with _counters_lock:
        counters["sessions_retired"] += 1
        counters["retired_by_reason"][reason] = counters["retired_by_reason"].get(reason, 0) + 1


def reset_counters() -> None:
    with _counters_lock:
        for k in counters:
            counters[k] = {} if k == "retired_by_reason" else 0


# --------------------------------------------------------------------------- transport


@dataclass
class Resp:
    status: int
    text: str
    headers: dict = field(default_factory=dict)


class Transport(Protocol):
    def get(self, url: str, headers: dict | None = None, timeout: float | None = None) -> Resp: ...
    def post(self, url: str, data: dict | None = None, headers: dict | None = None) -> Resp: ...
    def cookie_names(self) -> list[str]: ...


class CurlTransport:
    """curl_cffi with Chrome impersonation. One instance = one cookie jar."""

    def __init__(self, impersonate: str | None = None, timeout_s: float | None = None, proxy: str | None = None) -> None:
        from curl_cffi import requests

        kwargs = {"impersonate": impersonate or settings.impersonate, "timeout": timeout_s or settings.request_timeout_s}
        if proxy:
            kwargs["proxy"] = proxy
        self._s = requests.Session(**kwargs)
        self._s.headers.update({"Accept-Language": "en-US,en;q=0.9"})

    def get(self, url: str, headers: dict | None = None, timeout: float | None = None) -> Resp:
        kwargs = {"timeout": timeout} if timeout else {}
        r = self._s.get(url, headers=headers or {}, **kwargs)
        return Resp(int(r.status_code), r.text, dict(r.headers))

    def post(self, url: str, data: dict | None = None, headers: dict | None = None) -> Resp:
        r = self._s.post(url, data=data or {}, headers=headers or {})
        return Resp(int(r.status_code), r.text, dict(r.headers))

    def cookie_names(self) -> list[str]:
        return sorted({c.name for c in self._s.cookies.jar})


def lane_transport(proxy: str | None) -> Transport:
    """A transport on one lane's exit. `None` is the direct address, allowed only when
    LANE_REQUIRE_PROXY is off (CI); production refuses it at startup (proxy.lane_proxies)."""
    return CurlTransport(proxy=proxy)


def default_transport() -> Transport:
    """The legacy direct transport (SCRAPER_PROXY or none). CLI probes and tests only."""
    return CurlTransport(proxy=proxy_url())


# --------------------------------------------------------------------------- pacing


class RateLimiter:
    """At most `per_minute` calls in any 60 s window, across every session in the process."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.per_minute = max(1, int(per_minute))
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(now)
                    return
                delay = 60.0 - (now - self._stamps[0])
            self._sleep(max(0.0, delay))

    def eta(self) -> float:
        """Seconds until the next call may go, 0 when a slot is free now. Lets a caller with a
        deadline refuse a GET it could not start in time instead of sleeping past it."""
        with self._lock:
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= 60.0:
                self._stamps.popleft()
            if len(self._stamps) < self.per_minute:
                return 0.0
            return max(0.0, 60.0 - (now - self._stamps[0]))


# --------------------------------------------------------------------------- session


class FbSession:
    """One fake browser: a cookie jar and its counters, paced. Nothing is fetched until the
    first search; that GET clears the challenge and fills the jar."""

    PAGE_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(
        self,
        transport: Transport,
        limiter: RateLimiter,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.limiter = limiter
        self._clock = clock
        self._sleep = sleep
        self.label = "fb-" + uuid.uuid4().hex[:6]
        self.requests_made = 0
        self.challenges = 0
        self.born = clock()
        self.last_call = 0.0
        self.retired = False
        self.retire_reason = ""
        self.miss_streak = 0
        # What the last fetch() saw, step by step, for the diag command and for log lines.
        self.trace: list[str] = []
        # The last page body, raw. The diag command saves it as a fixture source.
        self.last_text = ""
        _bump("sessions_minted")

    # ----------------------------------------------------------------- lifecycle

    @property
    def expired(self) -> bool:
        return (
            self.requests_made >= settings.session_max_requests
            or self._clock() - self.born >= settings.session_max_age_s
        )

    def retire(self, reason: str) -> None:
        if not self.retired:
            self.retired = True
            self.retire_reason = reason
            _bump_reason(reason)
            log.info("%s retired (%s) after %d call(s)", self.label, reason, self.requests_made)

    def _pace(self) -> None:
        """A human-looking gap between two requests on the same session."""
        if not self.last_call:
            return
        gap = random.uniform(settings.spacing_min_s, settings.spacing_max_s)
        wait = self.last_call + gap - self._clock()
        if wait > 0:
            self._sleep(wait)

    # ----------------------------------------------------------------- one search

    def fetch(self, query: str, country: str, active_status: str = "active") -> tuple[wire.Page, list[dict]]:
        """GET the search page for a keyword and read its embedded results. Clears the challenge
        when Meta raises one. Raises the typed error for anything that is not an Ad Library page."""
        page, ads, _ = self.fetch_url(wire.bootstrap_url(query, country, active_status))
        return page, ads

    def fetch_url(self, url: str, deadline: float | None = None) -> tuple[wire.Page, list[dict], str]:
        """GET one Ad Library page (a keyword search or a page view) and classify it. With a
        deadline, the GET's timeout is what is left of it and transient retries stop at it."""
        r = self._get(url, deadline, kind="page")
        page, ads = wire.classify_page(r.text)
        self.trace.append(f"page={page.value} ads={len(ads)}")
        if page is wire.Page.MISS:
            _bump("misses")
            self.miss_streak += 1
            if self.miss_streak >= settings.miss_streak_retire:
                self.retire("miss_streak")
        else:
            self.miss_streak = 0
        return page, ads, r.text

    def get_plain(self, url: str, deadline: float | None = None) -> Resp:
        """GET a facebook.com page that is not the Ad Library (the page plugin, a profile page),
        on this session's jar and pacing. No results classification: the caller reads the body."""
        return self._get(url, deadline, kind="plain")

    def _get(self, url: str, deadline: float | None, kind: str) -> Resp:
        if self.retired:
            raise wire.SessionDead(f"{self.label} is retired ({self.retire_reason})")
        self.trace = []
        delays = (1.0, 3.0)
        for attempt in range(len(delays) + 1):
            self._pace()
            self.limiter.wait()
            timeout = None
            if deadline is not None:
                timeout = max(1.0, min(settings.request_timeout_s, deadline - self._clock()))
            try:
                r = self._get_page(url, timeout)
            except wire.FacebookError:
                raise
            except Exception as e:  # noqa: BLE001 - anything from the HTTP stack is a vendor failure
                r = Resp(0, f"{type(e).__name__}: {e}")
                self.trace.append(f"GET failed: {r.text[:80]}")
            self.last_call = self._clock()
            self.last_text = r.text or ""
            self.requests_made += 1
            _bump("calls" if kind == "page" else "plain_calls")
            _bump("bytes", len(r.text or ""))
            if r.status == 0 or r.status >= 500:
                _bump("transient")
                fits = deadline is None or self._clock() + delays[attempt if attempt < len(delays) else -1] + 2 < deadline
                if attempt < len(delays) and fits:
                    log.warning("%s: transient failure (http %s), retrying in %.0fs", self.label, r.status or "-", delays[attempt])
                    self._sleep(delays[attempt])
                    continue
                raise wire.ScrapeFailed(f"{kind} GET failed after {attempt + 1} attempt(s): http {r.status or '-'} {r.text[:120]!r}")
            break

        blocked, dead = ("blocked", "session_dead") if kind == "page" else ("plain_blocked", "plain_dead")
        if r.status == 400:
            _bump(blocked)
            raise wire.ScrapeBlocked(
                f"the {kind} answered HTTP 400 error page: the TLS fingerprint was rejected "
                f"(is FB_IMPERSONATE={settings.impersonate!r} a current Chrome?)"
            )
        if r.status == 403:
            _bump(blocked)
            raise wire.ScrapeBlocked(f"the {kind} answered HTTP 403 without a challenge, title={wire._title_of(r.text)!r}")
        if r.status == 429:
            _bump("rate_limited")
            raise wire.RateLimited(f"http 429 after {self.requests_made} call(s) on {self.label}")
        if r.status != 200:
            _bump(dead)
            raise wire.SessionDead(f"the {kind} answered HTTP {r.status}, title={wire._title_of(r.text)!r} on {self.label}")
        if kind == "page" and not wire.is_app_page(r.text):
            _bump(dead)
            raise wire.SessionDead(f"a 200 that is not the Ad Library page, title={wire._title_of(r.text)!r} on {self.label}")
        return r

    def _get_page(self, url: str, timeout: float | None = None) -> Resp:
        r = self.transport.get(url, headers=self.PAGE_HEADERS, timeout=timeout)
        self.trace.append(f"GET {r.status} {len(r.text)}B rd={r.headers.get('X-FB-Rd') or r.headers.get('x-fb-rd') or '-'}")
        if r.status == 403 and wire.CHALLENGE_MARKER in r.text:
            challenge = wire.challenge_url(r.text)
            if not challenge:
                _bump("blocked")
                raise wire.ScrapeBlocked("challenge page had no parseable __rd_verify URL")
            _bump("challenges")
            self.challenges += 1
            p = self.transport.post(challenge, headers={"Referer": url, "Origin": wire.ORIGIN})
            self.trace.append(f"POST challenge {p.status} cookies={','.join(self.transport.cookie_names()) or '-'}")
            r = self.transport.get(url, headers=self.PAGE_HEADERS, timeout=timeout)
            self.trace.append(f"GET {r.status} {len(r.text)}B cookies={','.join(self.transport.cookie_names()) or '-'}")
        return r


# --------------------------------------------------------------------------- pool


class _Lease:
    """What a search holds for its whole run: the current session, swappable exactly once."""

    def __init__(self, pool: "_SessionPool", session: FbSession) -> None:
        self._pool = pool
        self.session = session
        self.swaps = 0

    def replace(self) -> FbSession:
        self._pool._drop(self.session)
        self.session = self._pool._new()
        self.swaps += 1
        return self.session


class _SessionPool:
    """Round-robin over a few warm sessions; creates lazily; drops retired and expired ones."""

    def __init__(
        self,
        size: int,
        max_concurrency: int,
        factory: Callable[[], Transport] = default_transport,
        limiter: RateLimiter | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.size = max(1, int(size))
        self._factory = factory
        self._limiter = limiter or RateLimiter(settings.rate_limit_per_min, clock, sleep)
        self._clock = clock
        self._sleep = sleep
        self._free: deque[FbSession] = deque()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrency)))
        self.live = 0

    def _new(self) -> FbSession:
        s = FbSession(self._factory(), self._limiter, self._clock, self._sleep)
        with self._lock:
            self.live += 1
        return s

    def _drop(self, s: FbSession) -> None:
        if not s.retired:
            s.retire("dropped")
        with self._lock:
            self.live = max(0, self.live - 1)

    def _take(self) -> FbSession | None:
        with self._lock:
            while self._free:
                s = self._free.popleft()
                if s.retired:
                    self.live = max(0, self.live - 1)
                    continue
                if s.expired:
                    s.retire("expired")
                    self.live = max(0, self.live - 1)
                    continue
                return s
        return None

    def _give_back(self, s: FbSession) -> None:
        with self._lock:
            if s.retired:
                self.live = max(0, self.live - 1)
                return
            self._free.append(s)
            # Keep at most `size` warm; the oldest extra one goes.
            while len(self._free) > self.size:
                old = self._free.popleft()
                old.retire("surplus")
                self.live = max(0, self.live - 1)

    @contextlib.contextmanager
    def lease(self, timeout: float | None = None) -> Iterator[_Lease]:
        """A session for the duration of one search or lookup. With a timeout, a caller that
        cannot get a concurrency slot in time is answered `Busy` instead of queueing."""
        if not self._slots.acquire(timeout=timeout):
            _bump("busy")
            raise wire.Busy(f"every one of the {self._slots._initial_value} slot(s) stayed busy for {timeout:.0f}s")
        try:
            s = self._take()
            if s is None:
                s = self._new()
            lease = _Lease(self, s)
            try:
                yield lease
            finally:
                self._give_back(lease.session)
        finally:
            self._slots.release()

    def snapshot(self) -> dict:
        with self._lock:
            free = list(self._free)
        return {"live": self.live, "warm": len(free), **counters}

    def reset(self) -> None:
        with self._lock:
            self._free.clear()
            self.live = 0


# Until 0.4.0 this module ended with the process-wide `pool` (3 sessions on the host's own
# address), a one-session `recovery_pool` on FALLBACK_PROXY, and ONE limiter shared by both,
# "because the 20-a-minute ceiling is about how this host looks to Meta". Lanes (lanes.py)
# changed what Meta looks at: each lane is one exit IP with its own pool of one session, its own
# limiter and its own GraphQL session, and nothing leaves on the host's address at all.
