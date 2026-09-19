import threading

import pytest
from conftest import Clock, FakeFacebook, Resp, fixture, json_resp, make_pool, make_session, set_frozen, site

from facebook_ad_library import scraper as wire
from facebook_ad_library import session as sess
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import DocIdStale, RateLimited, ScrapeBlocked, ScrapeFailed, SessionDead
from facebook_ad_library.session import RateLimiter

# --------------------------------------------------------------------------- mint


def test_mint_clears_the_challenge_and_reads_tokens_and_doc_id():
    fb = site()
    s = make_session(fb)
    s.mint("running shoes", "US")
    methods = [(m, u.split("?")[0]) for m, u, _, _ in fb.calls]
    assert methods[0] == ("GET", wire.AD_LIBRARY)
    assert methods[1][0] == "POST" and "__rd_verify" in methods[1][1]
    assert methods[2] == ("GET", wire.AD_LIBRARY)
    assert methods[3][0] == "GET" and methods[3][1].startswith("https://static.xx.fbcdn.net/")
    # the challenge POST carries the page as referer, the bootstrap GETs look like a navigation
    assert fb.calls[1][3]["Referer"] == s.referer and fb.calls[1][3]["Origin"] == wire.ORIGIN
    assert fb.calls[0][3]["Upgrade-Insecure-Requests"] == "1"
    assert s.minted and s.tokens.lsd == "FIXTURELSDTOKEN0000000000"
    assert s.doc_id == "24922295957467452" and s.doc_id_source == "discovered"
    assert sess.counters["challenges"] == 1 and sess.counters["sessions_minted"] == 1
    assert any(line.startswith("POST challenge 200 cookies=rd_challenge") for line in s.trace)


def test_mint_without_a_challenge():
    fb = site(challenge=False)
    s = make_session(fb)
    s.mint("q", "US")
    assert [m for m, *_ in fb.calls] == ["GET", "GET"] and sess.counters["challenges"] == 0


def test_doc_id_from_env_skips_the_bundles():
    set_frozen(settings, "doc_id", "111")
    fb = site()
    s = make_session(fb)
    s.mint("q", "US")
    assert s.doc_id == "111" and s.doc_id_source == "env"
    assert not any(u.startswith("https://static.xx") for _, u, _, _ in fb.calls)


def test_no_doc_id_anywhere_is_docid_stale():
    fb = site(bundle=Resp(200, "nothing relevant"))
    with pytest.raises(DocIdStale, match="FB_DOC_ID"):
        make_session(fb).mint("q", "US")
    assert sess.counters["docid_stale"] == 1


def test_403_without_the_marker_is_a_block():
    fb = site(bootstrap=Resp(403, "<html><title>Forbidden</title></html>"), challenge=False)
    with pytest.raises(ScrapeBlocked, match="HTTP 403"):
        make_session(fb).mint("q", "US")
    assert sess.counters["blocked"] == 1


def test_400_after_the_challenge_names_the_tls_symptom():
    fb = site(bootstrap=Resp(400, "<html>Sorry, something went wrong.</html>"))
    with pytest.raises(ScrapeBlocked, match="TLS fingerprint"):
        make_session(fb).mint("q", "US")


def test_page_without_lsd_is_a_block():
    fb = site(bootstrap=Resp(200, "<html><title>Ad Library</title></html>"))
    with pytest.raises(ScrapeBlocked, match="lsd"):
        make_session(fb).mint("q", "US")


def test_network_failure_during_mint_is_scrape_failed():
    class Boom(FakeFacebook):
        def get(self, url, headers=None):
            raise ConnectionError("dns")

    with pytest.raises(ScrapeFailed, match="ConnectionError"):
        make_session(Boom()).mint("q", "US")


# --------------------------------------------------------------------------- search_page


def minted(fb=None, **kw):
    fb = fb or site()
    s = make_session(fb, **kw)
    s.mint("q", "US")
    return s, fb


def test_search_page_posts_the_form_with_the_headers():
    s, fb = minted()
    ads, cursor = s.search_page("running shoes", "US", None, "col", 30)
    assert len(ads) == 3 and cursor == "AQHRfixturecursor1"
    _, url, data, headers = fb.graphql_calls[0]
    assert url == wire.GRAPHQL and tuple(data) == wire.FORM_FIELDS
    assert headers["X-FB-LSD"] == s.tokens.lsd and headers["Referer"] == s.referer
    assert s.requests_made == 1 and s.last_text.startswith("{")


def test_search_page_raises_typed_errors():
    for name, exc in (("rate_limited_1675004.json", RateLimited), ("data_null.json", DocIdStale), ("html_200.html", SessionDead)):
        s, _ = minted(site([json_resp(name)]))
        with pytest.raises(exc):
            s.search_page("q", "US", None, "c", 30)
    s, _ = minted(site([Resp(200, "garbage")]))
    with pytest.raises(SessionDead, match="unparseable"):
        s.search_page("q", "US", None, "c", 30)
    assert sess.counters["rate_limited"] == 1 and sess.counters["docid_stale"] == 1 and sess.counters["session_dead"] == 2


def test_search_page_retries_transients_and_counts_bytes():
    sleeps = []
    s, fb = minted(site([Resp(500, "x"), json_resp("search_page1.json")]), sleeps=sleeps)
    ads, _ = s.search_page("q", "US", None, "c", 30)
    assert len(ads) == 3 and sleeps == [1.0] and s.requests_made == 2
    assert sess.counters["transient"] == 1 and sess.counters["calls"] == 2 and sess.counters["bytes"] > 1000


def test_retired_session_refuses_calls():
    s, _ = minted()
    s.retire("test")
    with pytest.raises(SessionDead, match="retired"):
        s.search_page("q", "US", None, "c", 30)
    assert sess.counters["retired_by_reason"] == {"test": 1}


def test_empty_streak_retires_the_session():
    set_frozen(settings, "empty_streak_retire", 2)
    s, _ = minted(site([json_resp("search_empty.json")]))
    s.search_page("q", "US", None, "c", 30)
    assert not s.retired
    s.search_page("q2", "US", None, "c", 30)
    assert s.retired and s.retire_reason == "empty_streak"


def test_a_page_with_ads_resets_the_empty_streak():
    set_frozen(settings, "empty_streak_retire", 2)
    s, _ = minted(site([json_resp("search_empty.json"), json_resp("search_page1.json"), json_resp("search_empty.json")]))
    for _ in range(3):
        s.search_page("q", "US", None, "c", 30)
    assert not s.retired and s.empty_streak == 1


# --------------------------------------------------------------------------- expiry and pacing


def test_session_expires_by_request_count_and_age():
    set_frozen(settings, "session_max_requests", 2)
    clock = Clock()
    s, _ = minted(clock=clock)
    assert not s.expired
    s.search_page("q", "US", None, "c", 30)
    s.search_page("q", "US", None, "c", 30)
    assert s.expired
    set_frozen(settings, "session_max_requests", 200)
    assert not s.expired
    clock.advance(settings.session_max_age_s + 1)
    assert s.expired


def test_pacing_waits_the_spacing_gap_between_calls():
    set_frozen(settings, "spacing_min_s", 2)
    set_frozen(settings, "spacing_max_s", 2)
    sleeps = []
    s, _ = minted(sleeps=sleeps)
    s.search_page("q", "US", None, "c", 30)
    s.search_page("q", "US", None, "c", 30)
    assert sleeps == [2]  # the first call has nothing to wait for


def test_rate_limiter_delays_the_call_over_the_window():
    clock, sleeps = Clock(), []
    limiter = RateLimiter(2, clock, lambda s: (sleeps.append(s), clock.advance(s)))
    limiter.wait()
    limiter.wait()
    limiter.wait()
    assert sleeps == [60.0]  # third call waited for the window to clear
    limiter.wait()  # second stamp at t=60, no wait
    clock.advance(30)
    limiter.wait()  # both stamps 30 s old -> waits the remaining 30 s
    assert sleeps == [60.0, 30.0]


# --------------------------------------------------------------------------- pool


def test_pool_mints_lazily_and_reuses_round_robin():
    a, b = site(), site()
    pool = make_pool([a, b], size=2)
    with pool.lease("q", "US") as l1:
        first = l1.session
    with pool.lease("q", "US") as l2:
        assert l2.session is first  # only one warm session, so it comes straight back
    assert pool.live == 1 and sess.counters["sessions_minted"] == 1


def test_pool_drops_retired_and_expired_sessions_on_the_way_back_in():
    pool = make_pool([site(), site()])
    with pool.lease("q", "US") as lease:
        lease.session.retire("test")
    assert pool.live == 0
    with pool.lease("q", "US") as lease:
        assert not lease.session.retired
    assert sess.counters["sessions_minted"] == 2


def test_pool_retires_an_expired_session_when_leased():
    set_frozen(settings, "session_max_requests", 1)
    pool = make_pool([site(), site()])
    with pool.lease("q", "US") as lease:
        lease.session.search_page("q", "US", None, "c", 30)
        first = lease.session
    with pool.lease("q", "US") as lease:
        assert lease.session is not first
    assert first.retired and first.retire_reason == "expired"


def test_lease_replace_mints_a_fresh_session_once():
    pool = make_pool([site(), site()])
    with pool.lease("q", "US") as lease:
        old = lease.session
        new = lease.replace("q", "US")
        assert new is not old and old.retired and lease.swaps == 1 and new.minted
    assert pool.live == 1


def test_pool_bounds_concurrency_with_the_semaphore():
    pool = make_pool([site(), site(), site()], size=3, max_concurrency=1)
    entered, release = threading.Event(), threading.Event()

    def hold():
        with pool.lease("q", "US"):
            entered.set()
            release.wait(2)

    t = threading.Thread(target=hold)
    t.start()
    entered.wait(2)
    assert not pool._slots.acquire(blocking=False)  # the one slot is taken
    release.set()
    t.join(2)
    assert pool._slots.acquire(blocking=False)
    pool._slots.release()


def test_snapshot_reports_doc_id_and_counters():
    pool = make_pool([site()])
    with pool.lease("q", "US"):
        pass
    snap = pool.snapshot()
    assert snap["live"] == 1 and snap["warm"] == 1 and snap["doc_id"] == "24922295957467452" and snap["doc_id_source"] == "discovered"
    assert snap["sessions_minted"] == 1 and snap["challenges"] == 1


def test_curl_transport_builds_with_the_configured_impersonation(monkeypatch):
    seen = {}

    class FakeSession:
        def __init__(self, **kw):
            seen.update(kw)
            self.headers = {}
            self.cookies = type("J", (), {"jar": []})()

    import curl_cffi.requests as cr

    monkeypatch.setattr(cr, "Session", FakeSession)
    sess.CurlTransport(impersonate="chrome131", timeout_s=7, proxy="http://u:p@h:10001")
    assert seen == {"impersonate": "chrome131", "timeout": 7, "proxy": "http://u:p@h:10001"}
    seen.clear()
    sess.CurlTransport()
    assert seen["impersonate"] == settings.impersonate and "proxy" not in seen
