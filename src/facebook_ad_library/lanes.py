"""Lanes: every request to Facebook runs on one of N fake browsers, each on its own sticky exit.

Until 0.4.0 the service talked to Facebook from the host's own address, three sessions sharing
one 20-a-minute limiter, and reached for a single residential exit only when Meta withheld the ad
payload. That shape capped Stage 0 at one keyword at a time and, on 24 Sep 2026, answered 722 of
1,320 searches with an empty list before anyone noticed. Umer's decision (stage0.md, 25 Sep 2026):
every Facebook request leaves through a proxy from lane 1 on, up to 8 lanes in parallel, because
the same service answers 01's 50+ checks and 02's research ads - a block on the host's address
would stop the whole pipeline, not just sourcing.

A LANE is one exit IP and everything that was process-global before, per exit:
  - a pool of ONE rendered-page session (cookie jar A) and one GraphQL session (jar B), both on
    the lane's proxy port; a lane serves one request at a time, like one browser
  - its own RateLimiter: the identity Meta sees is the exit IP, so the 20/min pace is per lane
  - its own Throttle memory ("this exit is being withheld from") and its own MintBreaker
  - a state: up | cooling | blocked, with a cooldown that doubles while the probe after it
    keeps failing, and a move to a fresh reserve port on a hard block (the IP is what is blocked)
  - its exit IP, learned through the proxy at mint and re-checked between items, so a vendor
    rotation under a live jar is caught (the jar is retired, the port kept) and so every log line
    and /health row names the address that made the call

The DISPATCHER is one worker thread per lane pulling from one shared queue: lookups from 01/02
first (25 s budgets), single searches next, batch job items last. An item refused by a lane
(blocked, rate limited, withheld with nothing to recover) is retried on a DIFFERENT lane, never the
same one twice, at most LANE_MAX_TRIES times, and then reported `blocked` - never `no_ads`.

Statuses a try can end with, and what they mean to the caller:
  ok         ads were served (rendered page, or recovered over GraphQL on the same lane)
  no_ads     the rendered page carried its results blob with a total of 0: Facebook answered
  withheld   a total above zero and no ads, and GraphQL on this exit could not recover them:
             this exit is being throttled (reported to the caller as `blocked`)
  blocked    a 403 without a challenge, a 400 TLS page, two dead jars, or a closed breaker
  rate_limited  an HTTP 429 or GraphQL 1675004 (reported as `blocked`)
  error      results missing, a network or 5xx failure, a budget or a stale doc_id
  not_found  a count lookup for a page the Ad Library does not know
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable

from .brand import BrandResult, lookup
from .config import settings
from .graphql import DocIdStale, GraphSession, GraphSlot, MintBreaker, page_search
from .proxy import LaneProxy, PortAllocator, lane_proxies
from .scraper import (
    BudgetExceeded,
    Busy,
    FacebookError,
    RateLimited,
    ResultsMissing,
    ScrapeBlocked,
    ScrapeFailed,
    SearchResult,
    SessionDead,
    search,
)
from .session import CurlTransport, RateLimiter, Resp, Transport, _SessionPool, _bump_reason, lane_transport
from .throttle import Throttle

log = logging.getLogger(__name__)

# What the caller is told, per try status.
REPORTED = {
    "ok": "ok", "no_ads": "no_ads", "not_found": "not_found",
    "withheld": "blocked", "blocked": "blocked", "rate_limited": "blocked",
    "error": "error",
}
FINAL = ("ok", "no_ads", "not_found")
BLOCK_KINDS = ("withheld", "blocked", "rate_limited")


# --------------------------------------------------------------------------- items and outcomes


@dataclass
class Item:
    """One unit of work: a keyword search or a brand count, with its retry bookkeeping."""

    kind: str  # search | count
    id: str
    priority: int  # 0 lookups from 01/02, 1 single searches, 2 batch job items
    deadline: float
    query: str = ""
    country: str = "US"
    status: str = "active"
    max_ads: int = 0
    page_id: str | None = None
    facebook_url: str | None = None
    company_domain: str | None = None
    media: str = "all"
    # Explicit deep paging (POST /facebook with max_pages > 1): GraphQL on the lane, no rendered page.
    max_pages: int = 1
    novelty_stop: int = 25
    empty_tol: int = 8
    budget_s: float = 0.0
    cursor: str | None = None
    collation: str | None = None
    max_tries: int = 0
    seq: int = 0
    tried: set[int] = field(default_factory=set)
    tries: list[dict] = field(default_factory=list)
    future: Future = field(default_factory=Future)
    cancelled: bool = False
    submitted_at: float = 0.0
    on_done: Callable[["Item", "Outcome"], None] | None = None


@dataclass
class Try:
    """What one lane made of one item."""

    status: str  # ok | no_ads | withheld | blocked | rate_limited | error | not_found
    ads: list[dict] = field(default_factory=list)
    count: int | None = None
    result: SearchResult | None = None
    brand: BrandResult | None = None
    run: dict | None = None  # the GraphQL recovery, when one ran
    error: str = ""
    error_type: str = ""
    direct_skipped: bool = False
    no_retry: bool = False
    seconds: float = 0.0
    decoded_bytes: int = 0


@dataclass
class Outcome:
    """The final answer for an item, after every try."""

    kind: str
    id: str
    status: str  # ok | no_ads | blocked | error | not_found
    ads: list[dict] = field(default_factory=list)
    count: int | None = None
    result: SearchResult | None = None
    brand: BrandResult | None = None
    run: dict | None = None
    lane: int | None = None
    exit_ip: str | None = None
    tries: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    decoded_bytes: int = 0
    error: str = ""
    error_type: str = ""
    direct_skipped: bool = False
    cached: bool = False
    items: list[dict] | None = None  # the mapped /facebook items when answered from the cache
    unexpected: bool = False  # a non-Facebook exception escaped the try: a bug, answered as a 500

    @property
    def exception(self) -> FacebookError:
        """The typed error a single-call endpoint answers with, for a non-ok outcome."""
        kinds = {"blocked": ScrapeBlocked, "error": ScrapeFailed}
        msg = self.error or f"{self.status} after {len(self.tries)} tr{'y' if len(self.tries) == 1 else 'ies'}"
        if self.error_type == "RateLimited":
            return RateLimited(msg)
        if self.error_type in ("ResultsMissing",):
            return ResultsMissing(msg)
        if self.error_type in ("BudgetExceeded",):
            return BudgetExceeded(msg)
        if self.error_type in ("Busy",):
            return Busy(msg)
        return kinds.get(self.status, ScrapeFailed)(msg)


def _classify(e: Exception) -> Try:
    name = type(e).__name__
    if isinstance(e, ScrapeBlocked):
        status = "blocked"
    elif isinstance(e, RateLimited):
        status = "rate_limited"
    elif isinstance(e, SessionDead):
        status = "blocked"
    elif isinstance(e, DocIdStale):
        return Try("error", error=str(e), error_type=name, no_retry=True)
    else:
        status = "error"
    return Try(status, error=str(e)[:300], error_type=name)


# --------------------------------------------------------------------------- a lane


class _Counting:
    """The lane's transport: whatever curl_cffi (or a test fake) answers, with the decoded bytes
    added to the lane's tally. Decoded, not wire: the proxy bills the compressed wire, about a
    fifth of this for a rendered page (deployment.md); the ratio is what the ramp measures."""

    def __init__(self, inner: Transport, lane: "Lane") -> None:
        self._inner = inner
        self._lane = lane

    def get(self, url: str, headers: dict | None = None, timeout: float | None = None) -> Resp:
        r = self._inner.get(url, headers=headers, timeout=timeout)
        self._lane._count(len(r.text or ""))
        return r

    def post(self, url: str, data: dict | None = None, headers: dict | None = None) -> Resp:
        r = self._inner.post(url, data=data, headers=headers)
        self._lane._count(len(r.text or ""))
        return r

    def cookie_names(self) -> list[str]:
        return self._inner.cookie_names()


def _ip_transport(proxy: str | None) -> Transport:
    return CurlTransport(proxy=proxy, timeout_s=10)


def _read_ip(text: str) -> str:
    """ipify answers `{"ip": "1.2.3.4"}` with `?format=json` and a bare address without."""
    text = (text or "").strip()
    try:
        ip = json.loads(text).get("ip")
        if ip:
            return str(ip)
    except (ValueError, AttributeError):
        pass
    m = re.match(r"^[0-9a-fA-F.:]{3,45}$", text)
    return m.group(0) if m else ""


class Lane:
    """One exit IP, one fake browser, and its own copies of the state that used to be global."""

    def __init__(
        self,
        id: int,
        allocator: PortAllocator,
        *,
        transport_factory: Callable[[str | None], Transport] = lane_transport,
        ip_transport_factory: Callable[[str | None], Transport] | None = _ip_transport,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.id = id
        self.allocator = allocator
        self._clock = clock
        self._sleep = sleep
        self._transport_factory = transport_factory
        self._ip_transport_factory = ip_transport_factory
        exit = allocator.take(id)
        self.port: int | None = exit.port
        self.proxy_url: str | None = exit.url
        self.limiter = limiter or RateLimiter(settings.rate_limit_per_min, clock, sleep)
        self.pool = _SessionPool(1, 1, factory=self._make_transport, limiter=self.limiter, clock=clock, sleep=sleep)
        self.gql = GraphSlot(factory=self._make_gql, breaker=MintBreaker(clock=clock))
        self.throttle = Throttle(clock=clock)
        self.lock = threading.Lock()
        # state
        self.state = "up"
        self.since = clock()
        self.cooldown_until = 0.0
        self.probe = False
        self.escalation = 0
        self.failed_probes = 0
        self.withheld_streak = 0
        self.rate_limited_streak = 0
        self.error_streak = 0
        self.rotations = 0
        self.ip_recheck = False
        # exit ip
        self.exit_ip: str | None = None
        self.ip_learned_at: float | None = None
        self.ip_changes = 0
        # stats
        self.requests = 0
        self.ok = 0
        self.no_ads = 0
        self.not_found = 0
        self.blocked = 0
        self.errors = 0
        self.ms_total = 0.0
        self.decoded_bytes = 0
        self.last_error = ""
        self.last_error_at: float | None = None
        self.events: deque[tuple[float, str]] = deque()  # the last hour, for the block rate

    # ----------------------------------------------------------------- transports

    def _make_transport(self) -> Transport:
        return _Counting(self._transport_factory(self.proxy_url), self)

    def _make_gql(self) -> GraphSession:
        return GraphSession(f"gql{self.id}", transport=self._make_transport(), limiter=self.limiter)

    def _count(self, n: int) -> None:
        with self.lock:
            self.decoded_bytes += n

    @property
    def name(self) -> str:
        return f"lane-{self.id}"

    def tag(self) -> str:
        return f"{self.name} port={self.port or '-'} ip={self.exit_ip or '?'}"

    # ----------------------------------------------------------------- exit ip

    def check_ip(self, reason: str = "mint") -> str | None:
        """Learn the exit IP through the lane's own proxy. A change on the same port is the vendor
        rotating under a live jar: both jars are retired (the port is kept - the new IP now sticks
        for the vendor's interval). A failed check never sinks the lane."""
        url = settings.lane_ip_check_url
        if not url or self._ip_transport_factory is None:
            return self.exit_ip
        try:
            t = self._ip_transport_factory(self.proxy_url)
            r = t.get(url, timeout=10)
            self._count(len(r.text or ""))
            ip = _read_ip(r.text) if r.status == 200 else ""
        except Exception as e:  # noqa: BLE001
            log.warning("%s: exit ip check failed (%s): %s", self.tag(), reason, e)
            with self.lock:
                self.ip_learned_at = self._clock()
                self.ip_recheck = False
            return self.exit_ip
        with self.lock:
            old = self.exit_ip
            self.ip_learned_at = self._clock()
            self.ip_recheck = False
            if not ip:
                log.warning("%s: exit ip check answered http %s with no address (%s)", self.tag(), r.status, reason)
                return old
            self.exit_ip = ip
        if old and ip != old:
            self.ip_changes += 1
            log.warning("%s exit changed %s -> %s (%s): retiring both jars, keeping the port", self.name, old, ip, reason)
            _bump_reason("ip_rotated")
            self.pool.reset()
            self.gql.reset()
        elif not old:
            log.info("%s port=%s exit_ip=%s (%s)", self.name, self.port or "-", ip, reason)
        return ip

    def maybe_check_ip(self) -> None:
        if not settings.lane_ip_check_url or self._ip_transport_factory is None:
            return
        now = self._clock()
        with self.lock:
            due = (
                self.ip_learned_at is None
                or self.ip_recheck
                or (settings.lane_ip_check_s > 0 and now - self.ip_learned_at >= settings.lane_ip_check_s)
            )
        if due:
            self.check_ip("mint" if self.ip_learned_at is None else "recheck")

    # ----------------------------------------------------------------- state

    def available(self) -> bool:
        """Whether this lane may take an item now. Cooldowns expire here, on the worker's own
        clock, so nothing has to run in the background."""
        now = self._clock()
        with self.lock:
            if self.state == "cooling" and now >= self.cooldown_until:
                if self.failed_probes >= 3:
                    self.state = "blocked"
                    self.since = now
                    self.cooldown_until = now + settings.lane_blocked_retry_s
                    self.failed_probes = 0
                    log.warning("%s: three probes blocked in a row -> blocked for %.0fs", self.tag(), settings.lane_blocked_retry_s)
                    return False
                self.state = "up"
                self.since = now
                self.probe = True
                log.info("%s: cooldown over -> up (probe)", self.tag())
            elif self.state == "blocked" and now >= self.cooldown_until:
                self.state = "up"
                self.since = now
                self.probe = True
                self.escalation = 0
                log.info("%s: blocked interval over -> up (probe)", self.tag())
            return self.state == "up"

    def cooldown_left(self) -> float:
        with self.lock:
            if self.state == "up":
                return 0.0
            return max(0.0, self.cooldown_until - self._clock())

    def record(self, t: Try) -> None:
        """Update the counters and the state from one try."""
        now = self._clock()
        cool = None
        with self.lock:
            self.requests += 1
            self.ms_total += t.seconds * 1000
            self.events.append((now, t.status))
            self._trim_events(now)
            if t.status in FINAL:
                setattr(self, t.status, getattr(self, t.status) + 1)
                self.withheld_streak = self.rate_limited_streak = self.error_streak = 0
                if self.probe:
                    self.probe = False
                    self.escalation = 0
                    self.failed_probes = 0
                return
            self.last_error = f"{t.error_type}: {t.error}"[:300] if t.error else t.status
            self.last_error_at = now
            if t.status == "withheld":
                self.blocked += 1
                self.withheld_streak += 1
                if self.withheld_streak >= settings.lane_withheld_rotate:
                    self.withheld_streak = 0
                    cool = (settings.lane_cooldown_s, True, "withheld %d times in a row" % settings.lane_withheld_rotate)
            elif t.status == "blocked":
                self.blocked += 1
                cool = (settings.lane_cooldown_s, settings.lane_rotate_on_block, "hard block")
            elif t.status == "rate_limited":
                self.blocked += 1
                self.rate_limited_streak += 1
                cool = (settings.lane_cooldown_s, self.rate_limited_streak >= 2, "rate limited")
            else:
                self.errors += 1
                self.error_streak += 1
                if self.error_streak >= settings.lane_error_cooldown_after:
                    self.error_streak = 0
                    self.ip_recheck = True
                    # The first run of errors cools the lane and re-checks its IP; errors again on
                    # the probe after that cooldown mean the exit itself is bad (lane-5's address
                    # failed every TLS handshake for an afternoon, 25 Sep 2026), so it is swapped
                    # for a reserve port rather than cooled for longer and longer.
                    cool = (settings.lane_error_cooldown_s, self.probe, "%d errors in a row" % settings.lane_error_cooldown_after)
        if cool:
            self._cool(*cool)

    def _trim_events(self, now: float) -> None:
        while self.events and now - self.events[0][0] > 3600:
            self.events.popleft()

    def _cool(self, base_s: float, rotate: bool, why: str) -> None:
        with self.lock:
            if self.probe:
                self.probe = False
                self.failed_probes += 1
                self.escalation += 1
            seconds = min(base_s * (2 ** self.escalation), settings.lane_cooldown_max_s)
            now = self._clock()
            self.state = "cooling"
            self.since = now
            self.cooldown_until = now + seconds
        self.pool.reset()
        self.gql.reset()
        moved = self.rotate() if rotate else None
        log.warning(
            "%s: %s -> cooling %.0fs%s", self.tag(), why, seconds,
            f", rotated to port {moved.port}" if moved else (", no reserve port; re-minting on the same exit" if rotate else ""),
        )

    def rotate(self) -> LaneProxy | None:
        """Move to the oldest reserve exit. Both jars are dropped: they were bound to the old IP."""
        new = self.allocator.rotate(self.id)
        if new is None:
            return None
        with self.lock:
            self.port, self.proxy_url = new.port, new.url
            self.exit_ip = None
            self.ip_learned_at = None
            self.rotations += 1
        self.pool.reset()
        self.gql.reset()
        return new

    def rate_1h(self) -> tuple[int, int]:
        """(tries, blocked tries) over the last hour."""
        now = self._clock()
        with self.lock:
            self._trim_events(now)
            tries = len(self.events)
            blocked = sum(1 for _, s in self.events if s in BLOCK_KINDS)
        return tries, blocked

    def snapshot(self) -> dict:
        tries, blocked = self.rate_1h()
        # The pool, the GraphQL slot and the throttle each have their own lock, and a worker holds
        # them while it calls back into this lane (a mint counts its bytes through `_count`, which
        # takes `self.lock`). Read them BEFORE taking the lane lock, never inside it: taking the
        # lane lock first and then waiting on a slot lock deadlocked /health against a lane
        # minting a session (25 Sep 2026, exec 4356), and with the event loop stuck every poll
        # hung and the whole service stopped.
        sess = self.pool.snapshot()
        gql = self.gql.snapshot()
        throttled = self.throttle.active()
        with self.lock:
            return {
                "id": self.name, "port": self.port, "exit_ip": self.exit_ip, "state": self.state,
                "since_s": round(self._clock() - self.since, 1),
                "cooldown_left_s": round(max(0.0, self.cooldown_until - self._clock()), 1) if self.state != "up" else 0.0,
                "probe": self.probe, "rotations": self.rotations, "ip_changes": self.ip_changes,
                "session": {"live": sess["live"], "warm": sess["warm"]},
                "gql": gql,
                "throttle_active": throttled,
                "requests": self.requests, "ok": self.ok, "no_ads": self.no_ads, "not_found": self.not_found,
                "blocked": self.blocked, "errors": self.errors,
                "avg_response_ms": round(self.ms_total / self.requests) if self.requests else None,
                "decoded_bytes": self.decoded_bytes,
                "tries_1h": tries, "blocked_tries_1h": blocked,
                "last_error": self.last_error, "last_error_at": self.last_error_at,
            }


# --------------------------------------------------------------------------- one try


def run_search_try(lane: Lane, item: Item) -> Try:
    """The search ladder, on one lane: the rendered page through the lane's exit; when Meta serves
    a total and no ads, GraphQL on the SAME lane (its tokens are bound to this exit); when this
    exit was withheld from recently, GraphQL first and one rendered GET only if that comes back
    empty, because the rendered page's total is what tells `no_ads` from `withheld`."""
    started = time.time()
    deadline = min(item.deadline, started + settings.lane_search_budget_s)
    bytes_before = lane.decoded_bytes

    def done(t: Try) -> Try:
        t.seconds = round(time.time() - started, 1)
        t.decoded_bytes = lane.decoded_bytes - bytes_before
        return t

    def rendered() -> tuple[SearchResult | None, Try | None]:
        try:
            return search(item.query, item.country, 300, item.status, pool=lane.pool, deadline=deadline), None
        except FacebookError as e:
            return None, _classify(e)

    if item.max_pages > 1:
        # Deep paging asked for explicitly: GraphQL on this lane, the caller's own budget and caps.
        try:
            run = page_search(
                item.query, item.country, item.status, item.max_pages, item.novelty_stop, item.empty_tol,
                item.max_ads, item.budget_s, item.cursor, item.collation, slot=lane.gql,
            )
        except FacebookError as e:
            return done(_classify(e))
        ads = list(run["ads"])
        return done(Try("ok" if ads else "no_ads", ads=ads, count=None, run=run, direct_skipped=True))

    result: SearchResult | None = None
    skipped = lane.throttle.active()
    if not skipped:
        result, failed = rendered()
        if failed:
            return done(failed)
    ads = list(result.ads) if result else []
    count = result.count if result else None
    withheld = result is not None and not ads and result.count > 0
    if withheld:
        lane.throttle.seen()
    elif result is not None and ads:
        lane.throttle.clear()

    run = None
    if withheld or skipped:
        remaining = deadline - time.time()
        if remaining < 5:
            if withheld:
                return done(Try("withheld", count=count, result=result, error=f"Meta reports {count} ads and served none; no budget left to recover them"))
            return done(Try("error", error="no budget left for the recovery", error_type="BudgetExceeded"))
        try:
            run = page_search(
                item.query, item.country, item.status,
                settings.fallback_max_pages, 0, settings.page_empty_tol, item.max_ads,
                remaining, None, None, slot=lane.gql,
            )
        except FacebookError as e:
            if withheld:
                # The exit is withholding and the recovery could not run either: this exit's problem,
                # reported as such (the caller sees `blocked`) and retried on another lane.
                return done(Try(
                    "withheld", count=count, result=result,
                    error=f"Meta reports {count} ads for {item.query!r} {item.country} and served none on this exit; the GraphQL recovery failed too: {e}"[:300],
                    error_type=type(e).__name__,
                    no_retry=isinstance(e, DocIdStale),  # another lane would page with the same stale id
                ))
            return done(_classify(e))
        ads = list(run["ads"])
        if not ads and skipped and result is None:
            # Nothing over GraphQL and no rendered total to judge it by: one rendered GET tells
            # an honest empty (count 0) from a withheld page (count above zero, no ads).
            result, failed = rendered()
            if failed:
                return done(failed)
            ads = list(result.ads)
            count = result.count
            withheld = not ads and result.count > 0
            if withheld:
                lane.throttle.seen()
            elif ads:
                lane.throttle.clear()

    if ads:
        return done(Try("ok", ads=ads, count=count, result=result, run=run, direct_skipped=skipped and result is None))
    if withheld:
        return done(Try("withheld", count=count, result=result, run=run, error=f"Meta reports {count} ads for {item.query!r} {item.country} and served none on this exit, even over GraphQL"))
    return done(Try("no_ads", count=0, result=result, run=run, direct_skipped=skipped and result is None))


def run_count_try(lane: Lane, item: Item) -> Try:
    """One brand lookup on one lane. A withheld page view (a total above zero and no ads) is the
    exit's problem, not the brand's: it is reported `withheld` and retried on another lane rather
    than refetched on the same address."""
    started = time.time()
    deadline = min(item.deadline, started + settings.lane_count_budget_s)
    bytes_before = lane.decoded_bytes

    def done(t: Try) -> Try:
        t.seconds = round(time.time() - started, 1)
        t.decoded_bytes = lane.decoded_bytes - bytes_before
        return t

    try:
        res = lookup(
            page_id=item.page_id, facebook_url=item.facebook_url, company_domain=item.company_domain,
            active_status=item.status, media_type=item.media, pool=lane.pool, deadline=deadline, recovery_pool=None,
        )
    except ValueError as e:
        return done(Try("error", error=str(e), error_type="ValueError", no_retry=True))
    except FacebookError as e:
        return done(_classify(e))
    if res.found and res.count > 0 and not res.ads:
        lane.throttle.seen()
        return done(Try("withheld", count=res.count, brand=res, error=f"Meta reports {res.count} ads for page {res.page_id} and served none on this exit"))
    if res.found:
        if res.ads:
            lane.throttle.clear()
        return done(Try("ok", ads=list(res.ads or []), count=res.count, brand=res))
    return done(Try("not_found", brand=res))


def run_try(lane: Lane, item: Item) -> Try:
    return run_search_try(lane, item) if item.kind == "search" else run_count_try(lane, item)


# --------------------------------------------------------------------------- the dispatcher


class Dispatcher:
    """One worker thread per lane, one shared queue ordered by (priority, arrival)."""

    def __init__(self, lanes: list[Lane], clock: Callable[[], float] = time.time, allocator: PortAllocator | None = None) -> None:
        self.lanes = list(lanes)
        self.allocator = allocator
        self._clock = clock
        self._queue: list[Item] = []
        self._cv = threading.Condition()
        self._seq = 0
        self._stop = False
        self._threads: list[threading.Thread] = []
        self.doc_id_stale = False
        self.ip_stats: dict[str, dict] = {}
        self.done_items = 0
        self.started_at = clock()

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._stop = False
        for lane in self.lanes:
            t = threading.Thread(target=self._worker, args=(lane,), name=lane.name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        for t in self._threads:
            t.join(timeout)
        self._threads.clear()

    # ----------------------------------------------------------------- submitting

    def submit(self, item: Item) -> Future:
        item.max_tries = item.max_tries or settings.lane_max_tries
        with self._cv:
            if len(self._queue) >= settings.job_queue_max:
                raise Busy(f"the queue holds {len(self._queue)} item(s); try later")
            self._seq += 1
            item.seq = self._seq
            item.submitted_at = self._clock()
            self._queue.append(item)
            self._cv.notify_all()
        return item.future

    def cancel(self, item: Item) -> None:
        with self._cv:
            item.cancelled = True
            if item in self._queue:
                self._queue.remove(item)
        if not item.future.done():
            self._finish(item, Outcome(item.kind, item.id, "error", tries=list(item.tries), error="cancelled", error_type="Cancelled"))

    @property
    def queue_depth(self) -> int:
        with self._cv:
            return len(self._queue)

    # ----------------------------------------------------------------- scheduling

    def _next_for(self, lane: Lane) -> Item | None:
        """The first queued item this lane may take: not tried here before and not past its
        deadline. Called under the condition's lock."""
        now = self._clock()
        best = None
        for item in self._queue:
            if item.cancelled:
                continue
            if lane.id in item.tried:
                continue
            if best is None or (item.priority, item.seq) < (best.priority, best.seq):
                best = item
        if best is not None:
            self._queue.remove(best)
        return best

    def _expire(self) -> None:
        """Items nobody could take before their deadline. Under the lock."""
        now = self._clock()
        for item in list(self._queue):
            if item.cancelled or now < item.deadline:
                continue
            self._queue.remove(item)
            blocked = any(t.get("status") in BLOCK_KINDS for t in item.tries)
            self._finish(item, Outcome(
                item.kind, item.id, "blocked" if blocked else "error", tries=list(item.tries),
                error=("refused on every lane that could take it before the deadline" if blocked else "no lane was available before the deadline"),
                error_type="ScrapeBlocked" if blocked else "Busy",
            ))

    def _worker(self, lane: Lane) -> None:
        while True:
            with self._cv:
                item = None
                while not self._stop:
                    self._expire()
                    if lane.available():
                        item = self._next_for(lane)
                        if item is not None:
                            break
                        wait = 1.0
                    else:
                        wait = min(1.0, max(0.05, lane.cooldown_left()))
                    self._cv.wait(timeout=wait)
                if self._stop:
                    return
            try:
                self.run_item_once(lane, item)
            except Exception as e:  # noqa: BLE001
                log.exception("%s: unexpected failure on %s %s", lane.tag(), item.kind, item.id)
                lane.record(Try("error", error=str(e)[:300], error_type=type(e).__name__))
                self._finish(item, Outcome(
                    item.kind, item.id, "error", lane=lane.id, exit_ip=lane.exit_ip, tries=list(item.tries),
                    error=str(e)[:300], error_type=type(e).__name__, unexpected=True,
                ))
            with self._cv:
                self._cv.notify_all()

    def run_item_once(self, lane: Lane, item: Item) -> Try:
        """One try of one item on one lane, then the retry-or-finish decision. Public so a test can
        drive it without worker threads."""
        lane.maybe_check_ip()
        self._dedupe_ip(lane)
        t = run_try(lane, item)
        ip = lane.exit_ip  # before record(): a rotation forgets the exit this try ran on
        item.tried.add(lane.id)
        item.tries.append({
            "lane": lane.name, "ip": ip, "status": REPORTED[t.status], "outcome": t.status,
            "reason": (f"{t.error_type}: {t.error}" if t.error_type else t.error)[:300], "seconds": t.seconds,
            "decoded_bytes": t.decoded_bytes,
        })
        lane.record(t)
        self._record_ip(lane, ip, t)
        if isinstance(t.error_type, str) and t.error_type == "DocIdStale":
            self.doc_id_stale = True
        log.log(
            logging.INFO if t.status in FINAL else logging.WARNING,
            "%s %s %s %s%s try=%d/%d %.1fs%s",
            lane.tag(), t.status, item.kind,
            (f"{item.query!r} {item.country}" if item.kind == "search" else (item.page_id or item.facebook_url or item.company_domain)),
            (f" ads={len(t.ads)} total={t.count if t.count is not None else '-'}" if t.status in ("ok", "no_ads", "withheld") else ""),
            len(item.tries), item.max_tries, t.seconds,
            f" {t.error_type}: {t.error}"[:200] if t.error else "",
        )

        final = t.status in FINAL or t.no_retry or item.cancelled or len(item.tries) >= item.max_tries
        if not final:
            with self._cv:
                untried = [l for l in self.lanes if l.id not in item.tried]
                if not untried or self._clock() >= item.deadline:
                    final = True
                else:
                    self._queue.append(item)
                    self._cv.notify_all()
        if final:
            self._finish(item, self._outcome(item, t, lane))
        return t

    def _outcome(self, item: Item, t: Try, lane: Lane) -> Outcome:
        blocked_seen = any(x.get("outcome") in BLOCK_KINDS for x in item.tries)
        status = REPORTED[t.status]
        if status == "error" and blocked_seen:
            status = "blocked"
        return Outcome(
            item.kind, item.id, status, ads=t.ads, count=t.count, result=t.result, brand=t.brand, run=t.run,
            lane=lane.id, exit_ip=item.tries[-1].get("ip") if item.tries else lane.exit_ip, tries=list(item.tries),
            seconds=round(sum(x["seconds"] for x in item.tries), 1),
            decoded_bytes=sum(x.get("decoded_bytes", 0) for x in item.tries),
            error=t.error, error_type=t.error_type, direct_skipped=t.direct_skipped,
        )

    def _finish(self, item: Item, outcome: Outcome) -> None:
        self.done_items += 1
        if item.on_done is not None:
            try:
                item.on_done(item, outcome)
            except Exception:  # noqa: BLE001
                log.exception("on_done failed for %s %s", item.kind, item.id)
        if not item.future.done():
            item.future.set_result(outcome)

    # ----------------------------------------------------------------- exits

    def _dedupe_ip(self, lane: Lane) -> None:
        """Residential pools hand out repeats. Two lanes on one IP are one identity to Meta, so the
        one that learned it later moves to a reserve port (and says so when there is none)."""
        if not lane.exit_ip:
            return
        for other in self.lanes:
            if other is lane or other.exit_ip != lane.exit_ip:
                continue
            newer = lane if (lane.ip_learned_at or 0) >= (other.ip_learned_at or 0) else other
            moved = newer.rotate()
            if moved:
                log.warning("%s and %s share exit %s; %s rotated to port %s", lane.name, other.name, lane.exit_ip, newer.name, moved.port)
                newer.check_ip("shared exit")
            else:
                log.warning("%s and %s share exit %s and there is no reserve port to move to", lane.name, other.name, lane.exit_ip)
            return

    def _record_ip(self, lane: Lane, ip: str | None, t: Try) -> None:
        if not ip:
            return
        now = self._clock()
        with self._cv:
            s = self.ip_stats.get(ip)
            if s is None:
                if len(self.ip_stats) >= 100:
                    oldest = min(self.ip_stats, key=lambda k: self.ip_stats[k]["last_seen"])
                    del self.ip_stats[oldest]
                s = self.ip_stats[ip] = {"requests": 0, "blocks": 0, "ms_total": 0.0, "first_seen": now, "last_seen": now, "lanes": []}
            s["requests"] += 1
            s["blocks"] += 1 if t.status in BLOCK_KINDS else 0
            s["ms_total"] += t.seconds * 1000
            s["last_seen"] = now
            if lane.name not in s["lanes"]:
                s["lanes"].append(lane.name)

    # ----------------------------------------------------------------- health

    def snapshot(self) -> dict:
        lanes = [l.snapshot() for l in self.lanes]
        tries = sum(l["tries_1h"] for l in lanes)
        blocked = sum(l["blocked_tries_1h"] for l in lanes)
        states = [l["state"] for l in lanes]
        with self._cv:
            ip_stats = {
                ip: {
                    "requests": s["requests"], "blocks": s["blocks"],
                    "avg_response_ms": round(s["ms_total"] / s["requests"]) if s["requests"] else None,
                    "first_seen": s["first_seen"], "last_seen": s["last_seen"], "lanes": list(s["lanes"]),
                }
                for ip, s in self.ip_stats.items()
            }
            depth = len(self._queue)
        return {
            "lanes": lanes,
            "lanes_summary": {
                "total": len(lanes), "up": states.count("up"), "cooling": states.count("cooling"), "blocked": states.count("blocked"),
            },
            "block_rate_1h": round(blocked / tries, 3) if tries else 0.0,
            "tries_1h": tries,
            "blocked_tries_1h": blocked,
            "ip_stats": ip_stats,
            "ports": self.allocator.snapshot() if self.allocator else {},
            "queue_depth": depth,
            "doc_id_stale": self.doc_id_stale,
        }


def build_dispatcher(clock: Callable[[], float] = time.time) -> Dispatcher:
    """The production dispatcher from the environment: LANE_COUNT lanes over the configured exits."""
    exits = lane_proxies()
    allocator = PortAllocator(exits)
    lanes = [Lane(i, allocator, clock=clock) for i in range(1, int(settings.lane_count) + 1)]
    return Dispatcher(lanes, clock=clock, allocator=allocator)
