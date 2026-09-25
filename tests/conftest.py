from pathlib import Path

import pytest

from facebook_ad_library import scraper as wire
from facebook_ad_library.session import FbSession, RateLimiter, Resp, _SessionPool

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def set_frozen(obj, name, value):
    """Settings is a frozen dataclass; tests poke it directly."""
    object.__setattr__(obj, name, value)


class Clock:
    """A clock the tests advance by hand, so ages and pacing need no real time."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeFacebook:
    """Stands in for curl_cffi: maps URL prefixes to scripted responses and records every call.

    Each prefix holds a queue; a call pops the next response and the last one repeats, so a
    page script is one response per GET of the search page.
    """

    def __init__(self, script: dict[str, list[Resp]] | None = None) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[tuple[str, str, dict | None, dict]] = []
        self.timeouts: list[float | None] = []
        self.cookies: set[str] = set()

    def _next(self, url: str) -> Resp:
        for key, queue in self.script.items():
            if url.startswith(key):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        return Resp(404, "<html><title>unscripted url</title></html>")

    def get(self, url: str, headers: dict | None = None, timeout: float | None = None) -> Resp:
        self.calls.append(("GET", url, None, headers or {}))
        self.timeouts.append(timeout)
        r = self._next(url)
        if url.startswith(wire.AD_LIBRARY) and r.status == 200:
            self.cookies.add("datr")
        return r

    def post(self, url: str, data: dict | None = None, headers: dict | None = None) -> Resp:
        self.calls.append(("POST", url, data, headers or {}))
        if wire.CHALLENGE_MARKER in url:
            self.cookies.add("rd_challenge")
        return self._next(url)

    def cookie_names(self) -> list[str]:
        return sorted(self.cookies)

    @property
    def page_gets(self) -> list[tuple[str, str, dict | None, dict]]:
        return [c for c in self.calls if c[0] == "GET" and c[1].startswith(wire.AD_LIBRARY)]

    @property
    def plain_gets(self) -> list[tuple[str, str, dict | None, dict]]:
        """GETs of facebook.com pages that are not the Ad Library: the page plugin and profiles."""
        return [c for c in self.calls if c[0] == "GET" and not c[1].startswith(wire.AD_LIBRARY) and wire.CHALLENGE_MARKER not in c[1]]


def page(name: str = "ssr_ads.html", status: int = 200) -> Resp:
    return Resp(status, fixture(name), {"X-FB-Rd": "0"})


CHALLENGE = Resp(403, fixture("challenge_403.html"), {"X-FB-Rd": "1"})


def site(pages: list[Resp] | None = None, challenge: bool = True) -> FakeFacebook:
    """A FakeFacebook whose search page answers `pages` in order (the last one repeats), after
    the one-time challenge when `challenge` is set."""
    pages = list(pages or [page()])
    return FakeFacebook(
        {
            wire.AD_LIBRARY: ([CHALLENGE] if challenge else []) + pages,
            wire.ORIGIN + "/__rd_verify": [Resp(200, "")],
        }
    )


def make_session(transport: FakeFacebook, clock: Clock | None = None, sleeps: list | None = None, per_minute: int = 1000) -> FbSession:
    clock = clock or Clock()
    rec = sleeps if sleeps is not None else []
    sleep = lambda s: (rec.append(s), clock.advance(s))  # noqa: E731
    return FbSession(transport, RateLimiter(per_minute, clock, sleep), clock=clock, sleep=sleep)


def make_pool(transports: list[FakeFacebook], size: int = 2, max_concurrency: int = 2, clock: Clock | None = None, sleeps: list | None = None) -> _SessionPool:
    """A pool whose sessions take transports from `transports` in order; the last one repeats."""
    clock = clock or Clock()
    rec = sleeps if sleeps is not None else []
    sleep = lambda s: (rec.append(s), clock.advance(s))  # noqa: E731
    queue = list(transports)

    def factory():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return _SessionPool(size, max_concurrency, factory, RateLimiter(1000, clock, sleep), clock, sleep)


class StubSession:
    """Stands in for a minted GraphQL session: a script of (ads, cursor) answers, one per page."""

    def __init__(self, script):
        self.script = list(script)
        self.label = "stub"
        self.requests_made = 0
        self.decoded_bytes = 0
        self.minted_at = 0.0
        self.retired = False
        self.asked = []

    @property
    def ready(self) -> bool:
        return not self.retired

    def mint(self, query, country, status):
        self.minted_at = 1.0

    def search_page(self, query, country, cursor, collation, first=30, active_status="ACTIVE"):
        self.asked.append(cursor)
        self.requests_made += 1
        self.decoded_bytes += 1000
        return self.script.pop(0) if self.script else ([], None)


def make_lane(transports: list[FakeFacebook], lane_id: int = 1, clock: Clock | None = None, sleeps: list | None = None,
              gql_script: list | None = None, ports: list[int] | None = None, allocator=None, ip_transport: FakeFacebook | None = None,
              per_minute: int = 1000):
    """A lane on scripted transports. `transports` are handed out in order for every session the
    lane mints (the last one repeats); `gql_script` scripts its GraphQL session; `ip_transport`
    answers the exit-ip check (None: the check is off for this lane)."""
    from facebook_ad_library.lanes import Lane
    from facebook_ad_library.proxy import LaneProxy, PortAllocator

    clock = clock or Clock()
    rec = sleeps if sleeps is not None else []
    sleep = lambda s: (rec.append(s), clock.advance(s))  # noqa: E731
    queue = list(transports)
    ports = ports or [11500 + lane_id]
    allocator = allocator or PortAllocator([LaneProxy(p, f"http://u:p@proxy:{p}") for p in ports])

    def factory(proxy):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    lane = Lane(
        lane_id, allocator, transport_factory=factory,
        ip_transport_factory=(lambda proxy: ip_transport) if ip_transport is not None else None,
        clock=clock, sleep=sleep, limiter=RateLimiter(per_minute, clock, sleep),
    )
    if gql_script is not None:
        from facebook_ad_library.graphql import GraphSlot, MintBreaker

        stub = StubSession(gql_script)
        lane.gql = GraphSlot(factory=lambda: stub, breaker=MintBreaker(clock=clock))
        lane.gql_stub = stub
    return lane


def make_dispatcher(lanes, clock: Clock | None = None, allocator=None):
    """A dispatcher whose workers are NOT started: tests drive `run_item_once` by hand."""
    from facebook_ad_library.lanes import Dispatcher

    return Dispatcher(list(lanes), clock=clock or Clock(), allocator=allocator or lanes[0].allocator)


@pytest.fixture(autouse=True)
def fast_settings():
    """No pacing gaps and no rate-limit naps unless a test sets them, clean counters, and one
    proxy-less lane with the exit-ip check off, so the API tests never touch the network."""
    from facebook_ad_library import api, session
    from facebook_ad_library.config import settings

    before = {
        k: getattr(settings, k)
        for k in (
            "api_token", "spacing_min_s", "spacing_max_s", "gql_spacing_min_s", "gql_spacing_max_s", "rate_limit_sleep_s",
            "ssr_retries", "miss_streak_retire", "scrape_budget_s", "brand_budget_s", "brand_ssr_retries", "brand_profile_fallback",
            "adyntel_cache_ttl_s", "lane_count", "lane_proxy_template", "lane_proxy_ports", "lane_proxies", "lane_require_proxy",
            "lane_ip_check_url", "lane_ip_check_s", "lane_max_tries", "lane_cooldown_s", "lane_cooldown_max_s", "lane_blocked_retry_s",
            "lane_error_cooldown_after", "lane_error_cooldown_s", "lane_withheld_rotate", "lane_rotate_on_block",
            "lane_search_budget_s", "lane_count_budget_s", "fallback_max_pages",
            "job_max_items", "job_store_max", "job_queue_max", "job_item_max_wait_s", "job_ttl_s", "job_poll_max_wait_s",
        )
    }
    set_frozen(settings, "api_token", "")  # a filled local .env must not turn the API tests into 401s
    set_frozen(settings, "spacing_min_s", 0)
    set_frozen(settings, "spacing_max_s", 0)
    set_frozen(settings, "gql_spacing_min_s", 0)
    set_frozen(settings, "gql_spacing_max_s", 0)
    set_frozen(settings, "lane_count", 1)
    set_frozen(settings, "lane_proxy_template", "")
    set_frozen(settings, "lane_proxy_ports", "")
    set_frozen(settings, "lane_proxies", "")
    set_frozen(settings, "lane_require_proxy", False)
    set_frozen(settings, "lane_ip_check_url", "")
    session.reset_counters()
    api.cache.clear()
    api.brand_cache.clear()
    for k in api.counters:
        api.counters[k] = 0
    yield
    for k, v in before.items():
        set_frozen(settings, k, v)


@pytest.fixture
def budget():
    """Override SCRAPE_BUDGET_S on the frozen settings singleton, restoring it afterwards."""
    from facebook_ad_library.config import settings

    before = settings.scrape_budget_s
    yield lambda seconds: set_frozen(settings, "scrape_budget_s", seconds)
    set_frozen(settings, "scrape_budget_s", before)


@pytest.fixture(autouse=True)
def _forget_the_breaker():
    """The legacy GraphQL path (no lane) keeps one mint breaker and one session for the process;
    a test leaving failures on it would make the next refuse to mint. Cleared before and after.
    Lanes carry their own throttle and breaker, made fresh with every lane."""
    from facebook_ad_library import graphql

    graphql.reset_breaker()
    graphql.reset_session()
    yield
    graphql.reset_breaker()
    graphql.reset_session()
