"""Lanes: one exit per lane, the per-lane ladder, and the dispatcher's cross-lane retry rules."""

import threading
import time

import pytest
from conftest import CHALLENGE, Clock, FakeFacebook, StubSession, fixture, make_dispatcher, make_lane, page, set_frozen, site

from facebook_ad_library import lanes as L
from facebook_ad_library import scraper as wire
from facebook_ad_library.config import settings
from facebook_ad_library.proxy import LaneProxy, PortAllocator, lane_proxies, parse_ports
from facebook_ad_library.scraper import RateLimited, ResultsMissing, ScrapeBlocked, ScrapeFailed, SearchResult
from facebook_ad_library.session import Resp

SHAKTI = "775991435791863"


def search_item(query="kw", country="US", deadline=None, **kw) -> L.Item:
    return L.Item("search", f"{query}|{country}", 1, deadline if deadline is not None else time.time() + 60, query=query, country=country, **kw)


def count_item(page_id=SHAKTI, deadline=None) -> L.Item:
    return L.Item("count", page_id, 0, deadline if deadline is not None else time.time() + 20, page_id=page_id)


def ad(n, page_id="9"):
    return {"ad_archive_id": str(n), "page_id": page_id, "snapshot": {"page_id": page_id, "caption": "brand.com"}}


def result(count, ads=None) -> SearchResult:
    return SearchResult(query="kw", country="US", ads=list(ads or []), attempts=1, misses=0, seconds=0.1, count=count)


def fake_search(monkeypatch, answer):
    """`answer` is a SearchResult, an exception, or a list of them consumed in order (last repeats)."""
    queue = list(answer) if isinstance(answer, list) else [answer]
    calls = []

    def _search(query, country, max_items, active_status, *, pool, deadline=None, **kw):
        calls.append((query, country, pool, deadline))
        a = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(L, "search", _search)
    return calls


def fake_page_search(monkeypatch, ads=None, exc=None):
    calls = []

    def _page_search(query, country, status, max_pages, novelty, empty_tol, max_ads, budget_s, cursor, collation, slot=None):
        calls.append({"query": query, "max_pages": max_pages, "budget_s": budget_s, "slot": slot})
        if exc is not None:
            raise exc
        return {"ads": list(ads or []), "advertisers": len(ads or []), "pages": 2, "empty_pages": 0, "stopped_because": "end",
                "decoded_bytes": 500, "seconds": 1.0, "next_cursor": None, "collation": "c", "truncated": False,
                "session": {"label": "s", "requests_made": 1, "minted_now": False}}

    monkeypatch.setattr(L, "page_search", _page_search)
    return calls


def step(d: L.Dispatcher, lane: L.Lane):
    """One scheduling turn for one lane, the way its worker would take it, without a thread."""
    with d._cv:
        d._expire()
        item = d._next_for(lane) if lane.available() else None
    if item is None:
        return None
    d.run_item_once(lane, item)
    return item


# --------------------------------------------------------------------------- proxy identity


def test_lane_ports_parse_ranges_and_lists():
    assert parse_ports("11510-11513,11540") == [11510, 11511, 11512, 11513, 11540]
    assert parse_ports("") == []


def test_a_port_listed_twice_is_refused():
    with pytest.raises(ValueError, match="twice"):
        parse_ports("11510,11510")


def test_a_rotating_gateway_port_in_the_lane_range_is_refused():
    with pytest.raises(ValueError, match="rotating gateway"):
        parse_ports("820-825")


def test_startup_refuses_fewer_ports_than_lanes():
    set_frozen(settings, "lane_count", 3)
    set_frozen(settings, "lane_proxy_template", "http://u:p@gw:{port}")
    set_frozen(settings, "lane_proxy_ports", "11510-11511")
    with pytest.raises(ValueError, match="only 2 proxy exit"):
        lane_proxies()


def test_startup_refuses_a_template_without_the_port_slot():
    set_frozen(settings, "lane_proxy_template", "http://u:p@gw:11510")
    set_frozen(settings, "lane_proxy_ports", "11510")
    with pytest.raises(ValueError, match="{port}"):
        lane_proxies()


def test_startup_refuses_a_lane_without_a_proxy_unless_allowed():
    set_frozen(settings, "lane_require_proxy", True)
    with pytest.raises(ValueError, match="no lane proxy configured"):
        lane_proxies()
    set_frozen(settings, "lane_require_proxy", False)
    assert lane_proxies() == [LaneProxy(None, None)]


def test_the_template_fills_each_port_and_the_rest_is_the_reserve():
    set_frozen(settings, "lane_count", 2)
    set_frozen(settings, "lane_proxy_template", "http://u__cr.us:p@gw:{port}")
    set_frozen(settings, "lane_proxy_ports", "11510-11512")
    exits = lane_proxies()
    assert [e.port for e in exits] == [11510, 11511, 11512]
    assert exits[0].url == "http://u__cr.us:p@gw:11510"
    d = L.build_dispatcher()
    assert [l.port for l in d.lanes] == [11510, 11511] and d.allocator.snapshot()["reserve"] == [11512]


def test_two_lanes_never_hold_the_same_port():
    alloc = PortAllocator([LaneProxy(p, f"http://u:p@gw:{p}") for p in (1, 2, 3)])
    a = make_lane([site()], 1, allocator=alloc)
    b = make_lane([site()], 2, allocator=alloc)
    assert a.port != b.port
    moved = a.rotate()
    assert moved.port == 3 and alloc.snapshot() == {"held": {1: 3, 2: 2}, "reserve": [1]}
    assert b.rotate().port == 1 and a.rotate().port == 2 and a.rotate() is not None


def test_a_lane_learns_and_logs_its_exit_ip_at_mint(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="facebook_ad_library.lanes")
    set_frozen(settings, "lane_ip_check_url", "https://api.ipify.org?format=json")
    ipify = FakeFacebook({"https://api.ipify.org": [Resp(200, '{"ip": "86.1.2.3"}')]})
    lane = make_lane([site()], ip_transport=ipify)
    lane.maybe_check_ip()
    assert lane.exit_ip == "86.1.2.3" and lane.ip_learned_at is not None
    assert "exit_ip=86.1.2.3" in caplog.text
    lane.maybe_check_ip()
    assert len(ipify.calls) == 1, "not re-checked before LANE_IP_CHECK_S"


def test_a_failed_ip_check_does_not_sink_the_lane():
    set_frozen(settings, "lane_ip_check_url", "https://api.ipify.org?format=json")
    lane = make_lane([site()], ip_transport=FakeFacebook({"https://api.ipify.org": [Resp(500, "boom")]}))
    lane.maybe_check_ip()
    assert lane.exit_ip is None and lane.state == "up" and lane.ip_learned_at is not None


def test_a_changed_exit_ip_retires_both_jars_and_keeps_the_port(monkeypatch):
    set_frozen(settings, "lane_ip_check_url", "https://api.ipify.org?format=json")
    ipify = FakeFacebook({"https://api.ipify.org": [Resp(200, "86.1.2.3"), Resp(200, "86.9.9.9")]})
    lane = make_lane([site(), site()], ip_transport=ipify, gql_script=[])
    fake_search(monkeypatch, result(30, [ad(1)]))
    lane.maybe_check_ip()
    port = lane.port
    with lane.pool.lease() as lease:
        first = lease.session
    lane.gql.live("kw", "US", "active")
    lane.check_ip("recheck")
    assert lane.exit_ip == "86.9.9.9" and lane.ip_changes == 1 and lane.port == port
    assert lane.pool.snapshot()["warm"] == 0, "the rendered-page jar is dropped"
    with lane.pool.lease() as lease:
        assert lease.session is not first
    assert lane.gql_stub.retired, "the GraphQL jar is dropped too"


def test_two_lanes_with_one_exit_ip_rotate_the_later_one(monkeypatch):
    set_frozen(settings, "lane_ip_check_url", "https://api.ipify.org?format=json")
    alloc = PortAllocator([LaneProxy(p, f"http://u:p@gw:{p}") for p in (1, 2, 3)])
    ipify = FakeFacebook({"https://api.ipify.org": [Resp(200, "1.1.1.1")]})
    clock = Clock()
    a = make_lane([site()], 1, allocator=alloc, ip_transport=ipify, clock=clock)
    b = make_lane([site()], 2, allocator=alloc, ip_transport=ipify, clock=clock)
    d = make_dispatcher([a, b], clock=clock, allocator=alloc)
    fake_search(monkeypatch, result(30, [ad(1)]))
    a.maybe_check_ip()
    clock.advance(1)
    d.submit(search_item("x"))
    step(d, b)  # b learns the same ip as a and, being the later one, moves to port 3
    assert b.port == 3 and b.rotations == 1 and a.port == 1


# --------------------------------------------------------------------------- the ladder per lane


def test_a_page_with_ads_is_ok_with_ads_found():
    lane = make_lane([site()])
    t = L.run_search_try(lane, search_item())
    assert t.status == "ok" and len(t.ads) == 5 and t.count is not None and t.run is None
    assert t.decoded_bytes > 0 and t.seconds >= 0


def test_an_honest_empty_page_is_no_ads_and_never_pays_for_graphql(monkeypatch):
    calls = fake_page_search(monkeypatch, ads=[ad(1)])
    lane = make_lane([site([page("ssr_empty.html")])])
    t = L.run_search_try(lane, search_item())
    assert t.status == "no_ads" and t.count == 0 and not calls
    assert not lane.throttle.active()


def test_a_withheld_page_recovers_over_graphql_on_the_same_lane(monkeypatch):
    fake_search(monkeypatch, result(1039))
    calls = fake_page_search(monkeypatch, ads=[ad(1), ad(2)])
    lane = make_lane([site()])
    t = L.run_search_try(lane, search_item())
    assert t.status == "ok" and len(t.ads) == 2 and t.count == 1039 and t.run is not None
    assert calls[0]["slot"] is lane.gql, "the recovery runs on this lane's own GraphQL session"
    assert calls[0]["max_pages"] == settings.fallback_max_pages
    assert lane.throttle.active(), "the exit is remembered as withheld"


def test_a_withheld_page_whose_recovery_is_empty_is_withheld_not_no_ads(monkeypatch):
    fake_search(monkeypatch, result(1039))
    fake_page_search(monkeypatch, ads=[])
    lane = make_lane([site()])
    t = L.run_search_try(lane, search_item())
    assert t.status == "withheld" and L.REPORTED[t.status] == "blocked" and t.count == 1039
    assert "served none" in t.error


def test_a_withheld_page_whose_recovery_fails_is_still_withheld(monkeypatch):
    fake_search(monkeypatch, result(500))
    fake_page_search(monkeypatch, exc=ScrapeBlocked("breaker open"))
    lane = make_lane([site()])
    t = L.run_search_try(lane, search_item())
    assert t.status == "withheld" and t.error_type == "ScrapeBlocked" and "served none" in t.error


def test_the_throttle_memory_is_per_lane(monkeypatch):
    calls = fake_search(monkeypatch, result(500))
    fake_page_search(monkeypatch, ads=[ad(1)])
    a, b = make_lane([site()], 1), make_lane([site()], 2)
    L.run_search_try(a, search_item())
    assert a.throttle.active() and not b.throttle.active()
    t = L.run_search_try(a, search_item("second"))
    assert t.status == "ok" and t.direct_skipped and len(calls) == 1, "lane a skips its rendered GET while withheld"
    L.run_search_try(b, search_item("third"))
    assert len(calls) == 2, "lane b still makes its rendered GET"


def test_a_skipped_direct_get_with_an_empty_recovery_makes_one_rendered_get_to_judge_it(monkeypatch):
    """No total from GraphQL, so one rendered page tells an honest empty from a withheld one."""
    calls = fake_search(monkeypatch, [result(500), result(0)])
    fake_page_search(monkeypatch, ads=[])
    lane = make_lane([site()])
    assert L.run_search_try(lane, search_item()).status == "withheld"
    t = L.run_search_try(lane, search_item("quiet"))
    assert t.status == "no_ads" and t.count == 0 and len(calls) == 2


def test_the_recovery_budget_is_what_is_left_of_the_try(monkeypatch):
    fake_search(monkeypatch, result(500))
    calls = fake_page_search(monkeypatch, ads=[ad(1)])
    set_frozen(settings, "lane_search_budget_s", 30)
    lane = make_lane([site()])
    L.run_search_try(lane, search_item(deadline=time.time() + 600))
    assert 0 < calls[0]["budget_s"] <= 30


def test_a_429_fails_fast_instead_of_napping_60s_on_a_lane():
    set_frozen(settings, "rate_limit_sleep_s", 60)
    set_frozen(settings, "lane_search_budget_s", 90)
    sleeps: list = []
    lane = make_lane([site([Resp(429, "slow down")]), site([Resp(429, "slow down")])], sleeps=sleeps)
    t = L.run_search_try(lane, search_item())
    assert t.status == "rate_limited" and 60 not in sleeps


def test_graphql_pages_count_against_the_lane_limiter():
    from facebook_ad_library.graphql import GraphSession, Tokens
    from facebook_ad_library.session import RateLimiter

    waits = []

    class Limiter(RateLimiter):
        def wait(self):
            waits.append(1)

    fb = FakeFacebook({"https://www.facebook.com/api/graphql/": [Resp(200, '{"data": {"ad_library_main": {"search_results_connection": {"edges": [], "page_info": {"has_next_page": false, "end_cursor": null}}}}}')]})
    s = GraphSession("t", transport=fb, limiter=Limiter(1000))
    s.tokens, s.doc_id = Tokens(lsd="x"), "123"
    s.search_page("kw", "US", None, "c")
    assert waits == [1]


def test_a_withheld_count_lookup_is_not_refetched_on_the_same_ip():
    lane = make_lane([FakeFacebook({wire.AD_LIBRARY: [CHALLENGE, page("page_view_withheld.html")], wire.ORIGIN + "/__rd_verify": [Resp(200, "")]})])
    t = L.run_count_try(lane, count_item())
    assert t.status == "withheld" and t.count == 1039 and t.brand.recovery_gets == 0
    assert len(lane.pool.snapshot()) and lane.throttle.active()


def test_a_count_with_ads_is_ok_and_an_unknown_page_is_not_found():
    lane = make_lane([FakeFacebook({wire.AD_LIBRARY: [CHALLENGE, page("page_view_ads.html")], wire.ORIGIN + "/__rd_verify": [Resp(200, "")]})])
    t = L.run_count_try(lane, count_item("105396194411046"))
    assert t.status == "ok" and t.count == 1783 and len(t.ads) == 4
    lane = make_lane([FakeFacebook({wire.AD_LIBRARY: [CHALLENGE, page("page_view_unknown.html")], wire.ORIGIN + "/__rd_verify": [Resp(200, "")]})])
    assert L.run_count_try(lane, count_item("1234")).status == "not_found"


# --------------------------------------------------------------------------- lane state


def test_three_withheld_pages_in_a_row_rotate_the_lane(monkeypatch):
    fake_search(monkeypatch, result(500))
    fake_page_search(monkeypatch, ads=[])
    alloc = PortAllocator([LaneProxy(p, f"http://u:p@gw:{p}") for p in (1, 2)])
    lane = make_lane([site()], allocator=alloc)
    for _ in range(3):
        lane.record(L.run_search_try(lane, search_item()))
    assert lane.state == "cooling" and lane.port == 2 and lane.rotations == 1
    assert lane.cooldown_left() == pytest.approx(settings.lane_cooldown_s)


def test_a_hard_block_cools_the_lane_and_rotates_the_port():
    alloc = PortAllocator([LaneProxy(p, f"http://u:p@gw:{p}") for p in (1, 2)])
    lane = make_lane([site()], allocator=alloc)
    lane.record(L.Try("blocked", error="403", error_type="ScrapeBlocked"))
    assert lane.state == "cooling" and lane.port == 2 and lane.blocked == 1 and not lane.available()


def test_the_cooldown_doubles_when_the_probe_blocks_again():
    set_frozen(settings, "lane_cooldown_s", 100)
    clock = Clock()
    lane = make_lane([site()], clock=clock)
    lane.record(L.Try("blocked", error="403", error_type="ScrapeBlocked"))
    assert lane.cooldown_left() == 100
    clock.advance(101)
    assert lane.available() and lane.probe
    lane.record(L.Try("blocked", error="403", error_type="ScrapeBlocked"))
    assert lane.cooldown_left() == 200 and lane.failed_probes == 1
    clock.advance(201)
    assert lane.available()
    lane.record(L.Try("ok"))
    assert lane.escalation == 0 and lane.failed_probes == 0 and not lane.probe


def test_three_failed_probes_block_the_lane_until_the_retry_interval():
    set_frozen(settings, "lane_cooldown_s", 10)
    set_frozen(settings, "lane_cooldown_max_s", 10)
    set_frozen(settings, "lane_blocked_retry_s", 1000)
    clock = Clock()
    lane = make_lane([site()], clock=clock)
    for _ in range(4):
        lane.record(L.Try("blocked", error="403", error_type="ScrapeBlocked"))
        clock.advance(11)
        lane.available()
    assert lane.state == "blocked" and lane.cooldown_left() > 900
    clock.advance(1001)
    assert lane.available() and lane.state == "up" and lane.probe


def test_errors_cool_the_lane_briefly_after_three_in_a_row_and_ask_for_an_ip_check():
    set_frozen(settings, "lane_error_cooldown_after", 3)
    set_frozen(settings, "lane_error_cooldown_s", 120)
    lane = make_lane([site()])
    for _ in range(2):
        lane.record(L.Try("error", error="net", error_type="ScrapeFailed"))
    assert lane.state == "up"
    lane.record(L.Try("error", error="net", error_type="ScrapeFailed"))
    assert lane.state == "cooling" and lane.cooldown_left() == 120 and lane.ip_recheck and lane.rotations == 0


def test_the_block_rate_forgets_events_older_than_an_hour():
    clock = Clock()
    lane = make_lane([site()], clock=clock)
    lane.record(L.Try("blocked", error="x", error_type="ScrapeBlocked"))
    clock.advance(3601)
    lane.available()
    lane.record(L.Try("ok"))
    lane.record(L.Try("ok"))
    assert lane.rate_1h() == (2, 0)
    assert lane.snapshot()["requests"] == 3 and lane.snapshot()["blocked"] == 1


def test_per_lane_counters_and_average_response_time():
    lane = make_lane([site()])
    lane.record(L.Try("ok", seconds=1.0))
    lane.record(L.Try("no_ads", seconds=3.0))
    s = lane.snapshot()
    assert s["requests"] == 2 and s["ok"] == 1 and s["no_ads"] == 1 and s["avg_response_ms"] == 2000 and s["state"] == "up"


# --------------------------------------------------------------------------- the dispatcher


def three_lanes(clock=None):
    clock = clock or Clock()
    alloc = PortAllocator([LaneProxy(p, f"http://u:p@gw:{p}") for p in (1, 2, 3, 4)])
    ls = [make_lane([site()], i, allocator=alloc, clock=clock) for i in (1, 2, 3)]
    return ls, make_dispatcher(ls, clock=clock, allocator=alloc)


def test_a_blocked_try_moves_the_search_to_a_different_lane(monkeypatch):
    fake_search(monkeypatch, [ScrapeBlocked("wall"), result(30, [ad(1)])])
    (a, b, c), d = three_lanes()
    item = search_item()
    fut = d.submit(item)
    step(d, a)
    assert not fut.done() and a.state == "cooling"
    assert step(d, a) is None, "the same lane never takes it again"
    step(d, b)
    out = fut.result(0)
    assert out.status == "ok" and out.lane == 2 and [t["lane"] for t in out.tries] == ["lane-1", "lane-2"]


def test_never_the_same_lane_twice_and_at_most_three_tries(monkeypatch):
    fake_search(monkeypatch, ScrapeBlocked("wall"))
    set_frozen(settings, "lane_max_tries", 3)
    ls, d = three_lanes()
    alloc = ls[0].allocator
    extra = make_lane([site()], 4, allocator=alloc, clock=d._clock)
    d.lanes.append(extra)
    fut = d.submit(search_item())
    for lane in ls:
        step(d, lane)
    out = fut.result(0)
    assert out.status == "blocked" and len(out.tries) == 3 and len({t["lane"] for t in out.tries}) == 3
    assert step(d, extra) is None, "a fourth lane is never asked"
    assert out.exception.status == 503 and isinstance(out.exception, ScrapeBlocked)


def test_with_one_lane_a_block_is_reported_after_one_try(monkeypatch):
    fake_search(monkeypatch, RateLimited("429"))
    lane = make_lane([site()])
    d = make_dispatcher([lane])
    fut = d.submit(search_item())
    step(d, lane)
    out = fut.result(0)
    assert out.status == "blocked" and len(out.tries) == 1 and isinstance(out.exception, RateLimited)


def test_results_missing_is_retried_elsewhere_but_does_not_cool_the_lane(monkeypatch):
    fake_search(monkeypatch, [ResultsMissing("no blob"), result(30, [ad(1)])])
    (a, b, c), d = three_lanes()
    fut = d.submit(search_item())
    step(d, a)
    assert a.state == "up" and a.errors == 1 and not fut.done()
    step(d, b)
    assert fut.result(0).status == "ok"


def test_an_error_on_every_lane_is_reported_as_error_not_blocked(monkeypatch):
    fake_search(monkeypatch, ScrapeFailed("net"))
    ls, d = three_lanes()
    fut = d.submit(search_item())
    for lane in ls:
        step(d, lane)
    out = fut.result(0)
    assert out.status == "error" and isinstance(out.exception, ScrapeFailed)


def test_a_cooling_lane_pulls_no_work_until_its_cooldown_expires(monkeypatch):
    fake_search(monkeypatch, result(30, [ad(1)]))
    clock = Clock()
    lane = make_lane([site()], clock=clock)
    d = make_dispatcher([lane], clock=clock)
    lane.record(L.Try("blocked", error="x", error_type="ScrapeBlocked"))
    fut = d.submit(search_item(deadline=clock() + 5000))
    assert step(d, lane) is None
    clock.advance(settings.lane_cooldown_s + 1)
    assert step(d, lane) is not None and fut.result(0).status == "ok"


def test_lookups_jump_the_queue_ahead_of_batch_items(monkeypatch):
    fake_search(monkeypatch, result(30, [ad(1)]))
    monkeypatch.setattr(L, "lookup", lambda **kw: __import__("facebook_ad_library.brand", fromlist=["BrandResult"]).BrandResult("page_id", SHAKTI, "active", "all", found=True, page_id=SHAKTI, count=5, ads=[ad(1)], info={"page_name": "x"}))
    lane = make_lane([site()])
    d = make_dispatcher([lane])
    for n in range(3):
        d.submit(L.Item("search", f"job{n}", 2, time.time() + 60, query=f"kw{n}"))
    d.submit(count_item())
    d.submit(L.Item("search", "single", 1, time.time() + 60, query="single"))
    order = [step(d, lane).id for _ in range(5)]
    assert order == [SHAKTI, "single", "job0", "job1", "job2"]


def test_an_item_nobody_can_take_before_its_deadline_is_answered(monkeypatch):
    fake_search(monkeypatch, ScrapeBlocked("wall"))
    clock = Clock()
    lane = make_lane([site()], clock=clock)
    d = make_dispatcher([lane], clock=clock)
    lane.record(L.Try("blocked", error="x", error_type="ScrapeBlocked"))
    fut = d.submit(search_item(deadline=clock() + 10))
    clock.advance(11)
    step(d, lane)
    out = fut.result(0)
    assert out.status == "error" and "no lane was available" in out.error


def test_cancel_removes_a_queued_item_and_answers_it(monkeypatch):
    lane = make_lane([site()])
    d = make_dispatcher([lane])
    item = search_item()
    fut = d.submit(item)
    d.cancel(item)
    assert fut.result(0).error == "cancelled" and d.queue_depth == 0


def test_a_stale_doc_id_is_flagged_and_not_retried(monkeypatch):
    from facebook_ad_library.graphql import DocIdStale

    fake_search(monkeypatch, result(500))
    fake_page_search(monkeypatch, exc=DocIdStale("schema moved"))
    ls, d = three_lanes()
    fut = d.submit(search_item())
    step(d, ls[0])
    out = fut.result(0)
    assert d.doc_id_stale and out.status == "blocked", "withheld plus a stale doc id: this exit's problem, and a human's"
    assert len(out.tries) == 1


def test_the_snapshot_reports_lanes_block_rate_and_ips(monkeypatch):
    fake_search(monkeypatch, [ScrapeBlocked("wall"), result(30, [ad(1)])])
    (a, b, c), d = three_lanes()
    a.exit_ip, b.exit_ip = "1.1.1.1", "2.2.2.2"
    fut = d.submit(search_item())
    step(d, a)
    step(d, b)
    fut.result(0)
    s = d.snapshot()
    assert s["lanes_summary"] == {"total": 3, "up": 2, "cooling": 1, "blocked": 0}
    assert s["tries_1h"] == 2 and s["blocked_tries_1h"] == 1 and s["block_rate_1h"] == 0.5
    assert s["ip_stats"]["1.1.1.1"]["blocks"] == 1 and s["ip_stats"]["2.2.2.2"]["requests"] == 1
    assert s["ports"]["held"][1] == 4, "lane 1 rotated onto the reserve port"


def test_workers_run_items_in_threads_and_the_counters_stay_consistent(monkeypatch):
    fake_search(monkeypatch, result(30, [ad(1)]))
    ls, d = three_lanes(clock=time.time)
    for lane in ls:
        lane._clock = time.time
    d.start()
    try:
        futs = [d.submit(L.Item("search", f"kw{n}", 2, time.time() + 30, query=f"kw{n}")) for n in range(24)]
        outs = [f.result(10) for f in futs]
    finally:
        d.stop()
    assert all(o.status == "ok" for o in outs)
    assert sum(l.requests for l in ls) == 24 and d.done_items == 24 and d.queue_depth == 0
