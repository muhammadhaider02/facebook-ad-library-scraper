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
    bootstrap script is [challenge, page] and a search script is one response per page.
    """

    def __init__(self, script: dict[str, list[Resp]] | None = None) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[tuple[str, str, dict | None, dict]] = []
        self.cookies: set[str] = set()

    def _next(self, url: str) -> Resp:
        for key, queue in self.script.items():
            if url.startswith(key):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        return Resp(404, "<html><title>unscripted url</title></html>")

    def get(self, url: str, headers: dict | None = None) -> Resp:
        self.calls.append(("GET", url, None, headers or {}))
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
    def graphql_calls(self) -> list[tuple[str, str, dict | None, dict]]:
        return [c for c in self.calls if c[1] == wire.GRAPHQL]


def json_resp(name: str, status: int = 200) -> Resp:
    return Resp(status, fixture(name))


def site(graphql: list[Resp] | None = None, challenge: bool = True, bootstrap: Resp | None = None, bundle: Resp | None = None) -> FakeFacebook:
    """A FakeFacebook that mints cleanly: challenge -> trimmed page -> bundle with the doc_id -> the given search pages."""
    page = bootstrap or Resp(200, fixture("bootstrap_trimmed.html"), {"X-FB-Rd": "0"})
    boot = [Resp(403, fixture("challenge_403.html"), {"X-FB-Rd": "1"}), page] if challenge else [page]
    return FakeFacebook(
        {
            wire.AD_LIBRARY: boot,
            wire.ORIGIN + "/__rd_verify": [Resp(200, "")],
            "https://static.xx.fbcdn.net/": [bundle or Resp(200, fixture("bundle_snippet.js"))],
            wire.GRAPHQL: list(graphql or [json_resp("search_page1.json"), json_resp("search_page2_last.json")]),
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

    before = {k: getattr(settings, k) for k in ("spacing_min_s", "spacing_max_s", "rate_limit_sleep_s", "doc_id", "variables_json", "max_pages", "scrape_budget_s")}
    set_frozen(settings, "spacing_min_s", 0)
    set_frozen(settings, "spacing_max_s", 0)
    set_frozen(settings, "doc_id", "")
    set_frozen(settings, "variables_json", "")
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
