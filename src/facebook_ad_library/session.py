"""One fake browser session against the Ad Library, and the pool that hands them out.

Modelled on reddit-reviews' mobile.py (Device / _DevicePool). The unit of identity is a session,
not a request: one curl_cffi cookie jar (`datr`, `rd_challenge`), one `lsd` token, one
`sessionID`, one `__req` counter, all minted together and never mixed. A session is used for many
searches and retired at a request count, an age, or the first hard failure; a retired session is
never handed out again.

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
    "rate_limited": 0,
    "session_dead": 0,
    "docid_stale": 0,
    "blocked": 0,
    "transient": 0,
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
    def get(self, url: str, headers: dict | None = None) -> Resp: ...
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

    def get(self, url: str, headers: dict | None = None) -> Resp:
        r = self._s.get(url, headers=headers or {})
        return Resp(int(r.status_code), r.text, dict(r.headers))

    def post(self, url: str, data: dict | None = None, headers: dict | None = None) -> Resp:
        r = self._s.post(url, data=data or {}, headers=headers or {})
        return Resp(int(r.status_code), r.text, dict(r.headers))

    def cookie_names(self) -> list[str]:
        return sorted({c.name for c in self._s.cookies.jar})


def default_transport() -> Transport:
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


# --------------------------------------------------------------------------- session


class FbSession:
    """One fake browser: cookie jar + tokens + doc_id + counters, minted once and paced."""

    BOOTSTRAP_HEADERS = {
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
        self.session_id = str(uuid.uuid4())
        self.tokens: wire.Tokens | None = None
        self.doc_id = ""
        self.doc_id_source = "none"
        self.referer = ""
        self.req_n = 0
        self.requests_made = 0
        self.born = clock()
        self.last_call = 0.0
        self.retired = False
        self.retire_reason = ""
        self.empty_streak = 0
        # What mint() saw, step by step, for the diag command and for log lines on failure.
        self.trace: list[str] = []
        # The last GraphQL response body, raw. The diag command saves it as a fixture source.
        self.last_text = ""

    # ----------------------------------------------------------------- lifecycle

    @property
    def minted(self) -> bool:
        return self.tokens is not None and bool(self.doc_id)

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

    def mint(self, query: str, country: str, active_status: str = "active") -> None:
        """GET the search page; clear the challenge if there is one; read the tokens and the doc_id."""
        url = wire.bootstrap_url(query, country, active_status)
        self.referer = url
        try:
            r = self.transport.get(url, headers=self.BOOTSTRAP_HEADERS)
            self.trace.append(f"GET {r.status} {len(r.text)}B rd={r.headers.get('X-FB-Rd') or r.headers.get('x-fb-rd') or '-'}")
            if r.status == 403 and wire.CHALLENGE_MARKER in r.text:
                challenge = wire.challenge_url(r.text)
                if not challenge:
                    raise wire.ScrapeBlocked("challenge page had no parseable __rd_verify URL")
                _bump("challenges")
                p = self.transport.post(challenge, headers={"Referer": url, "Origin": wire.ORIGIN})
                self.trace.append(f"POST challenge {p.status} cookies={','.join(self.transport.cookie_names()) or '-'}")
                r = self.transport.get(url, headers=self.BOOTSTRAP_HEADERS)
                self.trace.append(f"GET {r.status} {len(r.text)}B cookies={','.join(self.transport.cookie_names()) or '-'}")
        except wire.FacebookError:
            raise
        except Exception as e:  # noqa: BLE001 - anything from the HTTP stack is a vendor failure
            _bump("transient")
            raise wire.ScrapeFailed(f"bootstrap failed: {type(e).__name__}: {e}"[:300]) from e

        if r.status == 400:
            _bump("blocked")
            raise wire.ScrapeBlocked(
                "bootstrap answered HTTP 400 error page after the challenge: the TLS fingerprint was "
                f"rejected (is FB_IMPERSONATE={settings.impersonate!r} a current Chrome?)"
            )
        if r.status != 200:
            _bump("blocked")
            raise wire.ScrapeBlocked(f"bootstrap answered HTTP {r.status}, title={wire._title_of(r.text)!r}")
        try:
            self.tokens = wire.extract_tokens(r.text)
        except wire.ScrapeBlocked:
            _bump("blocked")
            raise
        self.trace.append("tokens " + ", ".join(k for k, v in vars(self.tokens).items() if v))

        if settings.doc_id:
            self.doc_id, self.doc_id_source = settings.doc_id, "env"
        else:
            self.doc_id = self._discover_doc_id(r.text)
            self.doc_id_source = "discovered"
        self.trace.append(f"doc_id {self.doc_id} ({self.doc_id_source})")
        _bump("sessions_minted")
        log.info("%s minted for %s/%s: %s", self.label, query, country, "; ".join(self.trace))

    def _discover_doc_id(self, html: str) -> str:
        urls = wire.bundle_urls(html)

        def texts():
            for u in urls:
                try:
                    yield self.transport.get(u).text
                except Exception as e:  # noqa: BLE001 - one bad bundle must not sink the mint
                    log.warning("%s: bundle fetch failed: %s", self.label, e)

        doc_id = wire.discover_doc_id(texts())
        if not doc_id:
            _bump("docid_stale")
            raise wire.DocIdStale(
                f"no AdLibrarySearchPaginationQuery doc_id in {len(urls)} bundle(s); set FB_DOC_ID from DevTools"
            )
        return doc_id

    # ----------------------------------------------------------------- one call

    def _pace(self) -> None:
        gap = random.uniform(settings.spacing_min_s, max(settings.spacing_min_s, settings.spacing_max_s))
        wait = self.last_call + gap - self._clock()
        if wait > 0:
            self._sleep(wait)
        self.limiter.wait()

    def search_page(
        self,
        query: str,
        country: str,
        cursor: str | None,
        collation_token: str,
        first: int,
        active_status: str = "ACTIVE",
    ) -> tuple[list[dict], str | None]:
        """One GraphQL page. Raises the typed error for anything that is not a page of ads."""
        if self.retired:
            raise wire.SessionDead(f"{self.label} is retired ({self.retire_reason})")
        assert self.tokens is not None and self.doc_id, "search_page before mint"
        self.req_n += 1
        variables = wire.build_variables(
            query=query, country=country, cursor=cursor, collation_token=collation_token,
            session_id=self.session_id, first=first, active_status=active_status, extra=wire.variables_extra(),
        )
        form = wire.build_form(self.tokens, self.doc_id, variables, self.req_n)
        headers = wire.graphql_headers(self.tokens, self.referer)

        delays = (1.0, 3.0)
        for attempt in range(len(delays) + 1):
            self._pace()
            try:
                r = self.transport.post(wire.GRAPHQL, data=form, headers=headers)
            except Exception as e:  # noqa: BLE001
                kind, body, status, text = wire.Kind.TRANSIENT, None, 0, f"{type(e).__name__}: {e}"
            else:
                status, text = r.status, r.text
                kind, body = wire.classify(status, text)
            self.last_call = self._clock()
            self.last_text = text or ""
            self.requests_made += 1
            _bump("calls")
            _bump("bytes", len(text or ""))

            if kind is wire.Kind.TRANSIENT:
                _bump("transient")
                if attempt < len(delays):
                    log.warning("%s: transient failure (http %s), retrying in %.0fs", self.label, status or "-", delays[attempt])
                    self._sleep(delays[attempt])
                    continue
                raise wire.ScrapeFailed(f"graphql failed after {attempt + 1} attempts: http {status or '-'} {text[:120]!r}")
            break

        if kind is wire.Kind.RATE_LIMITED:
            _bump("rate_limited")
            raise wire.RateLimited(f"rate limited after {self.requests_made} call(s) on {self.label}: {wire.error_summary(body)}")
        if kind is wire.Kind.HTML or kind is wire.Kind.BAD_JSON:
            _bump("session_dead")
            raise wire.SessionDead(
                f"graphql answered http {status} with {'an HTML' if kind is wire.Kind.HTML else 'an unparseable'} body "
                f"(title={wire._title_of(text)!r}) on {self.label}"
            )
        if kind is wire.Kind.DATA_NULL:
            _bump("docid_stale")
            raise wire.DocIdStale(f"graphql returned no data on {self.label}: {wire.error_summary(body) or 'no error given'}")

        ads, next_cursor = wire.extract_ads(body)
        if cursor is None:
            self.empty_streak = self.empty_streak + 1 if not ads else 0
            if self.empty_streak >= settings.empty_streak_retire:
                self.retire("empty_streak")
        return ads, next_cursor


# --------------------------------------------------------------------------- pool


class _Lease:
    """What a search holds for its whole run: the current session, swappable exactly once."""

    def __init__(self, pool: "_SessionPool", session: FbSession) -> None:
        self._pool = pool
        self.session = session
        self.swaps = 0

    def replace(self, query: str, country: str) -> FbSession:
        self._pool._drop(self.session)
        self.session = self._pool._new(query, country)
        self.swaps += 1
        return self.session


class _SessionPool:
    """Round-robin over a few warm sessions; mints lazily; drops retired and expired ones."""

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

    def _new(self, query: str, country: str) -> FbSession:
        s = FbSession(self._factory(), self._limiter, self._clock, self._sleep)
        s.mint(query, country)
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
    def lease(self, query: str, country: str) -> Iterator[_Lease]:
        self._slots.acquire()
        try:
            s = self._take()
            if s is None:
                s = self._new(query, country)
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
        return {
            "live": self.live,
            "warm": len(free),
            "doc_id": next((s.doc_id for s in free if s.doc_id), ""),
            "doc_id_source": next((s.doc_id_source for s in free if s.doc_id), "none"),
            **counters,
        }

    def reset(self) -> None:
        with self._lock:
            self._free.clear()
            self.live = 0


pool = _SessionPool(settings.session_pool_size, settings.max_concurrency)
