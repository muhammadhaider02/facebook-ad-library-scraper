"""The session lifecycle on scripted transports: the challenge, the page shapes, retirement, the pool."""

import threading

import pytest
from conftest import CHALLENGE, Clock, FakeFacebook, fixture, make_pool, make_session, page, set_frozen, site

from facebook_ad_library import scraper as wire
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import Page, RateLimited, ScrapeBlocked, ScrapeFailed, SessionDead
from facebook_ad_library.session import RateLimiter, Resp, counters

# --------------------------------------------------------------------------- fetch()


def test_first_fetch_clears_the_challenge_and_reads_the_page():
    fb = site()
    s = make_session(fb)
    kind, ads = s.fetch("acupressure mat for back pain", "NZ")
    assert kind is Page.ADS and len(ads) == 5
    methods = [(c[0], c[1].split("?")[0]) for c in fb.calls]
    assert [m[0] for m in methods] == ["GET", "POST", "GET"]
    assert methods[0][1] == methods[2][1] == wire.AD_LIBRARY and methods[1][1].startswith("https://www.facebook.com/__rd_verify_")
    post = fb.calls[1]
    assert post[3]["Origin"] == wire.ORIGIN and post[3]["Referer"].startswith(wire.AD_LIBRARY)
    assert fb.calls[0][3]["Accept"].startswith("text/html") and fb.calls[0][3]["Upgrade-Insecure-Requests"] == "1"
    assert s.challenges == 1 and counters["challenges"] == 1 and s.requests_made == 1 and counters["calls"] == 1
    assert s.trace[0].startswith("GET 403") and "POST challenge 200 cookies=rd_challenge" in s.trace[1] and s.trace[-1] == "page=ads ads=5"
    assert counters["sessions_minted"] == 1


def test_later_fetches_on_the_same_jar_are_not_challenged():
    fb = site([page(), page("ssr_empty.html")])
    s = make_session(fb)
    s.fetch("a", "US")
    kind, ads = s.fetch("b", "US")
    assert kind is Page.EMPTY and ads == [] and len(fb.page_gets) == 3 and s.challenges == 1


def test_challenge_page_without_a_url_is_a_block():
    fb = site([page()])
    fb.script[wire.AD_LIBRARY][0] = Resp(403, "<html>__rd_verify but no fetch()</html>", {"X-FB-Rd": "1"})
    with pytest.raises(ScrapeBlocked, match="parseable"):
        make_session(fb).fetch("a", "US")


def test_403_without_the_marker_is_a_block():
    with pytest.raises(ScrapeBlocked, match="403 without a challenge"):
        make_session(site([Resp(403, "<title>Forbidden</title>")], challenge=False)).fetch("a", "US")
    assert counters["blocked"] == 1


def test_400_after_the_challenge_names_the_tls_symptom():
    with pytest.raises(ScrapeBlocked, match="TLS fingerprint"):
        make_session(site([Resp(400, "<title>Sorry, something went wrong</title>")])).fetch("a", "US")


def test_429_is_rate_limited():
    with pytest.raises(RateLimited, match="429"):
        make_session(site([Resp(429, "")])).fetch("a", "US")
    assert counters["rate_limited"] == 1


@pytest.mark.parametrize("resp", [Resp(302, "", {"Location": "/login"}), Resp(200, fixture("html_200.html"))])
def test_unexpected_status_or_foreign_page_is_session_dead(resp):
    with pytest.raises(SessionDead):
        make_session(site([resp])).fetch("a", "US")
    assert counters["session_dead"] == 1


def test_transients_retry_with_backoff_then_fail():
    sleeps: list = []
    s = make_session(site([Resp(503, "x"), Resp(502, "y"), page()]), sleeps=sleeps)
    kind, ads = s.fetch("a", "US")
    assert kind is Page.ADS and sleeps == [1.0, 3.0] and s.requests_made == 3 and counters["transient"] == 2

    class Boom:
        def get(self, url, headers=None, timeout=None):
            raise ConnectionError("reset")

        def post(self, url, data=None, headers=None):
            raise AssertionError

        def cookie_names(self):
            return []

    with pytest.raises(ScrapeFailed, match="ConnectionError"):
        make_session(Boom()).fetch("a", "US")


def test_bytes_and_last_text_are_recorded():
    s = make_session(site())
    s.fetch("a", "US")
    assert counters["bytes"] == len(fixture("ssr_ads.html")) and s.last_text == fixture("ssr_ads.html")


def test_retired_session_refuses_calls():
    s = make_session(site())
    s.retire("test")
    with pytest.raises(SessionDead, match="retired"):
        s.fetch("a", "US")
    assert counters["retired_by_reason"] == {"test": 1}
    s.retire("again")  # idempotent
    assert counters["sessions_retired"] == 1


def test_miss_streak_retires_the_session_and_a_result_resets_it():
    set_frozen(settings, "miss_streak_retire", 2)
    s = make_session(site([page("ssr_miss.html"), page("ssr_empty.html"), page("ssr_miss.html"), page("ssr_miss.html")]))
    assert s.fetch("a", "US")[0] is Page.MISS and s.miss_streak == 1
    assert s.fetch("a", "US")[0] is Page.EMPTY and s.miss_streak == 0  # an empty result is a result
    s.fetch("a", "US")
    s.fetch("a", "US")
    assert s.retired and s.retire_reason == "miss_streak" and counters["misses"] == 3


def test_session_expires_by_request_count_and_age():
    clock = Clock()
    set_frozen(settings, "session_max_requests", 2)
    set_frozen(settings, "session_max_age_s", 100)
    try:
        s = make_session(site(), clock=clock)
        assert not s.expired
        s.fetch("a", "US")
        s.fetch("b", "US")
        assert s.expired
        s2 = make_session(site(), clock=clock)
        clock.advance(101)
        assert s2.expired
    finally:
        set_frozen(settings, "session_max_requests", 200)
        set_frozen(settings, "session_max_age_s", 7200)


def test_pacing_waits_the_spacing_gap_between_calls():
    set_frozen(settings, "spacing_min_s", 2)
    set_frozen(settings, "spacing_max_s", 2)
    sleeps: list = []
    s = make_session(site([page(), page()]), sleeps=sleeps)
    s.fetch("a", "US")
    s.fetch("b", "US")
    assert sleeps == [2]  # nothing before the first call, one gap before the second


def test_rate_limiter_delays_the_call_over_the_window():
    clock, sleeps = Clock(), []
    lim = RateLimiter(2, clock, lambda s: (sleeps.append(s), clock.advance(s)))
    lim.wait()
    clock.advance(30)
    lim.wait()
    lim.wait()  # third inside the window: waits until the first stamp is 60 s old
    assert sleeps == [30.0]
    clock.advance(31)
    lim.wait()
    assert sleeps == [30.0]


# --------------------------------------------------------------------------- pool


def test_pool_creates_lazily_and_reuses_round_robin():
    pool = make_pool([site([page(), page()]), site([page(), page()])])
    assert counters["sessions_minted"] == 0
    with pool.lease() as a:
        a.session.fetch("a", "US")
    with pool.lease() as b:
        b.session.fetch("b", "US")
    assert a.session is b.session and counters["sessions_minted"] == 1 and pool.snapshot()["live"] == 1


def test_pool_drops_retired_and_expired_sessions_on_the_way_back_in():
    pool = make_pool([site(), site()])
    with pool.lease() as a:
        a.session.retire("test")
    assert pool.snapshot()["live"] == 0
    with pool.lease() as b:
        assert b.session is not a.session
    assert pool.snapshot() == {**pool.snapshot(), "live": 1, "warm": 1}


def test_pool_retires_an_expired_session_when_leased():
    clock = Clock()
    set_frozen(settings, "session_max_age_s", 10)
    try:
        pool = make_pool([site(), site()], clock=clock)
        with pool.lease() as a:
            pass
        clock.advance(11)
        with pool.lease() as b:
            assert b.session is not a.session
        assert counters["retired_by_reason"] == {"expired": 1}
    finally:
        set_frozen(settings, "session_max_age_s", 7200)


def test_lease_replace_creates_a_fresh_session_once():
    pool = make_pool([site(), site()])
    with pool.lease() as lease:
        first = lease.session
        fresh = lease.replace()
        assert fresh is not first and first.retired and first.retire_reason == "dropped" and lease.swaps == 1
    assert pool.snapshot()["live"] == 1 and pool.snapshot()["warm"] == 1


def test_pool_bounds_concurrency_with_the_semaphore():
    pool = make_pool([site()], size=2, max_concurrency=1)
    entered, release = threading.Event(), threading.Event()

    def hold():
        with pool.lease():
            entered.set()
            release.wait(2)

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(2)
    assert not pool._slots.acquire(timeout=0.1)  # the one slot is taken
    release.set()
    t.join(2)
    assert pool._slots.acquire(timeout=1)
    pool._slots.release()


def test_pool_keeps_at_most_size_warm():
    pool = make_pool([site(), site(), site()], size=1)
    with pool.lease() as a, pool.lease() as b:
        assert a.session is not b.session
    snap = pool.snapshot()
    assert snap["warm"] == 1 and snap["live"] == 1 and counters["retired_by_reason"] == {"surplus": 1}


def test_snapshot_reports_counters():
    pool = make_pool([site()])
    with pool.lease() as lease:
        lease.session.fetch("a", "US")
    snap = pool.snapshot()
    assert snap["live"] == 1 and snap["warm"] == 1 and snap["calls"] == 1 and snap["challenges"] == 1 and snap["sessions_minted"] == 1


def test_curl_transport_builds_with_the_configured_impersonation(monkeypatch):
    import sys
    import types

    seen = {}

    class FakeSession:
        def __init__(self, **kw):
            seen.clear()
            seen.update(kw)
            self.headers = {}
            self.cookies = types.SimpleNamespace(jar=[])

    fake = types.ModuleType("curl_cffi")
    fake.requests = types.SimpleNamespace(Session=FakeSession)
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake.requests)
    from facebook_ad_library.session import CurlTransport

    t = CurlTransport(impersonate="chrome", timeout_s=12, proxy="http://u:p@h:1")
    assert seen == {"impersonate": "chrome", "timeout": 12, "proxy": "http://u:p@h:1"}
    assert t._s.headers["Accept-Language"].startswith("en-US") and t.cookie_names() == []
    CurlTransport()
    assert seen == {"impersonate": settings.impersonate, "timeout": settings.request_timeout_s}


# --------------------------------------------------------------------------- fetch_url(), get_plain(), eta, lease timeout


def test_fetch_url_returns_the_html_and_classifies_it():
    fb = site([page("page_view_ads.html")])
    s = make_session(fb)
    kind, ads, html = s.fetch_url(wire.page_view_url("105396194411046"))
    assert kind is Page.ADS and len(ads) == 4 and "search_results_connection" in html
    assert fb.page_gets[-1][1] == wire.page_view_url("105396194411046") and s.requests_made == 1 and counters["calls"] == 1


def test_get_plain_fetches_a_non_ad_library_page_on_the_same_jar_and_pacing():
    fb = FakeFacebook({wire.PLUGIN_URL: [Resp(200, fixture("plugin_page.html"))]})
    s = make_session(fb)
    r = s.get_plain(wire.plugin_url("shaktimats"))
    assert r.status == 200 and wire.page_id_from_plugin(r.text) == "775991435791863"
    assert fb.page_gets == [] and len(fb.plain_gets) == 1 and fb.plain_gets[0][3]["Accept"].startswith("text/html")
    assert counters["plain_calls"] == 1 and counters["calls"] == 0 and s.requests_made == 1


def test_get_plain_clears_a_challenge_and_returns_whatever_page_comes_back():
    fb = FakeFacebook({wire.PLUGIN_URL: [CHALLENGE, Resp(200, fixture("profile_wall.html"))], wire.ORIGIN + "/__rd_verify": [Resp(200, "")]})
    s = make_session(fb)
    r = s.get_plain(wire.plugin_url("x"))
    assert r.status == 200 and "Log in" in r.text and s.challenges == 1  # the caller decides what a wall means


@pytest.mark.parametrize("status, error, counter", [(400, ScrapeBlocked, "plain_blocked"), (403, ScrapeBlocked, "plain_blocked"), (404, SessionDead, "plain_dead")])
def test_get_plain_refusals_are_counted_apart_from_the_page(status, error, counter):
    fb = FakeFacebook({wire.PLUGIN_URL: [Resp(status, "<title>nope</title>")]})
    s = make_session(fb)
    with pytest.raises(error):
        s.get_plain(wire.plugin_url("x"))
    assert counters[counter] == 1 and counters["blocked"] == 0 and counters["session_dead"] == 0


def test_a_deadline_bounds_the_get_timeout_and_stops_transient_retries():
    clock = Clock()
    fb = site([page()], challenge=False)
    s = make_session(fb, clock=clock)
    s.fetch_url(wire.bootstrap_url("a", "US"), deadline=clock() + 12)
    assert fb.timeouts[-1] == 12  # what is left of the budget, under REQUEST_TIMEOUT_S
    s.fetch_url(wire.bootstrap_url("a", "US"))
    assert fb.timeouts[-1] is None
    sleeps: list = []
    s2 = make_session(site([Resp(503, "x"), Resp(503, "y"), page()], challenge=False), clock=clock, sleeps=sleeps)
    with pytest.raises(ScrapeFailed):
        s2.fetch_url(wire.bootstrap_url("a", "US"), deadline=clock() + 2)  # no room for the 1 s nap plus a GET
    assert sleeps == [] and s2.requests_made == 1


def test_rate_limiter_eta_tells_the_wait_without_sleeping():
    clock = Clock()
    lim = RateLimiter(2, clock, lambda s: clock.advance(s))
    assert lim.eta() == 0
    lim.wait()
    lim.wait()
    assert lim.eta() == 60
    clock.advance(45)
    assert lim.eta() == 15
    clock.advance(15)
    assert lim.eta() == 0


def test_lease_with_a_timeout_answers_busy_instead_of_queueing():
    from facebook_ad_library.scraper import Busy

    pool = make_pool([site()], max_concurrency=1)
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with pool.lease():
            holding.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    holding.wait(5)
    try:
        with pytest.raises(Busy):
            with pool.lease(timeout=0.05):
                pass
        assert counters["busy"] == 1
    finally:
        release.set()
        t.join(5)
    with pool.lease(timeout=1) as lease:  # the slot is free again
        assert lease.session is not None
