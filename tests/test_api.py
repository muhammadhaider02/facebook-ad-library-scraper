import json

import pytest
from conftest import fixture, set_frozen
from fastapi.testclient import TestClient

from facebook_ad_library import api
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import DocIdStale, RateLimited, ScrapeBlocked, ScrapeFailed, SearchResult, extract_ads

# Verbatim from the live Stage 0 node `Apify: Facebook Ad Library` (facebook.md §2.1).
STAGE0_BODY = {
    "maxItems": 80,
    "query": "acupressure mat for back pain",
    "country": "NZ",
    "category": "all",
    "mediaType": "all",
    "activeStatus": "active",
    "advertisers": [],
    "fetchDetails": True,
}


def result(**kw) -> SearchResult:
    ads = extract_ads(json.loads(fixture("search_page1.json")))[0]
    base = dict(query="acupressure mat for back pain", country="NZ", ads=ads, pages_fetched=1, pages_failed=0, seconds=3.2)
    base.update(kw)
    return SearchResult(**base)


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def test_post_accepts_the_stage0_body_verbatim_and_returns_apify_shaped_items(client, monkeypatch):
    seen = {}

    def fake_search(query, country, max_items, active_status, *, pool):
        seen.update(query=query, country=country, max_items=max_items, active_status=active_status)
        return result()

    monkeypatch.setattr(api, "search", fake_search)
    r = client.post("/facebook?maxTotalChargeUsd=1", json=STAGE0_BODY)
    assert r.status_code == 200, r.text
    assert seen == {"query": "acupressure mat for back pain", "country": "NZ", "max_items": 80, "active_status": "active"}
    items = r.json()
    assert isinstance(items, list) and len(items) == 3
    assert items[0]["snapshot"]["caption"] == "shaktimat.com" and items[0]["page_name"] == "Shakti Mat"
    assert r.headers["X-Pages-Fetched"] == "1" and r.headers["X-Truncated"] == "false" and r.headers["X-Cache"] == "miss"
    assert api.counters["ok"] == 1 and api.counters["requests"] == 1


def test_post_accepts_plain_names_and_lists(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "search", lambda q, c, m, s, *, pool: seen.update(q=q, c=c, m=m) or result(ads=[]))
    r = client.post("/facebook", json={"q": "running shoes", "countries": ["gb", "US"], "max": "500"})
    assert r.status_code == 200 and r.json() == [] and seen == {"q": "running shoes", "c": "GB", "m": 300}
    assert api.counters["empty"] == 1


def test_400_without_query_or_with_a_bad_country(client, monkeypatch):
    monkeypatch.setattr(api, "search", lambda *a, **k: pytest.fail("should not search"))
    r = client.post("/facebook", json={"country": "NZ"})
    assert r.status_code == 400 and "query" in r.json()["error"]["message"]
    r = client.post("/facebook", json={"query": "x", "country": "NZL"})
    assert r.status_code == 400 and r.json()["error"]["type"] == "ValueError"
    assert api.counters["bad_request"] == 2


@pytest.mark.parametrize(
    "exc, counter",
    [(ScrapeBlocked("refused"), "blocked"), (RateLimited("1675004"), "rate_limited"), (DocIdStale("no data"), "docid_stale"), (ScrapeFailed("net"), "failed")],
)
def test_vendor_failures_are_503_with_the_error_envelope(client, monkeypatch, exc, counter):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(api, "search", boom)
    r = client.post("/facebook", json=STAGE0_BODY)
    assert r.status_code == 503
    body = r.json()["error"]
    assert body["type"] == type(exc).__name__ and body["status"] == 503 and body["message"] == str(exc) and body["description"] == str(exc)
    assert api.counters[counter] == 1 and api.counters["in_flight"] == 0


def test_unexpected_failure_is_500(client, monkeypatch):
    def boom(*a, **k):
        raise KeyError("ad_library_main")

    monkeypatch.setattr(api, "search", boom)
    r = client.post("/facebook", json=STAGE0_BODY)
    assert r.status_code == 500 and r.json()["error"]["type"] == "KeyError" and api.counters["failed"] == 1


def test_bearer_token_enforced(monkeypatch):
    set_frozen(settings, "api_token", "s3cret")
    try:
        with TestClient(api.app) as c:
            r = c.post("/facebook", json=STAGE0_BODY)
            assert r.status_code == 401 and r.json()["error"]["status"] == 401
            r = c.post("/facebook", json=STAGE0_BODY, headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401
            monkeypatch.setattr(api, "search", lambda *a, **k: result(ads=[]))
            r = c.post("/facebook", json=STAGE0_BODY, headers={"Authorization": "Bearer s3cret"})
            assert r.status_code == 200
            assert c.get("/health").status_code == 200  # health stays open
    finally:
        set_frozen(settings, "api_token", "")


def test_identical_request_is_served_from_the_cache(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "search", lambda *a, **k: calls.append(1) or result())
    first = client.post("/facebook", json=STAGE0_BODY)
    second = client.post("/facebook", json={**STAGE0_BODY, "query": "  Acupressure Mat For Back Pain"})
    assert first.json() == second.json() and len(calls) == 1
    assert second.headers["X-Cache"] == "hit" and api.counters["cache_hits"] == 1 and api.counters["ok"] == 2
    # a different size is a different search
    client.post("/facebook", json={**STAGE0_BODY, "maxItems": 30})
    assert len(calls) == 2


def test_truncated_and_partial_results_are_flagged_and_not_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "search", lambda *a, **k: calls.append(1) or result(truncated=True, pages_fetched=2))
    r = client.post("/facebook", json=STAGE0_BODY)
    assert r.status_code == 200 and r.headers["X-Truncated"] == "true" and api.counters["truncated"] == 1
    monkeypatch.setattr(api, "search", lambda *a, **k: calls.append(1) or result(partial=True, pages_failed=1, session_swaps=1))
    r = client.post("/facebook", json=STAGE0_BODY)
    assert r.headers["X-Pages-Failed"] == "1" and r.headers["X-Session-Swaps"] == "1" and api.counters["partial"] == 1
    client.post("/facebook", json=STAGE0_BODY)
    assert len(calls) == 3 and api.counters["cache_hits"] == 0


def test_health_shape(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["auth"] is False and h["proxy"] is False
    for key in ("requests", "ok", "empty", "partial", "truncated", "cache_hits", "bad_request", "blocked", "rate_limited", "docid_stale", "failed", "in_flight"):
        assert key in h
    assert h["sessions"]["live"] == 0 and "sessions_minted" in h["sessions"] and h["cache"]["entries"] == 0
    assert h["doc_id_source"] == "none"
