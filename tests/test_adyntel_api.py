"""POST /adyntel: the vendor's bodies in, the vendor's envelope out, and the two caches behind it."""

import pytest
from conftest import Clock, fixture, set_frozen
from fastapi.testclient import TestClient

from facebook_ad_library import api, lanes
from facebook_ad_library.brand import BrandResult
from facebook_ad_library.cache import TTLCache
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import BudgetExceeded, Busy, ResultsMissing, ScrapeBlocked, ScrapeFailed, classify_page_view

# Verbatim from the live nodes (adyntel.md §3, §4), key and email redacted.
ADY01_BODY = {"api_key": "<redacted>", "email": "subscriptions@ecombench.com", "company_domain": "gymshark.com"}
ADY02_CREATIVE = {"api_key": "<redacted>", "email": "subscriptions@ecombench.com", "company_domain": "gymshark.com", "active_status": "all"}
ADY02_VIDEO = {"api_key": "<redacted>", "email": "subscriptions@ecombench.com", "company_domain": "gymshark.com", "media_type": "video"}
ADY_FB_URL = {"api_key": "<redacted>", "email": "subscriptions@ecombench.com", "facebook_url": "https://www.facebook.com/Gymshark", "active_status": "all"}


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def brand(found=True, **kw) -> BrandResult:
    v = classify_page_view(fixture("page_view_ads.html"))[1]
    base = dict(
        resolver="company_domain", query="gymshark.com", active_status="active", media_type="all", found=found,
        page_id="129669023798560" if found else None, count=v.count if found else 0, ads=v.ads if found else None,
        info=v.info if found else None, attempts=2, seconds=4.2, queue_s=0.0,
    )
    base.update(kw)
    return BrandResult(**base)


def fake_lookup(seen: list, results):
    queue = list(results) if isinstance(results, list) else [results]

    def _lookup(*, active_status, media_type, pool, deadline=None, recovery_pool=None, **kw):
        seen.append({"active_status": active_status, "media_type": media_type, **{k: v for k, v in kw.items() if v is not None}})
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        return r

    return _lookup


def test_accepts_the_01_body_verbatim_and_answers_the_vendor_envelope(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(lanes, "lookup", fake_lookup(seen, brand()))
    r = client.post("/adyntel", json=ADY01_BODY)
    assert r.status_code == 200, r.text
    assert seen == [{"active_status": "active", "media_type": "all", "company_domain": "gymshark.com"}]
    env = r.json()
    assert env["number_of_ads"] == 1783 and env["is_result_complete"] is True and env["continuation_token"] is None
    assert isinstance(env["platform"], list) and len(env["results"]) == 4 and env["results"][0]["snapshot"]["body"]["text"]
    assert r.headers["X-Resolver"] == "company_domain" and r.headers["X-Resolved-Page-Id"] == "129669023798560" and r.headers["X-Found"] == "1"
    assert r.headers["X-Cache"] == "miss" and r.headers["X-Attempts"] == "2"
    assert api.counters["adyntel_requests"] == 1 and api.counters["adyntel_found"] == 1 and api.counters["adyntel_by_domain"] == 1


def test_accepts_the_02_bodies_and_forces_live_for_video(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(lanes, "lookup", fake_lookup(seen, brand()))
    assert client.post("/adyntel", json=ADY02_CREATIVE).status_code == 200
    assert seen[-1] == {"active_status": "all", "media_type": "all", "company_domain": "gymshark.com"}
    api.brand_cache.clear()
    assert client.post("/adyntel", json=ADY02_VIDEO).status_code == 200
    assert seen[-1] == {"active_status": "active", "media_type": "video", "company_domain": "gymshark.com"}
    assert client.post("/adyntel", json=ADY_FB_URL).status_code == 200
    assert seen[-1] == {"active_status": "all", "media_type": "all", "facebook_url": "https://www.facebook.com/Gymshark"}
    assert api.counters["adyntel_by_url"] == 1


def test_page_id_comes_first_and_is_a_string(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(lanes, "lookup", fake_lookup(seen, brand(resolver="page_id")))
    r = client.post("/adyntel", json={"page_id": 775991435791863, "facebook_url": "https://www.facebook.com/x", "company_domain": "x.com"})
    assert r.status_code == 200 and seen == [{"active_status": "active", "media_type": "all", "page_id": "775991435791863"}]
    assert r.headers["X-Resolver"] == "page_id" and api.counters["adyntel_by_page_id"] == 1


def test_not_found_is_an_empty_object_with_200(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(lanes, "lookup", fake_lookup(seen, brand(found=False, note="no ad mentions x.com")))
    r = client.post("/adyntel", json={"company_domain": "x.com"})
    assert r.status_code == 200 and r.json() == {} and r.headers["X-Found"] == "0" and r.headers["X-Resolved-Page-Id"] == ""
    assert api.counters["adyntel_not_found"] == 1 and api.counters["adyntel_found"] == 0


@pytest.mark.parametrize(
    "exc, counter",
    [
        (Busy("all slots taken"), "busy"),
        (BudgetExceeded("no budget"), "budget_exceeded"),
        (ResultsMissing("miss"), "results_missing"),
        (ScrapeBlocked("wall"), "blocked"),
        (ScrapeFailed("boom"), "failed"),
    ],
)
def test_vendor_failures_are_503_with_the_envelope(client, monkeypatch, exc, counter):
    monkeypatch.setattr(lanes, "lookup", fake_lookup([], exc))
    r = client.post("/adyntel", json={"page_id": "775991435791863"})
    assert r.status_code == 503 and r.json()["error"]["type"] == type(exc).__name__ and api.counters[counter] == 1


@pytest.mark.parametrize(
    "body",
    [{}, {"api_key": "k", "email": "e"}, {"page_id": "abc"}, {"company_domain": "nodots"}, {"page_id": "1", "media_type": "image"}, {"page_id": "1", "active_status": "live"}],
)
def test_bad_bodies_are_400_before_any_lookup(client, monkeypatch, body):
    def never(**kw):
        raise AssertionError("lookup must not run")

    monkeypatch.setattr(lanes, "lookup", never)
    r = client.post("/adyntel", json=body)
    assert r.status_code == 400 and r.json()["error"]["status"] == 400 and api.counters["bad_request"] == 1


def test_requires_the_token_like_facebook(client):
    set_frozen(settings, "api_token", "s3cret")
    assert client.post("/adyntel", json={"page_id": "1"}).status_code == 401
    assert client.post("/adyntel", json={"page_id": "1"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_page_view_is_cached_per_status_and_media(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(lanes, "lookup", fake_lookup(seen, brand(resolver="page_id")))
    a = client.post("/adyntel", json={"page_id": "129669023798560"})
    b = client.post("/adyntel", json={"page_id": "129669023798560", "max_results": 2})
    assert a.status_code == b.status_code == 200 and len(seen) == 1
    assert b.headers["X-Cache"] == "hit" and len(b.json()["results"]) == 2 and b.json()["number_of_ads"] == 1783
    client.post("/adyntel", json={"page_id": "129669023798560", "active_status": "all"})
    assert len(seen) == 2 and api.counters["adyntel_cache_hits"] == 1


def test_domain_resolution_is_reused_across_filters(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(lanes, "lookup", fake_lookup(seen, brand()))
    client.post("/adyntel", json=ADY02_CREATIVE)  # resolves gymshark.com -> the page id (status all)
    r = client.post("/adyntel", json=ADY02_VIDEO)  # same brand, another filter: by id, no resolution
    assert r.status_code == 200 and r.headers["X-Resolver"] == "company_domain" and r.headers["X-Cache"] == "miss"
    assert seen[-1] == {"active_status": "active", "media_type": "video", "page_id": "129669023798560"}
    assert api.counters["adyntel_resolve_hits"] == 1
    r = client.post("/adyntel", json=ADY02_CREATIVE)
    assert r.headers["X-Cache"] == "hit" and api.counters["adyntel_cache_hits"] == 1


def test_not_found_by_url_is_not_pinned_but_an_unknown_id_is(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(lanes, "lookup", fake_lookup(seen, brand(found=False)))
    client.post("/adyntel", json={"facebook_url": "https://www.facebook.com/nobody"})
    client.post("/adyntel", json={"facebook_url": "https://www.facebook.com/nobody"})
    assert len(seen) == 2
    client.post("/adyntel", json={"page_id": "1234"})
    r = client.post("/adyntel", json={"page_id": "1234"})
    assert len(seen) == 3 and r.json() == {} and r.headers["X-Cache"] == "hit"


def test_health_reports_the_adyntel_counters_and_the_brand_cache(client):
    h = client.get("/health").json()
    for k in ("adyntel_requests", "adyntel_found", "adyntel_not_found", "adyntel_cache_hits", "adyntel_resolve_hits", "adyntel_by_page_id", "adyntel_by_url", "adyntel_by_domain", "busy", "budget_exceeded"):
        assert k in h
    assert h["brand_cache"] == {"entries": 0, "hits": 0, "misses": 0, "evictions": 0}
    for k in ("plain_calls", "plain_blocked", "plain_dead", "busy"):
        assert k in h["sessions"]


# --------------------------------------------------------------------------- the cache under it


def test_cache_stores_any_value_as_a_copy_and_an_explicit_ttl_wins():
    clock = Clock()
    c = TTLCache(1000, empty_ttl_s=10, clock=clock)
    c.put(("adyntel", "1", "active", "all"), {"number_of_ads": 3})
    c.put(("resolve", "company_domain", "x.com"), "775991435791863")
    got = c.get(("adyntel", "1", "active", "all"))
    assert got == {"number_of_ads": 3}
    got["number_of_ads"] = 99
    assert c.get(("adyntel", "1", "active", "all")) == {"number_of_ads": 3}
    assert c.get(("resolve", "company_domain", "x.com")) == "775991435791863"
    c.put(("k",), {}, ttl=5)  # an empty dict comes back as an empty dict, not None
    assert c.get(("k",)) == {}
    clock.advance(6)
    assert c.get(("k",)) is None
    c.put(("k2",), {"a": 1}, ttl=3)
    clock.advance(4)
    assert c.get(("k2",)) is None


def test_a_withheld_brand_lookup_is_a_503_not_a_found_page_with_no_ads(monkeypatch, client):
    """The retry guard, brand half. Answering "Meta has 1039 ads" with an empty list makes the
    caller reject a brand it cannot see ads for and spend one of its three retries on it."""
    from facebook_ad_library import api as m
    from facebook_ad_library.brand import BrandResult

    res = BrandResult("page_id", "775991435791863", "active", "all")
    res.found, res.page_id, res.count, res.ads = True, "775991435791863", 1039, []
    res.withheld, res.recovery_gets = 1, 1
    monkeypatch.setattr(lanes, "lookup", lambda *a, **k: res)

    r = client.post("/adyntel", json={"page_id": "775991435791863"})
    assert r.status_code == 503
    msg = r.json()["error"]["message"]
    assert "served none" in msg and "on this exit" in msg
    assert r.json()["error"]["kind"] == "blocked" and r.json()["error"]["type"] == "ScrapeBlocked"


def test_a_brand_with_genuinely_no_live_ads_is_still_a_normal_answer(monkeypatch, client):
    """count 0 is an honest zero and must stay a 200, or every quiet brand looks like a failure."""
    from facebook_ad_library import api as m
    from facebook_ad_library.brand import BrandResult

    res = BrandResult("page_id", "1", "active", "all")
    res.found, res.page_id, res.count, res.ads, res.info = True, "1", 0, [], {"page_name": "Quiet"}
    monkeypatch.setattr(lanes, "lookup", lambda *a, **k: res)

    r = client.post("/adyntel", json={"page_id": "1"})
    assert r.status_code == 200 and r.json()["number_of_ads"] == 0
