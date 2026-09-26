"""Homepage summaries and fetch jobs (26 Sep 2026): the sourcing workflow's DTC check reads every qualified brand's
homepage as one job with a deadline, and gets a few KB of facts per brand instead of raw HTML."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from facebook_ad_library import api, fetch, service
from facebook_ad_library import jobs as jobs_mod


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


SHOP = """<!doctype html><html lang="en"><head><title>Aeki | Lymphatic Tools</title>
<meta name="description" content="Aeki makes lymphatic drainage tools for your face and body.">
<meta property="og:site_name" content="Aeki">
<script src="https://cdn.shopify.com/s/files/theme.js"></script>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Organization","name":"Aeki"},
{"@type":"Product","name":"Lymphatic Body Brush","brand":{"@type":"Brand","name":"Aeki"},"offers":{"@type":"AggregateOffer","lowPrice":"24.99","priceCurrency":"USD"}}]}</script>
<style>.x{color:red}</style></head><body>
<nav><a href="/">Home</a><a href="/collections/all">Shop</a><a href="/cart">Cart</a></nav>
<h1>Drain the puff, naturally</h1><p>Our body brush is made in-house.</p><p>Our body brush is made in-house.</p>
<a href="/products/body-brush">Body Brush</a><button>Add to cart</button>
<footer>Copyright Aeki</footer></body></html>"""

SAAS = """<html><head><title>Tally</title></head><body><h2>Forms that feel like docs</h2>
<p>Start free, no credit card required. Book a demo.</p><a href="https://www.amazon.com/x">x</a></body></html>"""


def test_page_summary_reads_what_the_shop_publishes():
    s = fetch.page_summary(SHOP, "https://aeki.com", "https://www.aeki.com/")
    assert s["title"] == "Aeki | Lymphatic Tools" and s["description"].startswith("Aeki makes") and s["site_name"] == "Aeki" and s["lang"] == "en"
    assert s["platform"] == "shopify" and s["has_cart"] is True and s["product_links"] == 2
    assert s["products"] == [{"name": "Lymphatic Body Brush", "price": "24.99", "currency": "USD", "brand": "Aeki"}]
    assert "Product" in s["jsonld_types"] and s["brand_names"] == ["Aeki"] and s["headings"] == ["Drain the puff, naturally"]
    assert s["redirected_to"] is None, "www. is the same site"
    assert s["text"].count("made in-house") == 1, "repeated lines once"
    assert "Copyright" not in s["text"] and "Home" not in s["text"], "nav and footer are chrome"
    assert len(json.dumps(s)) < 3000


def test_page_summary_flags_saas_marketplaces_and_redirects():
    s = fetch.page_summary(SAAS, "https://tally.so", "https://linktr.ee/tally")
    assert "book a demo" in s["saas_markers"] and s["marketplace_links"] == ["amazon"]
    assert s["redirected_to"] == "linktr.ee" and s["platform"] is None and s["has_cart"] is False


def test_page_summary_survives_garbage():
    s = fetch.page_summary("<html><body><p>unclosed <div><<<>>> & text", "https://x.com", None)
    assert isinstance(s, dict) and "text" in s


@pytest.mark.parametrize("bad", ["http://localhost:8003/x", "http://127.0.0.1/", "http://10.0.0.5", "http://169.254.169.254/latest",
                                 "http://facebook-ad-library-sourcing:8003/jobs", "ftp://x.com", "", "http://[::1]/"])
def test_validate_public_url_refuses_what_is_not_a_public_site(bad):
    with pytest.raises(ValueError):
        fetch.validate_public_url(bad)


def test_validate_public_url_accepts_a_bare_domain_and_www_variant():
    assert fetch.validate_public_url("brand.co.uk") == "https://brand.co.uk"
    assert fetch.www_variant("https://brand.com/a?b=1") == "https://www.brand.com/a?b=1"
    assert fetch.www_variant("https://www.brand.com") is None


def test_post_fetch_refuses_a_private_address(client):
    assert client.post("/fetch", json={"url": "http://127.0.0.1:8003/health"}).status_code == 400


def fake_pages(monkeypatch, plan):
    """`plan[url]`: "ok" (default), "404", "503", "conn", or ("sleep", seconds) then ok."""
    calls = []

    def _fetch(url, proxy, timeout_s, max_bytes, summarise=False):
        calls.append((url, proxy, timeout_s))
        what = plan.get(url, "ok")
        if isinstance(what, tuple):
            time.sleep(what[1])
            what = "ok"
        base = {"url": url, "ok": False, "status": None, "final_url": url, "bytes": 0, "text": "<html>big</html>", "error": None, "seconds": 0.1}
        if what == "ok":
            base.update(ok=True, status=200, summary={"title": "T " + url})
        elif what == "conn":
            base.update(final_url=None, text="", error="ConnectionError: curl: (6) Could not resolve host")
        else:
            base.update(status=int(what), error=f"http {what}")
        return base

    monkeypatch.setattr(api, "fetch_page", _fetch)
    return calls


def run_fetch_job(client, items, wait_s=10, **extra):
    r = client.post("/jobs", json={"items": items, **extra})
    assert r.status_code == 202, r.text
    return client.get(f"/jobs/{r.json()['job_id']}", params={"wait_s": wait_s}).json()


def test_a_fetch_job_answers_every_item_with_a_summary_and_no_html(client, monkeypatch):
    fake_pages(monkeypatch, {"https://gone.com": "404"})
    p = run_fetch_job(client, [{"id": "a.com", "kind": "fetch", "url": "a.com"}, {"id": "gone.com", "kind": "fetch", "domain": "gone.com"},
                               {"id": "bad", "kind": "fetch", "url": "http://10.1.2.3"}])
    assert p["status"] == "done" and [x["id"] for x in p["results"]] == ["a.com", "gone.com", "bad"]
    a, g, b = p["results"]
    assert a["status"] == "ok" and a["summary"] == {"title": "T https://a.com"} and a["http_status"] == 200 and "text" not in a
    assert g["status"] == "failed" and g["http_status"] == 404 and len(g["tries"]) == 1, "a 404 is not retried"
    assert b["status"] == "failed" and b["error"].startswith("invalid url") and b["tries"] == []
    assert p["counts"] == {"total": 3, "done": 3, "ok": 1, "failed": 2, "timeout": 0, "error": 0}


def test_a_connection_error_retries_on_www_and_a_503_retries_once(client, monkeypatch):
    calls = fake_pages(monkeypatch, {"https://dns.com": "conn", "https://busy.com": "503"})
    p = run_fetch_job(client, [{"id": "d", "kind": "fetch", "url": "dns.com"}, {"id": "b", "kind": "fetch", "url": "busy.com"}])
    d, b = p["results"]
    assert d["status"] == "ok" and [t["url"] for t in d["tries"]] == ["https://dns.com", "https://www.dns.com"]
    assert b["status"] == "failed" and len(b["tries"]) == 2 and b["http_status"] == 503
    assert sum(1 for c in calls if c[0] == "https://busy.com") == 2


def test_a_hanging_page_times_out_at_the_deadline_and_the_rest_answer(client, monkeypatch):
    fake_pages(monkeypatch, {"https://hang.com": ("sleep", 3.0)})
    monkeypatch.setattr(jobs_mod, "_MIN_TRY_S", 0.1)  # a 1 s deadline would otherwise skip every try
    t0 = time.time()
    p = run_fetch_job(client, [{"id": "h", "kind": "fetch", "url": "hang.com"}, {"id": "ok", "kind": "fetch", "url": "fine.com"}], deadline_s=1.0)
    assert time.time() - t0 < 2.9, "the job ends at its deadline, not when the hung try returns"
    h, ok = p["results"]
    assert p["status"] == "done" and h["status"] == "timeout" and ok["status"] == "ok"
    time.sleep(2.3)  # the hung try returns late; its answer must not replace the timeout
    assert api.jobs.get(p["job_id"]).results["h"].status == "timeout"


def test_a_job_mixing_fetches_with_searches_is_400(client):
    r = client.post("/jobs", json={"items": [{"id": "a", "kind": "fetch", "url": "a.com"}, {"id": "b", "query": "x"}]})
    assert r.status_code == 400


def test_brand_lines_drop_catalog_placeholders_and_keep_the_longest_distinct_texts():
    def ad(title, body):
        return {"page_name": "P", "snapshot": {"page_id": "1", "title": title, "body": {"text": body}, "caption": "brand.com", "link_description": ""}}

    long_body = "The same long body about our own sauna " * 3
    items = [ad("{{product.name}}", "{{product.brand}}"), ad("Short", "Buy it"), ad("Headline A", long_body),
             ad("Headline B", long_body), ad("Mid", "A medium body about towels"), ad("x", "y" * 900)]
    [line] = service.brand_lines(items)
    assert line["ad_count"] == 6 and len(line["ad_texts"]) == 3
    assert not any("{{" in t for t in line["ad_texts"]), "placeholders carry nothing"
    assert len(line["ad_texts"][0]) == 400, "capped at 400"
    assert sum(1 for t in line["ad_texts"] if "same long body" in t) == 1, "one body under two headlines is one text"
    assert "Short | Buy it" not in line["ad_texts"], "the longest three win"
