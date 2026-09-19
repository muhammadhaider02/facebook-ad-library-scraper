"""The pure wire functions against saved pages, and search()'s recovery rules on fakes."""

import pytest
from conftest import CHALLENGE, Clock, fixture, make_pool, page, set_frozen, site

from facebook_ad_library import scraper as wire
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import Page, RateLimited, ResultsMissing, ScrapeBlocked, ScrapeFailed, search
from facebook_ad_library.session import Resp, counters

# --------------------------------------------------------------------------- pure functions


@pytest.mark.parametrize(
    "html, expected",
    [
        ("fetch('/__rd_verify_abc?challenge=3',{method:'POST'})", "https://www.facebook.com/__rd_verify_abc?challenge=3"),
        ('fetch("https://www.facebook.com/__rd_verify_x", {method: "POST"})', "https://www.facebook.com/__rd_verify_x"),
        ("<html>nothing here</html>", None),
    ],
)
def test_challenge_url_is_absolute_or_none(html, expected):
    assert wire.challenge_url(html) == expected


def test_challenge_url_from_the_saved_challenge_page():
    url = wire.challenge_url(fixture("challenge_403.html"))
    assert url and url.startswith("https://www.facebook.com/__rd_verify_") and "challenge=3" in url


def test_bootstrap_url_matches_the_site_shape():
    url = wire.bootstrap_url("acupressure mat for back pain", "NZ")
    assert url.startswith(wire.AD_LIBRARY + "?")
    for part in ("active_status=active", "ad_type=all", "country=NZ", "q=acupressure+mat+for+back+pain", "search_type=keyword_unordered", "media_type=all"):
        assert part in url
    assert "active_status=all" in wire.bootstrap_url("x", "US", "ALL")


@pytest.mark.parametrize("raw, expected", [("nz", "NZ"), (" gb ", "GB"), ("all", "ALL"), ("US", "US")])
def test_normalise_country(raw, expected):
    assert wire.normalise_country(raw) == expected


@pytest.mark.parametrize("raw", ["", "NZL", "1A", None])
def test_normalise_country_rejects_garbage(raw):
    with pytest.raises(ValueError):
        wire.normalise_country(raw)


def test_normalise_active_status():
    assert wire.normalise_active_status("Active") == "active" and wire.normalise_active_status(None) == "active"
    with pytest.raises(ValueError):
        wire.normalise_active_status("paused")


def test_app_page_marker_separates_the_ad_library_from_error_pages():
    assert wire.is_app_page(fixture("ssr_ads.html")) and wire.is_app_page(fixture("ssr_miss.html")) and wire.is_app_page(fixture("ssr_empty.html"))
    assert not wire.is_app_page(fixture("html_200.html")) and not wire.is_app_page(fixture("challenge_403.html"))


def test_find_results_reads_the_prefetched_blob():
    conn = wire.find_results(fixture("ssr_ads.html"))
    assert conn is not None and len(conn["edges"]) == 3 and conn["page_info"]["has_next_page"] is True
    assert wire.find_results(fixture("ssr_miss.html")) is None
    assert wire.find_results("") is None


def test_find_results_skips_a_broken_blob():
    html = '<script type="application/json">{"search_results_connection": not json</script>' + fixture("ssr_ads.html")
    assert wire.find_results(html) is not None


def test_extract_ads_flattens_collations_and_dedupes():
    conn = wire.find_results(fixture("ssr_ads.html"))
    ads = wire.extract_ads(conn)
    assert len(ads) == 5 and all(a.get("ad_archive_id") for a in ads)
    assert ads[0]["page_name"] == "Shakti Mat" and ads[0]["snapshot"]["caption"] == "shaktimat.com" and ads[0]["page_id"] == "775991435791863"
    doubled = {"edges": conn["edges"] + conn["edges"]}
    assert len(wire.extract_ads(doubled)) == 5
    assert wire.extract_ads({"edges": [{"node": {"collated_results": [None, "x"]}}, None]}) == []


@pytest.mark.parametrize("name, kind, n", [("ssr_ads.html", Page.ADS, 5), ("ssr_empty.html", Page.EMPTY, 0), ("ssr_miss.html", Page.MISS, 0)])
def test_classify_page_table(name, kind, n):
    page_kind, ads = wire.classify_page(fixture(name))
    assert page_kind is kind and len(ads) == n


def test_page_cost_prices_the_worst_case():
    set_frozen(settings, "spacing_max_s", 5)
    set_frozen(settings, "rate_limit_per_min", 4)
    set_frozen(settings, "request_timeout_s", 30)
    try:
        assert wire.page_cost_s() == 5 + 15 + 30
    finally:
        set_frozen(settings, "spacing_max_s", 0)


# --------------------------------------------------------------------------- search()


def run(pool, **kw):
    return search("acupressure mat for back pain", "NZ", 80, pool=pool, **kw)


def test_search_is_one_get_and_returns_the_pages_ads():
    fb = site()
    r = run(make_pool([fb]))
    assert len(r.ads) == 5 and r.attempts == 1 and r.misses == 0 and r.session_swaps == 0
    assert len(fb.page_gets) == 2  # the challenged GET and the one after it
    assert r.query == "acupressure mat for back pain" and r.country == "NZ"


def test_search_applies_max_items_on_the_way_out():
    r = search("x", "nz", 2, pool=make_pool([site()]))
    assert len(r.ads) == 2 and r.country == "NZ"


def test_search_returns_empty_list_for_a_keyword_with_no_ads():
    r = run(make_pool([site([page("ssr_empty.html")])]))
    assert r.ads == [] and r.attempts == 1 and r.misses == 0


def test_search_retries_a_page_without_results_then_succeeds():
    fb = site([page("ssr_miss.html"), page("ssr_ads.html")])
    r = run(make_pool([fb]))
    assert len(r.ads) == 5 and r.attempts == 2 and r.misses == 1 and counters["misses"] == 1


def test_every_attempt_missing_is_results_missing_not_empty():
    set_frozen(settings, "ssr_retries", 2)
    fb = site([page("ssr_miss.html")])
    with pytest.raises(ResultsMissing, match="3 time"):
        run(make_pool([fb]))
    assert len(fb.page_gets) == 4  # challenge + 3 attempts


def test_search_rejects_bad_input():
    pool = make_pool([site()])
    with pytest.raises(ValueError):
        search("", "US", pool=pool)
    with pytest.raises(ValueError):
        search("x", "NZL", pool=pool)
    with pytest.raises(ValueError):
        search("x", "US", active_status="paused", pool=pool)


def test_budget_never_skips_the_first_get_but_stops_retries(budget):
    set_frozen(settings, "ssr_retries", 5)
    budget(0)  # already expired: the first GET still happens, a retry does not
    fb = site([page("ssr_miss.html")])
    with pytest.raises(ResultsMissing, match="no budget"):
        run(make_pool([fb]))
    assert len(fb.page_gets) == 2


def test_429_sleeps_once_then_retries_the_same_session():
    set_frozen(settings, "rate_limit_sleep_s", 60)
    sleeps: list = []
    fb = site([Resp(429, "slow down"), page()])
    r = run(make_pool([fb], sleeps=sleeps), sleep=sleeps.append)
    assert len(r.ads) == 5 and r.session_swaps == 0 and 60 in sleeps and counters["rate_limited"] == 1


def test_second_429_retires_and_swaps_sessions():
    set_frozen(settings, "rate_limit_sleep_s", 0)
    first = site([Resp(429, "slow down")])
    second = site()
    r = run(make_pool([first, second]), sleep=lambda s: None)
    assert len(r.ads) == 5 and r.session_swaps == 1 and counters["retired_by_reason"] == {"rate_limited": 1}


def test_429_with_no_budget_left_raises(budget):
    set_frozen(settings, "rate_limit_sleep_s", 60)
    budget(0)
    with pytest.raises(RateLimited):
        run(make_pool([site([Resp(429, "slow down")])]), sleep=lambda s: None)


def test_dead_session_swaps_once_then_succeeds():
    dead = site([page("html_200.html")])  # a 200 that is not the Ad Library page
    fresh = site()
    r = run(make_pool([dead, fresh]))
    assert len(r.ads) == 5 and r.session_swaps == 1 and counters["session_dead"] == 1 and counters["sessions_minted"] == 2


def test_two_dead_sessions_in_a_row_is_a_block():
    with pytest.raises(ScrapeBlocked, match="two fresh sessions"):
        run(make_pool([site([Resp(302, "", {"Location": "/login"})])]))


def test_blocked_raises_without_a_swap():
    fb = site([Resp(400, "<title>Sorry, something went wrong</title>")])
    with pytest.raises(ScrapeBlocked, match="TLS"):
        run(make_pool([fb]))
    assert counters["sessions_minted"] == 1


def test_transient_failures_retry_with_backoff_then_swap_then_fail():
    sleeps: list = []
    flaky = site([Resp(503, "upstream")])
    r = run(make_pool([flaky, site()], sleeps=sleeps))
    assert len(r.ads) == 5 and r.session_swaps == 1 and sleeps[:2] == [1.0, 3.0] and counters["transient"] == 3


def test_all_transient_everywhere_is_scrape_failed():
    with pytest.raises(ScrapeFailed):
        run(make_pool([site([Resp(503, "upstream")])]))


def test_unchallenged_session_makes_exactly_one_get():
    fb = site(challenge=False)
    r = run(make_pool([fb]))
    assert len(fb.page_gets) == 1 and len(r.ads) == 5 and counters["challenges"] == 0
