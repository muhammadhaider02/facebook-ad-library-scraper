import pytest
from conftest import fixture, set_frozen
from fastapi.testclient import TestClient

from facebook_ad_library import api, lanes
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import RateLimited, ResultsMissing, ScrapeBlocked, ScrapeFailed, SearchResult, classify_page

# The body the sourcing workflow's Apify node sends, verbatim.
SOURCING_BODY = {
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


def test_post_accepts_the_sourcing_body_verbatim_and_returns_apify_shaped_items(client, monkeypatch):
    seen = {}

    def fake_search(query, country, max_items, active_status, *, pool, **kw):
        seen.update(query=query, country=country, max_items=max_items, active_status=active_status)
        return result()

    monkeypatch.setattr(lanes, "search", fake_search)
    r = client.post("/facebook?maxTotalChargeUsd=1", json=SOURCING_BODY)
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
    monkeypatch.setattr(lanes, "search", lambda q, c, m, s, *, pool, **kw: seen.update(q=q, c=c) or result(ads=[]))
    r = client.post("/facebook", json={"q": "running shoes", "countries": ["gb", "US"], "max": "500"})
    assert r.status_code == 200 and r.json() == [] and seen == {"q": "running shoes", "c": "GB"}
    assert api.counters["empty"] == 1


def test_max_items_slices_the_result(client, monkeypatch):
    monkeypatch.setattr(lanes, "search", lambda *a, **k: result())
    r = client.post("/facebook", json={**SOURCING_BODY, "maxItems": 2})
    assert r.status_code == 200 and len(r.json()) == 2


def test_400_without_query_or_with_a_bad_country(client, monkeypatch):
    monkeypatch.setattr(lanes, "search", lambda *a, **k: pytest.fail("should not search"))
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

    monkeypatch.setattr(lanes, "search", boom)
    r = client.post("/facebook", json=SOURCING_BODY)
    assert r.status_code == 503
    body = r.json()["error"]
    assert body["type"] == type(exc).__name__ and body["status"] == 503 and body["message"] == str(exc) and body["description"] == str(exc)
    assert api.counters[counter] == 1 and api.counters["in_flight"] == 0


def test_unexpected_failure_is_500(client, monkeypatch):
    def boom(*a, **k):
        raise KeyError("ad_library_main")

    monkeypatch.setattr(lanes, "search", boom)
    r = client.post("/facebook", json=SOURCING_BODY)
    assert r.status_code == 500 and r.json()["error"]["type"] == "KeyError" and api.counters["failed"] == 1


def test_bearer_token_enforced(monkeypatch):
    set_frozen(settings, "api_token", "s3cret")
    try:
        with TestClient(api.app) as c:
            r = c.post("/facebook", json=SOURCING_BODY)
            assert r.status_code == 401 and r.json()["error"]["status"] == 401
            r = c.post("/facebook", json=SOURCING_BODY, headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401
            monkeypatch.setattr(lanes, "search", lambda *a, **k: result(ads=[]))
            r = c.post("/facebook", json=SOURCING_BODY, headers={"Authorization": "Bearer s3cret"})
            assert r.status_code == 200
            assert c.get("/health").status_code == 200  # health stays open
    finally:
        set_frozen(settings, "api_token", "")


def test_identical_request_is_served_from_the_cache_whatever_the_size(client, monkeypatch):
    calls = []
    monkeypatch.setattr(lanes, "search", lambda *a, **k: calls.append(1) or result())
    first = client.post("/facebook", json=SOURCING_BODY)
    second = client.post("/facebook", json={**SOURCING_BODY, "query": "  Acupressure Mat For Back Pain"})
    assert first.json() == second.json() and len(calls) == 1
    assert second.headers["X-Cache"] == "hit" and api.counters["cache_hits"] == 1 and api.counters["ok"] == 2
    # a different size is the same page, sliced
    r = client.post("/facebook", json={**SOURCING_BODY, "maxItems": 3})
    assert len(calls) == 1 and len(r.json()) == 3 and r.headers["X-Cache"] == "hit"
    # a different status is a different search
    client.post("/facebook", json={**SOURCING_BODY, "activeStatus": "all"})
    assert len(calls) == 2


def test_retried_searches_are_flagged_and_still_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(lanes, "search", lambda *a, **k: calls.append(1) or result(attempts=2, misses=1, session_swaps=1))
    r = client.post("/facebook", json=SOURCING_BODY)
    assert r.status_code == 200 and r.headers["X-Misses"] == "1" and r.headers["X-Session-Swaps"] == "1" and api.counters["retried"] == 1
    client.post("/facebook", json=SOURCING_BODY)
    assert len(calls) == 1 and api.counters["cache_hits"] == 1


def test_health_shape(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["auth"] is False and h["proxy"] is False and h["version"] == api.__version__
    for key in ("requests", "ok", "empty", "retried", "cache_hits", "bad_request", "blocked", "rate_limited", "results_missing", "failed", "in_flight"):
        assert key in h
    assert h["sessions"]["live"] == 0 and "sessions_minted" in h["sessions"] and "misses" in h["sessions"] and h["cache"]["entries"] == 0
    assert "doc_id" not in h


# --------------------------------------------------------------------------- the throttle


def _throttled(monkeypatch, count, fallback_ads=None, proxy="http://p:1"):
    """A rendered page that reports `count` ads and serves none, which is what Meta does to a
    throttled exit: no 403, no 429, just an empty list above a correct total. `proxy=None`
    stands for a lane whose GraphQL recovery cannot run at all."""
    from facebook_ad_library import api as m
    from facebook_ad_library.scraper import ScrapeBlocked, SearchResult

    monkeypatch.setattr(lanes, "search", lambda *a, **k: SearchResult(
        query="kw", country="US", ads=[], attempts=1, misses=0, seconds=0.1, count=count))
    if proxy is None:
        def refused(*a, **k):
            raise ScrapeBlocked("graphql paging needs a proxy")
        monkeypatch.setattr(lanes, "page_search", refused)
    if fallback_ads is not None:
        monkeypatch.setattr(lanes, "page_search", lambda *a, **k: {
            "ads": fallback_ads, "advertisers": len(fallback_ads), "pages": 3, "empty_pages": 0,
            "stopped_because": "Meta dropped the cursor (true end)", "decoded_bytes": 1000,
            "seconds": 5.0, "next_cursor": None, "collation": "c", "truncated": False,
            "session": {"label": "s", "requests_made": 1, "minted_now": False},
        })
    return m


def test_a_withheld_payload_falls_back_to_the_proxy(monkeypatch, client):
    ad = {"ad_archive_id": "1", "page_id": "9", "snapshot": {"page_id": "9", "caption": "brand.com"}}
    _throttled(monkeypatch, count=50001, fallback_ads=[ad])
    r = client.post("/facebook", json={"query": "running shoes", "country": "US"})
    assert r.status_code == 200 and len(r.json()) == 1


def test_a_withheld_payload_is_a_503_not_an_empty_success(monkeypatch, client):
    """The retry guard. Answering "Meta has 1039 ads" with [] makes the caller reject the brand
    and spend one of its three retries on a fault that was never the brand's."""
    _throttled(monkeypatch, count=1039, proxy=None)
    r = client.post("/facebook", json={"query": "running shoes", "country": "US"})
    assert r.status_code == 503
    assert "served none" in r.json()["error"]["message"] and r.json()["error"]["kind"] == "blocked"
    assert r.headers["X-Status"] == "blocked"


def test_a_keyword_with_genuinely_no_ads_is_still_an_empty_200(monkeypatch, client):
    """count 0 is an honest empty answer and must not be confused with a withheld one, or every
    niche keyword pays for the residential exit."""
    called = []
    m = _throttled(monkeypatch, count=0)
    monkeypatch.setattr(lanes, "page_search", lambda *a, **k: called.append(1))
    r = client.post("/facebook", json={"query": "nothing here", "country": "NZ"})
    assert r.status_code == 200 and r.json() == []
    assert not called, "don't proxy what isn't blocked"


def test_the_second_search_skips_the_direct_get_while_throttled(monkeypatch, client):
    """The direct GET costs ~3 s and ~1 MB to be told what the previous search established."""
    from facebook_ad_library import api as m
    from facebook_ad_library.scraper import SearchResult

    searches = []
    ad = {"ad_archive_id": "1", "page_id": "9", "snapshot": {"page_id": "9"}}

    def fake_search(*a, **k):
        searches.append(1)
        return SearchResult(query="kw", country="US", ads=[], attempts=1, misses=0, seconds=0.1, count=500)

    monkeypatch.setattr(lanes, "search", fake_search)
    monkeypatch.setattr(lanes, "page_search", lambda *a, **k: {
        "ads": [ad], "advertisers": 1, "pages": 1, "empty_pages": 0, "stopped_because": "true end",
        "decoded_bytes": 100, "seconds": 1.0, "next_cursor": None, "collation": "c",
        "truncated": False, "session": {"label": "s", "requests_made": 1, "minted_now": False},
    })

    assert client.post("/facebook", json={"query": "a", "country": "US"}).status_code == 200
    assert client.post("/facebook", json={"query": "b", "country": "US"}).status_code == 200
    assert len(searches) == 1, "the second search must not re-make a GET already known to fail"
    assert m.counters["throttle_skipped_direct"] == 1


def test_a_direct_page_with_ads_puts_the_direct_path_back(monkeypatch, client):
    from facebook_ad_library import api as m
    from facebook_ad_library.scraper import SearchResult

    throttle = m.dispatcher.lanes[0].throttle
    ad = {"ad_archive_id": "7", "page_id": "3", "snapshot": {"page_id": "3"}}
    monkeypatch.setattr(lanes, "search", lambda *a, **k: SearchResult(
        query="kw", country="US", ads=[ad], attempts=1, misses=0, seconds=0.1, count=30))
    throttle.seen()
    throttle.clear()  # the shortcut is off; this request goes direct and succeeds

    assert client.post("/facebook", json={"query": "a", "country": "US"}).status_code == 200
    assert not throttle.active(), "a page that carried ads means the throttle has stopped"


def test_the_recovery_pages_to_the_cap_not_to_max_items(monkeypatch, client):
    """`max_items` sizes the response; it must not also stop the paging. Conflating them capped
    every recovery at the caller's item count - 80 ads, reached on page 9 - so the page cap never
    applied. Measured in one production sourcing cycle: every broad keyword stopped at 9 pages on the 80-ad target."""
    from facebook_ad_library import api as m
    from facebook_ad_library.scraper import SearchResult

    seen = {}

    def fake_page_search(query, country, status, max_pages, novelty, empty_tol, max_ads, *a, **k):
        seen["max_pages"], seen["max_ads"] = max_pages, max_ads
        return {"ads": [], "advertisers": 0, "pages": 3, "empty_pages": 0, "stopped_because": "x",
                "decoded_bytes": 10, "seconds": 1.0, "next_cursor": None, "collation": "c",
                "truncated": False, "session": {"label": "s", "requests_made": 1, "minted_now": False}}

    monkeypatch.setattr(lanes, "search", lambda *a, **k: SearchResult(
        query="kw", country="US", ads=[], attempts=1, misses=0, seconds=0.1, count=900))
    monkeypatch.setattr(lanes, "page_search", fake_page_search)

    client.post("/facebook", json={"query": "a", "country": "US", "maxItems": 80})
    assert seen["max_ads"] == 0, "no ad target unless one is asked for; the page cap decides"
