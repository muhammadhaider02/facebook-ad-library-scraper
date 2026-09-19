import pytest
from conftest import fixture, set_frozen
from fastapi.testclient import TestClient

from facebook_ad_library import api
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import RateLimited, ResultsMissing, ScrapeBlocked, ScrapeFailed, SearchResult, classify_page

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
    ads = classify_page(fixture("ssr_ads.html"))[1]
    base = dict(query="acupressure mat for back pain", country="NZ", ads=ads, attempts=1, misses=0, seconds=3.2)
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
    # the whole page is fetched and cached; maxItems is applied on the way out
    assert seen == {"query": "acupressure mat for back pain", "country": "NZ", "max_items": 300, "active_status": "active"}
    items = r.json()
    assert isinstance(items, list) and len(items) == 5
    assert items[0]["snapshot"]["caption"] == "shaktimat.com" and items[0]["page_name"] == "Shakti Mat"
    assert r.headers["X-Attempts"] == "1" and r.headers["X-Misses"] == "0" and r.headers["X-Cache"] == "miss"
    assert api.counters["ok"] == 1 and api.counters["requests"] == 1


def test_post_accepts_plain_names_and_lists(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "search", lambda q, c, m, s, *, pool: seen.update(q=q, c=c) or result(ads=[]))
    r = client.post("/facebook", json={"q": "running shoes", "countries": ["gb", "US"], "max": "500"})
    assert r.status_code == 200 and r.json() == [] and seen == {"q": "running shoes", "c": "GB"}
    assert api.counters["empty"] == 1


def test_max_items_slices_the_result(client, monkeypatch):
    monkeypatch.setattr(api, "search", lambda *a, **k: result())
    r = client.post("/facebook", json={**STAGE0_BODY, "maxItems": 2})
    assert r.status_code == 200 and len(r.json()) == 2


def test_400_without_query_or_with_a_bad_country(client, monkeypatch):
    monkeypatch.setattr(api, "search", lambda *a, **k: pytest.fail("should not search"))
    r = client.post("/facebook", json={"country": "NZ"})
    assert r.status_code == 400 and "query" in r.json()["error"]["message"]
    r = client.post("/facebook", json={"query": "x", "country": "NZL"})
    assert r.status_code == 400 and r.json()["error"]["type"] == "ValueError"
    r = client.post("/facebook", json={"query": "x", "activeStatus": "paused"})
    assert r.status_code == 400
    assert api.counters["bad_request"] == 3


@pytest.mark.parametrize(
    "exc, counter",
    [(ScrapeBlocked("refused"), "blocked"), (RateLimited("429"), "rate_limited"), (ResultsMissing("no results"), "results_missing"), (ScrapeFailed("net"), "failed")],
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


def test_identical_request_is_served_from_the_cache_whatever_the_size(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "search", lambda *a, **k: calls.append(1) or result())
    first = client.post("/facebook", json=STAGE0_BODY)
    second = client.post("/facebook", json={**STAGE0_BODY, "query": "  Acupressure Mat For Back Pain"})
    assert first.json() == second.json() and len(calls) == 1
    assert second.headers["X-Cache"] == "hit" and api.counters["cache_hits"] == 1 and api.counters["ok"] == 2
    # a different size is the same page, sliced
    r = client.post("/facebook", json={**STAGE0_BODY, "maxItems": 3})
    assert len(calls) == 1 and len(r.json()) == 3 and r.headers["X-Cache"] == "hit"
    # a different status is a different search
    client.post("/facebook", json={**STAGE0_BODY, "activeStatus": "all"})
    assert len(calls) == 2


def test_retried_searches_are_flagged_and_still_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "search", lambda *a, **k: calls.append(1) or result(attempts=2, misses=1, session_swaps=1))
    r = client.post("/facebook", json=STAGE0_BODY)
    assert r.status_code == 200 and r.headers["X-Misses"] == "1" and r.headers["X-Session-Swaps"] == "1" and api.counters["retried"] == 1
    client.post("/facebook", json=STAGE0_BODY)
    assert len(calls) == 1 and api.counters["cache_hits"] == 1


def test_health_shape(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["auth"] is False and h["proxy"] is False and h["version"] == api.__version__
    for key in ("requests", "ok", "empty", "retried", "cache_hits", "bad_request", "blocked", "rate_limited", "results_missing", "failed", "in_flight"):
        assert key in h
    assert h["sessions"]["live"] == 0 and "sessions_minted" in h["sessions"] and "misses" in h["sessions"] and h["cache"]["entries"] == 0
    assert "doc_id" not in h
