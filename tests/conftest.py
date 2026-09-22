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


@pytest.fixture(autouse=True)
def fast_settings():
    """No pacing gaps and no rate-limit naps unless a test sets them, and clean counters."""
    from facebook_ad_library import api, session
    from facebook_ad_library.config import settings

    before = {
        k: getattr(settings, k)
        for k in (
            "api_token", "spacing_min_s", "spacing_max_s", "rate_limit_sleep_s", "ssr_retries", "miss_streak_retire",
            "scrape_budget_s", "brand_budget_s", "brand_ssr_retries", "brand_profile_fallback", "adyntel_cache_ttl_s",
        )
    }
    set_frozen(settings, "api_token", "")  # a filled local .env must not turn the API tests into 401s
    set_frozen(settings, "spacing_min_s", 0)
    set_frozen(settings, "spacing_max_s", 0)
    session.reset_counters()
    session.pool.reset()
    api.cache.clear()
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
