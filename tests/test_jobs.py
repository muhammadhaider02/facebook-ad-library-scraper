"""POST /jobs: a run of searches and counts in, results paired back by id out."""

import json
import time

import pytest
from conftest import fixture, set_frozen
from fastapi.testclient import TestClient

from facebook_ad_library import api, lanes
from facebook_ad_library.brand import BrandResult
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import ScrapeBlocked, SearchResult, classify_page, classify_page_view


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def ads():
    return classify_page(fixture("ssr_ads.html"))[1]


def fake_search(monkeypatch, per_query=None):
    calls = []

    def _search(query, country, max_items, active_status, *, pool, deadline=None, **kw):
        calls.append(query)
        answer = (per_query or {}).get(query, "ads")
        if isinstance(answer, Exception):
            raise answer
        return SearchResult(query=query, country=country, ads=ads() if answer == "ads" else [], attempts=1, misses=0, seconds=0.1, count=5 if answer == "ads" else 0)

    monkeypatch.setattr(lanes, "search", _search)
    return calls


def fake_lookup(monkeypatch, found=True):
    v = classify_page_view(fixture("page_view_ads.html"))[1]

    def _lookup(*, active_status, media_type, pool, page_id=None, facebook_url=None, company_domain=None, **kw):
        r = BrandResult("page_id", str(page_id), active_status, media_type, found=found, page_id=str(page_id) if found else None,
                        count=v.count if found else 0, ads=v.ads if found else None, info=v.info if found else None, attempts=1, seconds=1.0)
        return r

    monkeypatch.setattr(lanes, "lookup", _lookup)


def submit(client, items, **extra):
    return client.post("/jobs", json={"items": items, **extra})


def poll(client, job_id, wait_s=5, **params):
    return client.get(f"/jobs/{job_id}", params={"wait_s": wait_s, **params})


def test_submit_returns_202_and_a_job_id_and_the_poll_returns_results_in_order(client, monkeypatch):
    fake_search(monkeypatch, {"empty one": "none"})
    fake_lookup(monkeypatch)
    r = submit(client, [
        {"id": "kw1-US", "query": "grounding sheets", "country": "US", "maxItems": 30},
        {"id": "kw2-GB", "query": "empty one", "country": "gb"},
        {"id": "b1", "kind": "count", "page_id": "105396194411046"},
    ])
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["items"] == 3 and body["job_id"].startswith("j_") and body["poll"].startswith(f"/jobs/{body['job_id']}")

    p = poll(client, body["job_id"]).json()
    assert p["status"] == "done" and p["counts"] == {"total": 3, "done": 3, "ok": 2, "no_ads": 1, "blocked": 0, "error": 0, "not_found": 0}
    ids = [x["id"] for x in p["results"]]
    assert ids == ["kw1-US", "kw2-GB", "b1"], "submission order, whatever order the lanes finished in"
    s1, s2, c1 = p["results"]
    assert s1["status"] == "ok" and s1["ads_found"] == 5 and s1["query"] == "grounding sheets" and s1["country"] == "US" and s1["items"][0]["page_name"] == "Shakti Mat"
    assert s1["lane"] == "lane-1" and s1["tries"][0]["status"] == "ok"
    assert s2["status"] == "no_ads" and s2["ads_found"] == 0 and s2["country"] == "GB" and s2["reported_total"] == 0
    assert c1["status"] == "ok" and c1["number_of_ads"] == 1783 and c1["page_name"] == "Muscle Mat" and c1["envelope"]["number_of_ads"] == 1783
    assert p["lanes"]["total"] == 1 and api.counters["jobs_submitted"] == 1 and api.counters["job_items"] == 3


def test_a_duplicate_id_an_empty_job_and_a_bad_item_are_400(client, monkeypatch):
    fake_search(monkeypatch)
    assert submit(client, []).status_code == 400
    r = submit(client, [{"id": "a", "query": "x"}, {"id": "a", "query": "y"}])
    assert r.status_code == 400 and "twice" in r.json()["error"]["message"]
    r = submit(client, [{"id": "a", "query": "x", "country": "NZL"}])
    assert r.status_code == 400
    r = submit(client, [{"id": "a", "kind": "count"}])
    assert r.status_code == 400 and "page_id" in r.json()["error"]["message"]
    r = submit(client, [{"id": "a", "kind": "video"}])
    assert r.status_code == 400 and "kind" in r.json()["error"]["message"]


def test_more_than_the_item_cap_is_400(client, monkeypatch):
    fake_search(monkeypatch)
    set_frozen(settings, "job_max_items", 2)
    r = submit(client, [{"id": str(n), "query": "x"} for n in range(3)])
    assert r.status_code == 400 and "cap is 2" in r.json()["error"]["message"]


def test_a_blocked_search_is_reported_blocked_with_its_tries(client, monkeypatch):
    fake_search(monkeypatch, {"walled": ScrapeBlocked("403 no challenge")})
    # The fine one first: a hard block cools the only lane for LANE_COOLDOWN_S, by design.
    r = submit(client, [{"id": "ok", "query": "fine"}, {"id": "w", "query": "walled"}])
    p = poll(client, r.json()["job_id"]).json()
    ok, w = p["results"]
    assert w["status"] == "blocked" and "403" in w["error"] and w["tries"][0]["outcome"] == "blocked" and w["items"] == []
    assert ok["status"] == "ok"
    assert p["counts"]["blocked"] == 1


def test_include_items_0_drops_the_ad_arrays_and_partial_shows_progress(client, monkeypatch):
    fake_search(monkeypatch)
    r = submit(client, [{"id": "a", "query": "x"}])
    p = poll(client, r.json()["job_id"], include_items=0).json()
    assert p["status"] == "done" and p["results"][0]["items"] is None and p["results"][0]["ads_found"] == 5
    p = poll(client, r.json()["job_id"], wait_s=0, partial=1).json()
    assert p["results"] is not None


def test_a_cached_search_in_a_job_uses_no_lane(client, monkeypatch):
    calls = fake_search(monkeypatch)
    client.post("/facebook", json={"query": "warm", "country": "US"})
    assert calls == ["warm"]
    r = submit(client, [{"id": "a", "query": "warm", "country": "US"}])
    p = poll(client, r.json()["job_id"]).json()
    assert p["results"][0]["cached"] is True and p["results"][0]["ads_found"] == 5 and calls == ["warm"]


def test_a_job_result_feeds_the_single_call_cache(client, monkeypatch):
    calls = fake_search(monkeypatch)
    r = submit(client, [{"id": "a", "query": "shared", "country": "US"}])
    poll(client, r.json()["job_id"])
    r = client.post("/facebook", json={"query": "shared", "country": "US"})
    assert r.headers["X-Cache"] == "hit" and calls == ["shared"]


def test_an_unknown_job_is_404_and_a_finished_job_expires_after_its_ttl(client, monkeypatch):
    fake_search(monkeypatch)
    assert poll(client, "j_nope", wait_s=0).status_code == 404
    set_frozen(settings, "job_ttl_s", 0.05)
    r = submit(client, [{"id": "a", "query": "x"}])
    jid = r.json()["job_id"]
    assert poll(client, jid).status_code == 200
    time.sleep(0.1)
    assert poll(client, jid, wait_s=0).status_code == 404


def test_the_store_answers_busy_beyond_its_bound(client, monkeypatch):
    def slow(query, country, max_items, active_status, *, pool, deadline=None, **kw):
        time.sleep(0.5)
        return SearchResult(query=query, country=country, ads=[], attempts=1, misses=0, seconds=0.5, count=0)

    monkeypatch.setattr(lanes, "search", slow)
    set_frozen(settings, "job_store_max", 1)
    first = submit(client, [{"id": "a", "query": "one"}])
    assert first.status_code == 202
    second = submit(client, [{"id": "a", "query": "two"}])
    assert second.status_code == 503 and second.json()["error"]["type"] == "Busy"
    poll(client, first.json()["job_id"])


def test_cancel_marks_the_rest_as_error(client, monkeypatch):
    def slow(query, country, max_items, active_status, *, pool, deadline=None, **kw):
        time.sleep(0.3)
        return SearchResult(query=query, country=country, ads=[], attempts=1, misses=0, seconds=0.3, count=0)

    monkeypatch.setattr(lanes, "search", slow)
    r = submit(client, [{"id": str(n), "query": f"q{n}"} for n in range(4)])
    jid = r.json()["job_id"]
    d = client.delete(f"/jobs/{jid}")
    assert d.status_code == 200
    p = poll(client, jid).json()
    assert p["status"] == "cancelled" and p["counts"]["done"] == 4
    assert any(x["error"] and "cancelled" in x["error"] for x in p["results"])


def test_the_health_reports_jobs_and_lanes(client, monkeypatch):
    fake_search(monkeypatch)
    r = submit(client, [{"id": "a", "query": "x"}])
    poll(client, r.json()["job_id"])
    h = client.get("/health").json()
    assert h["jobs"]["done"] == 1 and h["jobs"]["store"] == 1
    assert h["lanes_summary"]["total"] == 1 and h["lanes"][0]["id"] == "lane-1" and h["lanes"][0]["requests"] == 1
    assert "block_rate_1h" in h and h["doc_id_stale"] is False and h["max_concurrency"] == 1


def test_facebook_and_adyntel_bodies_are_unchanged_and_carry_lane_headers(client, monkeypatch):
    fake_search(monkeypatch)
    fake_lookup(monkeypatch)
    r = client.post("/facebook", json={"maxItems": 80, "query": "acupressure mat", "country": "NZ", "category": "all", "mediaType": "all", "activeStatus": "active", "advertisers": [], "fetchDetails": True})
    assert r.status_code == 200 and isinstance(r.json(), list) and r.json()[0]["snapshot"]["caption"] == "shaktimat.com"
    assert r.headers["X-Status"] == "ok" and r.headers["X-Lane"] == "lane-1" and r.headers["X-Tries"] == "1" and r.headers["X-Ads-Found"] == "5"
    r = client.post("/adyntel", json={"api_key": "k", "email": "e", "page_id": "105396194411046"})
    assert r.status_code == 200 and r.json()["number_of_ads"] == 1783 and r.json()["is_result_complete"] is True
    assert r.headers["X-Status"] == "ok" and r.headers["X-Lane"] == "lane-1"


def test_a_deep_item_pages_on_the_lane_and_never_touches_the_cache(client, monkeypatch):
    """`max_pages` > 1 on a job item is the second pass over a pair the rendered page capped at 30:
    GraphQL on the lane, the cache neither answers it nor keeps its result."""
    fake_search(monkeypatch)
    calls = []

    def _page_search(query, country, status, max_pages, novelty, empty_tol, max_ads, budget_s, cursor, collation, slot=None):
        calls.append({"query": query, "max_pages": max_pages, "novelty": novelty, "empty_tol": empty_tol, "max_ads": max_ads, "budget_s": budget_s})
        return {"ads": ads() * 3, "advertisers": 3, "pages": max_pages, "empty_pages": 0, "stopped_because": "end",
                "decoded_bytes": 500, "seconds": 1.0, "next_cursor": None, "collation": "c", "truncated": False,
                "session": {"label": "s", "requests_made": 1, "minted_now": False}}

    monkeypatch.setattr(lanes, "page_search", _page_search)
    # The rendered pass first: it lands in the cache.
    r = submit(client, [{"id": "a", "query": "forage knife", "country": "CA"}])
    assert poll(client, r.json()["job_id"]).json()["results"][0]["ads_found"] == 5
    # The deep pass for the same pair: paged, not served from the cache, and not written over it.
    r = submit(client, [{"id": "a-deep", "query": "forage knife", "country": "CA", "max_pages": 5, "max_ads": 150, "novelty_stop": 10}])
    assert r.status_code == 202, r.text
    p = poll(client, r.json()["job_id"]).json()
    res = p["results"][0]
    assert res["status"] == "ok" and res["ads_found"] == 15 and res["direct_skipped"] is True and res["cached"] is False
    assert calls == [{"query": "forage knife", "max_pages": 5, "novelty": 10, "empty_tol": 8, "max_ads": 150, "budget_s": calls[0]["budget_s"]}]
    assert 30 <= calls[0]["budget_s"] <= settings.page_budget_s
    # A third, rendered, submission still sees the 5-ad page, not the 15.
    r = submit(client, [{"id": "a2", "query": "forage knife", "country": "CA"}])
    assert poll(client, r.json()["job_id"]).json()["results"][0]["ads_found"] == 5


def test_max_pages_outside_the_ceiling_is_400(client, monkeypatch):
    fake_search(monkeypatch)
    r = submit(client, [{"id": "a", "query": "x", "country": "US", "max_pages": -1}])
    assert r.status_code == 400 and "max_pages" in r.text
    r = submit(client, [{"id": "a", "query": "x", "country": "US", "max_pages": settings.page_max_pages + 1}])
    assert r.status_code == 400


def test_include_items_lite_keeps_only_the_sourcing_fields(client, monkeypatch):
    fake_search(monkeypatch)
    r = submit(client, [{"id": "a", "query": "grounding sheets", "country": "US"}])
    p = poll(client, r.json()["job_id"], include_items="lite").json()
    item = p["results"][0]["items"][0]
    assert item["page_name"] == "Shakti Mat" and item["page_id"] and "snapshot" in item
    assert set(item["snapshot"]).issubset({"caption", "link_url", "title", "link_description", "page_like_count", "page_profile_uri", "page_categories", "body"})
    assert isinstance(item["snapshot"]["body"], dict) and "text" in item["snapshot"]["body"]
    assert "cards" not in item["snapshot"] and "_details" not in item
    full = poll(client, r.json()["job_id"], include_items=1).json()["results"][0]["items"][0]
    assert len(json.dumps(item)) < len(json.dumps(full))
    assert poll(client, r.json()["job_id"], include_items=0).json()["results"][0]["items"] is None
