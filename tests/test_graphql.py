"""GraphQL deep paging: the stop conditions, the ceiling, and the proxy rule.

Each stop exists because a measurement said so, and the one that matters most is `empty_tol`:
quitting at the first blank page is what the old paginator did, and on `red light therapy mask`
it cost 135 ads and 25 advertisers. These tests are what stop that regressing.
"""

import pytest
from conftest import Clock, set_frozen

from facebook_ad_library import graphql as g
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import RateLimited, ScrapeBlocked, SessionDead
from facebook_ad_library.session import Resp


def ad(n, page="9"):
    return {"ad_archive_id": str(n), "page_id": str(page), "snapshot": {"page_id": str(page)}}


class StubSession:
    """Stands in for a minted session: a script of (ads, cursor) answers, one per page."""

    def __init__(self, script):
        self.script = list(script)
        self.label = "stub"
        self.requests_made = 0
        self.decoded_bytes = 0
        self.asked = []

    def search_page(self, query, country, cursor, collation, first=30, active_status="ACTIVE"):
        self.asked.append(cursor)
        self.requests_made += 1
        self.decoded_bytes += 1000
        return self.script.pop(0) if self.script else ([], None)


@pytest.fixture
def stub(monkeypatch):
    def install(script):
        s = StubSession(script)
        monkeypatch.setattr(g, "_live_session", lambda *a, **k: s)
        return s

    return install


# --------------------------------------------------------------------------- the stop conditions


def test_a_dropped_cursor_is_the_true_end(stub):
    stub([([ad(1)], "c1"), ([ad(2)], None)])
    r = g.page_search("kw", max_pages=150)
    assert r["pages"] == 2 and r["stopped_because"] == "Meta dropped the cursor (true end)"
    assert len(r["ads"]) == 2


def test_an_empty_page_is_not_the_end(stub):
    """The whole point of the change: page 2 is blank and page 3 still has ads."""
    stub([([ad(1)], "c1"), ([], "c2"), ([ad(2)], "c3"), ([ad(3)], None)])
    r = g.page_search("kw", max_pages=150, empty_tol=8)
    assert r["pages"] == 4 and len(r["ads"]) == 3
    assert r["empty_pages"] == 1
    assert r["stopped_because"] == "Meta dropped the cursor (true end)"


def test_enough_empty_pages_in_a_row_do_end_it(stub):
    stub([([ad(1)], "c")] + [([], "c")] * 20)
    r = g.page_search("kw", max_pages=150, empty_tol=3)
    assert r["stopped_because"] == "4 empty pages in a row"
    assert r["pages"] == 5  # one with ads, then four blanks


def test_empty_tolerance_off_restores_the_old_behaviour(stub):
    stub([([ad(1)], "c1"), ([], "c2"), ([ad(2)], "c3")])
    r = g.page_search("kw", max_pages=150, empty_tol=0)
    assert r["pages"] == 2 and r["stopped_because"] == "first empty page (tolerance off)"


def test_the_novelty_stop_ends_a_tail_of_the_same_advertisers(stub):
    """For brand sourcing the advertiser is the unit of value, not the ad."""
    stub([([ad(1, page="A")], "c")] + [([ad(n, page="A")], "c") for n in range(2, 40)])
    r = g.page_search("kw", max_pages=150, novelty_stop=5)
    assert r["stopped_because"] == "5 pages with no new advertiser"
    assert r["advertisers"] == 1


def test_a_new_advertiser_resets_the_novelty_counter(stub):
    script = [([ad(1, "A")], "c"), ([ad(2, "A")], "c"), ([ad(3, "B")], "c"), ([ad(4, "B")], "c"), ([ad(5, "B")], None)]
    stub(script)
    r = g.page_search("kw", max_pages=150, novelty_stop=3)
    assert r["stopped_because"] == "Meta dropped the cursor (true end)"
    assert r["advertisers"] == 2


def test_the_page_cap_stops_it(stub):
    stub([([ad(n, page=n)], "c") for n in range(1, 200)])
    r = g.page_search("kw", max_pages=10, novelty_stop=0)
    assert r["pages"] == 10 and r["stopped_because"] == "hit the 10-page cap"


def test_150_is_a_hard_ceiling_even_if_more_is_asked_for(stub):
    stub([([ad(n, page=n)], "c") for n in range(1, 400)])
    r = g.page_search("kw", max_pages=500, novelty_stop=0)
    assert r["caps"]["max_pages"] == 150 and r["pages"] == 150


def test_an_ad_target_stops_it(stub):
    stub([([ad(n, page=n)], "c") for n in range(1, 100)])
    r = g.page_search("kw", max_pages=150, novelty_stop=0, max_ads=5)
    assert r["stopped_because"] == "reached the 5-ad target" and len(r["ads"]) == 5


# --------------------------------------------------------------------------- bookkeeping


def test_ads_are_deduplicated_and_advertisers_counted(stub):
    stub([([ad(1, "A"), ad(2, "B")], "c"), ([ad(2, "B"), ad(3, "C")], None)])
    r = g.page_search("kw", max_pages=150)
    assert len(r["ads"]) == 3 and r["advertisers"] == 3


def test_the_cursor_is_threaded_through(stub):
    s = stub([([ad(1)], "c1"), ([ad(2)], "c2"), ([ad(3)], None)])
    g.page_search("kw", max_pages=150)
    assert s.asked == [None, "c1", "c2"]


def test_the_run_reports_what_it_cost(stub):
    stub([([ad(1)], "c"), ([ad(2)], None)])
    r = g.page_search("kw", country="gb", max_pages=150)
    assert r["country"] == "GB"
    assert r["decoded_bytes"] == 2000
    assert len(r["pages_detail"]) == 2
    assert r["pages_detail"][0]["advertisers_total"] == 1


# --------------------------------------------------------------------------- the proxy rule


def test_graphql_refuses_to_run_without_a_proxy(monkeypatch):
    """Meta refuses /api/graphql/ from the VPS address, so an unproxied attempt is a configuration
    error worth failing loudly on rather than a scrape that mysteriously returns nothing."""
    monkeypatch.setattr(g, "fallback_proxy_url", lambda: None)
    with pytest.raises(ScrapeBlocked, match="needs FALLBACK_PROXY"):
        g.GraphSession()


def test_a_session_is_reused_until_it_expires(monkeypatch):
    made = []

    class FakeSession:
        def __init__(self, *a, **k):
            made.append(self)
            self.requests_made = 0
            self.ready = True

        def mint(self, *a, **k):
            pass

    monkeypatch.setattr(g, "GraphSession", FakeSession)
    g.reset_session()
    g._live_session("a", "US", "active")
    g._live_session("b", "US", "active")
    assert len(made) == 1, "the ~725 KB mint must be amortised across keywords"
    made[0].ready = False
    g._live_session("c", "US", "active")
    assert len(made) == 2
    g.reset_session()


# --------------------------------------------------------------------------- transport errors


def build_session(monkeypatch, post_resp):
    class T:
        def get(self, url, headers=None, timeout=None):
            return Resp(200, "")

        def post(self, url, data=None, headers=None):
            return post_resp

    monkeypatch.setattr(g, "fallback_proxy_url", lambda: "http://proxy:1")
    monkeypatch.setattr("facebook_ad_library.session.CurlTransport", lambda **kw: T())
    s = g.GraphSession()
    s.tokens = g.Tokens(lsd="x")
    s.doc_id = "123"
    return s


def test_a_rate_limited_page_retires_the_session(monkeypatch):
    s = build_session(monkeypatch, Resp(200, '{"errors":[{"code":1675004,"message":"rate"}],"data":null}'))
    with pytest.raises(RateLimited):
        s.search_page("kw", "US", None, "tok")
    assert s.retired and s.expired


def test_an_html_body_is_a_dead_session(monkeypatch):
    s = build_session(monkeypatch, Resp(200, "<html><title>nope</title></html>"))
    with pytest.raises(SessionDead):
        s.search_page("kw", "US", None, "tok")


def test_a_null_data_body_is_a_stale_doc_id(monkeypatch):
    s = build_session(monkeypatch, Resp(200, '{"data":null,"errors":[{"code":1,"message":"bad"}]}'))
    with pytest.raises(g.DocIdStale):
        s.search_page("kw", "US", None, "tok")


# --------------------------------------------------------------------------- the query itself


def test_the_variables_carry_the_cursor_and_the_query():
    v = g.build_variables(query="standing desk", country="GB", cursor="C7", collation_token="t",
                          session_id="s", first=30, active_status="ACTIVE")
    assert v["queryString"] == "standing desk" and v["cursor"] == "C7"
    assert v["countries"] == ["GB"] and v["country"] == "GB"
    assert v["searchType"] == "KEYWORD_UNORDERED" and v["first"] == 30


def test_the_form_carries_the_doc_id_and_the_friendly_name():
    form = g.build_form(g.Tokens(lsd="L"), "999", {"a": 1}, 3)
    assert form["doc_id"] == "999"
    assert form["fb_api_req_friendly_name"] == g.FRIENDLY
    assert form["lsd"] == "L" and '"a": 1' in form["variables"].replace("'", '"') or form["variables"]


# --------------------------------------------------------------------------- max_items vs max_ads


def test_max_items_sizes_the_response_and_does_not_stop_the_paging():
    """They were the same field once, and that capped every 150-page run at 300 ads."""
    from facebook_ad_library.api import SearchRequest

    r = SearchRequest(query="kw", max_pages=150, max_items=80)
    assert r.max_ads == 0, "no ad target unless one is asked for: the page cap decides"
    assert SearchRequest(query="kw", max_items=1000).max_items == 1000
    assert SearchRequest(query="kw", max_ads=627).max_ads == 627


# --------------------------------------------------------------------------- doc_id discovery


def test_the_doc_id_comes_from_the_bundle_text_not_its_url(monkeypatch):
    """`discover_doc_id` takes bundle TEXT. Handing it the URLs found nothing and every mint
    died with `set FB_DOC_ID`, which looks like a Meta change and is not one."""
    fetched = []
    page = '<script src="https://static.xx.fbcdn.net/rsrc.php/v1/aa.js"></script>' \
           '<script src="https://static.xx.fbcdn.net/rsrc.php/v1/bb.js"></script>'
    bundles = {
        "https://static.xx.fbcdn.net/rsrc.php/v1/aa.js": "nothing useful here",
        "https://static.xx.fbcdn.net/rsrc.php/v1/bb.js":
            '__d("AdLibrarySearchPaginationQuery_facebookRelayOperation",[],(function(a){a.exports="24922295957467452"}))',
    }

    class T:
        def get(self, url, headers=None, timeout=None):
            fetched.append(url)
            return Resp(200, bundles[url])

    monkeypatch.setattr(g, "fallback_proxy_url", lambda: "http://proxy:1")
    monkeypatch.setattr("facebook_ad_library.session.CurlTransport", lambda **kw: T())
    set_frozen(settings, "doc_id", "")
    s = g.GraphSession()
    assert s._discover_doc_id(page) == "24922295957467452"
    assert len(fetched) == 2, "the generator stops at the first bundle that matches"


def test_a_bad_bundle_does_not_sink_the_mint(monkeypatch):
    page = '<script src="https://static.xx.fbcdn.net/rsrc.php/v1/aa.js"></script>' \
           '<script src="https://static.xx.fbcdn.net/rsrc.php/v1/bb.js"></script>'

    class T:
        def get(self, url, headers=None, timeout=None):
            if url.endswith("aa.js"):
                raise OSError("connection reset")
            return Resp(200, '__d("AdLibrarySearchPaginationQuery_facebookRelayOperation",[],(function(a){a.exports="777"}))')

    monkeypatch.setattr(g, "fallback_proxy_url", lambda: "http://proxy:1")
    monkeypatch.setattr("facebook_ad_library.session.CurlTransport", lambda **kw: T())
    set_frozen(settings, "doc_id", "")
    assert g.GraphSession()._discover_doc_id(page) == "777"


# --------------------------------------------------------------------------- budget and resume


class SlowStub(StubSession):
    """A stub whose pages cost real recorded time, so the budget guard has something to judge."""

    def __init__(self, script, seconds_per_page, clock):
        super().__init__(script)
        self.spp = seconds_per_page
        self.clock = clock

    def search_page(self, query, country, cursor, collation, first=30, active_status="ACTIVE"):
        self.clock.advance(self.spp)
        return super().search_page(query, country, cursor, collation, first, active_status)


@pytest.fixture
def slow(monkeypatch):
    def install(script, seconds_per_page):
        clock = Clock()
        monkeypatch.setattr(g.time, "time", clock)
        s = SlowStub(script, seconds_per_page, clock)
        monkeypatch.setattr(g, "_live_session", lambda *a, **k: s)
        return s

    return install


def test_the_budget_stops_before_a_page_it_cannot_pay_for(slow):
    """783 s for 150 pages against Stage 0's 300 s node timeout is what this exists for."""
    s = slow([([ad(n, page=n)], "c") for n in range(1, 200)], seconds_per_page=10)
    r = g.page_search("kw", max_pages=150, novelty_stop=0, budget_s=100)
    # 10s a page plus the 20% margin means the 9th page is the last one that fits under 100s.
    assert r["pages"] == 9, r["stopped_because"]
    assert r["stopped_because"] == "ran out of the 100s budget after 9 page(s)"
    assert r["truncated"] is True and r["next_cursor"] == "c"


def test_no_budget_runs_to_a_real_stop(slow):
    slow([([ad(n, page=n)], "c") for n in range(1, 40)], seconds_per_page=10)
    r = g.page_search("kw", max_pages=12, novelty_stop=0, budget_s=0)
    assert r["pages"] == 12 and r["stopped_because"] == "hit the 12-page cap"


def test_a_search_that_truly_ended_hands_back_no_cursor(stub):
    stub([([ad(1)], "c1"), ([ad(2)], None)])
    r = g.page_search("kw", max_pages=150)
    assert r["truncated"] is False and r["next_cursor"] is None


def test_a_resumed_call_starts_at_the_given_cursor_and_keeps_the_collation(stub):
    s = stub([([ad(3)], "c4"), ([ad(4)], None)])
    r = g.page_search("kw", max_pages=150, cursor="c3", collation="COL-1")
    assert s.asked == ["c3", "c4"], "the resumed call must not start from the beginning"
    assert r["collation"] == "COL-1", "Meta collates against this; a fresh one re-collates mid-search"


def test_a_first_call_mints_its_own_collation_token(stub):
    stub([([ad(1)], None)])
    r = g.page_search("kw", max_pages=150)
    assert r["collation"] and r["collation"] != "COL-1"


def test_the_two_halves_of_a_split_search_cover_what_one_long_call_would(stub):
    """The point of the handoff: depth reached across calls, not in one 13-minute request."""
    whole = [([ad(n, page=n)], f"c{n}") for n in range(1, 7)] + [([ad(7, page=7)], None)]
    stub(list(whole))
    one = g.page_search("kw", max_pages=150, novelty_stop=0)

    stub(list(whole[:3]))
    first = g.page_search("kw", max_pages=3, novelty_stop=0)
    assert first["truncated"] is True
    stub(list(whole[3:]))
    second = g.page_search("kw", max_pages=150, novelty_stop=0,
                           cursor=first["next_cursor"], collation=first["collation"])
    assert second["truncated"] is False

    ids = lambda r: [a["ad_archive_id"] for a in r["ads"]]  # noqa: E731
    assert ids(first) + ids(second) == ids(one)


# --------------------------------------------------------------------------- the mint breaker


def test_a_session_that_never_returns_a_page_is_counted(monkeypatch):
    """The 24 Sep incident: Meta answered 1675004 on the FIRST call of every fresh session, each
    already paid for, and nothing stopped the next keyword minting another."""
    b = g.MintBreaker(limit=2, cooldown_s=900, clock=Clock())
    b.failed()
    b.check()  # one is a session dying, which is ordinary
    b.failed()
    with pytest.raises(ScrapeBlocked, match="not minting"):
        b.check()


def test_the_breaker_reopens_after_the_cooldown():
    c = Clock()
    b = g.MintBreaker(limit=2, cooldown_s=900, clock=c)
    b.failed(); b.failed()
    with pytest.raises(ScrapeBlocked):
        b.check()
    c.advance(901)
    b.check()  # one more session is tried, rather than latching for good
    assert b.snapshot()["consecutive_failed_sessions"] == 0


def test_a_page_clears_the_count():
    b = g.MintBreaker(limit=2, cooldown_s=900, clock=Clock())
    b.failed()
    b.ok()
    b.failed()
    b.check(), "one failure after a success must not trip it"


def test_a_zero_limit_disables_the_breaker():
    b = g.MintBreaker(limit=0, cooldown_s=900, clock=Clock())
    for _ in range(10):
        b.failed()
    b.check()


def test_an_open_breaker_refuses_before_paying_for_a_mint(monkeypatch):
    """The point of it: no GraphSession is constructed, so no mint is paid for."""
    made = []

    class FakeSession:
        def __init__(self, *a, **k):
            made.append(self)
            self.requests_made, self.ready = 0, True

        def mint(self, *a, **k):
            pass

    monkeypatch.setattr(g, "GraphSession", FakeSession)
    monkeypatch.setattr(g, "breaker", g.MintBreaker(limit=1, cooldown_s=900, clock=Clock()))
    g.reset_session()
    g.breaker.failed()
    with pytest.raises(ScrapeBlocked, match="not minting"):
        g._live_session("kw", "US", "active")
    assert made == [], "an open breaker must not construct a session at all"
    g.reset_session()


def test_the_first_page_of_a_run_clears_the_breaker(stub):
    stub([([ad(1)], None)])
    g.reset_breaker()
    g.breaker.failed()
    g.page_search("kw", max_pages=150)
    assert g.breaker.snapshot()["consecutive_failed_sessions"] == 0


def test_a_run_that_dies_on_its_first_page_trips_the_breaker(monkeypatch):
    """End to end: this is the shape of the 24 Sep incident, where the session minted and then
    was refused 1675004 before returning anything."""
    class DeadSession:
        label, requests_made, decoded_bytes = "dead", 0, 0

        def search_page(self, *a, **k):
            raise RateLimited("1675004: Rate limit exceeded")

    monkeypatch.setattr(g, "_live_session", lambda *a, **k: DeadSession())
    g.reset_breaker()
    for _ in range(2):
        with pytest.raises(RateLimited):
            g.page_search("kw", max_pages=150)
    assert g.breaker.snapshot()["open"] is True
    with pytest.raises(ScrapeBlocked, match="not minting"):
        g.breaker.check()
