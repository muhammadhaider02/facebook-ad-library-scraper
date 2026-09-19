import json

import pytest
from conftest import Clock, Resp, budget, fixture, json_resp, make_pool, set_frozen, site  # noqa: F401

from facebook_ad_library import scraper as wire
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import (
    DocIdStale,
    Kind,
    RateLimited,
    ScrapeBlocked,
    ScrapeFailed,
    SessionDead,
    Tokens,
    b36,
    bootstrap_url,
    build_form,
    build_variables,
    bundle_urls,
    challenge_url,
    classify,
    discover_doc_id,
    extract_ads,
    extract_tokens,
    normalise_country,
    search,
    strip_prefix,
)

# --------------------------------------------------------------------------- tokens and urls


def test_every_token_pattern_matches_the_saved_page():
    t = extract_tokens(fixture("bootstrap_trimmed.html"))
    assert t.lsd == "FIXTURELSDTOKEN0000000000"
    assert t.jazoest == "22222"
    assert t.rev == "1000000001" and t.spin_r == "1000000001"
    assert t.hsi == "7000000000000000001"
    assert t.spin_t == "1700000000"
    assert t.spin_b == "trunk"
    assert t.haste_session.startswith("20714.HYP:")
    assert t.connection_class == "EXCELLENT"


def test_missing_lsd_is_a_block():
    with pytest.raises(ScrapeBlocked, match="lsd"):
        extract_tokens("<html><title>Ad Library</title><body>nothing here</body></html>")


def test_spin_values_fall_back_when_absent():
    t = extract_tokens('["LSD",[],{"token":"abc"}] "server_revision":42')
    assert t.spin_r == "42" and t.spin_t.isdigit() and t.spin_b == "trunk"


@pytest.mark.parametrize(
    "html, expected",
    [
        (fixture("challenge_403.html"), "https://www.facebook.com/__rd_verify_Q_6hBQR4H5wUZBrfIEa667ftNweIYlqeVIJ_bxuur3yo7LiXyg?challenge=3"),
        ("""<script>fetch("https://www.facebook.com/__rd_verify_abc?challenge=1", {method: 'POST'})</script>""", "https://www.facebook.com/__rd_verify_abc?challenge=1"),
        ("""<a href='/__rd_verify_xyz'>x</a>""", "https://www.facebook.com/__rd_verify_xyz"),
        ("<html>no challenge</html>", None),
    ],
)
def test_challenge_url_is_absolute_or_none(html, expected):
    assert challenge_url(html) == expected


def test_bootstrap_url_matches_the_site_shape():
    url = bootstrap_url("acupressure mat for back pain", "NZ")
    assert url.startswith("https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=NZ&q=acupressure+mat+for+back+pain")
    assert "search_type=keyword_unordered" in url and "media_type=all" in url


@pytest.mark.parametrize("raw, expected", [("nz", "NZ"), (" US ", "US"), ("all", "ALL")])
def test_normalise_country(raw, expected):
    assert normalise_country(raw) == expected


@pytest.mark.parametrize("raw", ["", "USA", "1", "u-s"])
def test_normalise_country_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalise_country(raw)


def test_bundle_urls_and_doc_id_discovery():
    urls = bundle_urls(fixture("bootstrap_trimmed.html"))
    assert len(urls) == 3 and all(u.startswith("https://static.xx.fbcdn.net/rsrc.php/") for u in urls)
    assert discover_doc_id([fixture("bundle_snippet.js")]) == "24922295957467452"
    # the 2025 minifier shape, "use strict" and a different exports variable
    legacy = '__d("AdLibrarySearchPaginationQuery_facebookRelayOperation",[],(function(a,b,c,d,e,f){"use strict";e.exports="1234"}),null);'
    assert discover_doc_id(["nothing", legacy]) == "1234"
    assert discover_doc_id(["nothing"]) is None


def test_bundle_urls_handles_json_escaped_slashes():
    html = r'"https:\/\/static.xx.fbcdn.net\/rsrc.php\/v4\/yA\/r\/abc.js?_nc_x=1"'
    assert bundle_urls(html) == ["https://static.xx.fbcdn.net/rsrc.php/v4/yA/r/abc.js?_nc_x=1"]


# --------------------------------------------------------------------------- form body


@pytest.mark.parametrize("n, s", [(0, "0"), (1, "1"), (9, "9"), (10, "a"), (35, "z"), (36, "10"), (1295, "zz")])
def test_b36(n, s):
    assert b36(n) == s


def tokens() -> Tokens:
    return extract_tokens(fixture("bootstrap_trimmed.html"))


def test_build_form_pins_every_field_in_order():
    variables = build_variables(query="q", country="US", cursor=None, collation_token="c", session_id="s")
    form = build_form(tokens(), "24922295957467452", variables, 11)
    assert tuple(form) == wire.FORM_FIELDS
    assert form["__req"] == "b" and form["lsd"] == "FIXTURELSDTOKEN0000000000" and form["jazoest"] == "22222"
    assert form["__hs"].startswith("20714.") and form["__ccg"] == "EXCELLENT" and form["__spin_b"] == "trunk"
    assert form["__rev"] == form["__spin_r"] == "1000000001" and form["__hsi"] == "7000000000000000001"
    assert form["fb_api_req_friendly_name"] == "AdLibrarySearchPaginationQuery" and form["doc_id"] == "24922295957467452"
    assert form["variables"] == json.dumps(variables, separators=(",", ":"))  # compact, no spaces


def test_build_variables_defaults_and_runtime_keys():
    v = build_variables(query="running shoes", country="NZ", cursor="AQH1", collation_token="col", session_id="sess", first=30, active_status="ALL")
    assert v["queryString"] == "running shoes" and v["countries"] == ["NZ"] and v["country"] == "NZ"
    assert v["cursor"] == "AQH1" and v["collationToken"] == "col" and v["sessionID"] == "sess"
    assert v["first"] == 30 and v["activeStatus"] == "ALL"
    # the keys the live query accepted on 2026-09-19
    for key in ("audienceTimeframe", "fetchPageInfo", "fetchSharedDisclaimers", "searchType", "viewAllPageID", "sortData"):
        assert key in v
    assert v["searchType"] == "KEYWORD_UNORDERED" and v["sortData"] is None


def test_build_variables_merges_extra_but_runtime_keys_win():
    v = build_variables(query="q", country="US", cursor=None, collation_token="c", session_id="s", extra={"audienceTimeframe": "LAST_30_DAYS", "queryString": "ignored", "newKey": 1})
    assert v["audienceTimeframe"] == "LAST_30_DAYS" and v["newKey"] == 1 and v["queryString"] == "q"


def test_variables_extra_reads_env_json():
    set_frozen(settings, "variables_json", '{"audienceTimeframe": "LAST_30_DAYS"}')
    assert wire.variables_extra() == {"audienceTimeframe": "LAST_30_DAYS"}
    set_frozen(settings, "variables_json", "[1]")
    with pytest.raises(ValueError):
        wire.variables_extra()


def test_graphql_headers():
    h = wire.graphql_headers(tokens(), "https://www.facebook.com/ads/library/?q=x")
    assert h["X-FB-LSD"] == "FIXTURELSDTOKEN0000000000" and h["X-FB-Friendly-Name"] == "AdLibrarySearchPaginationQuery"
    assert h["Origin"] == "https://www.facebook.com" and h["Referer"].endswith("q=x") and h["Content-Type"] == "application/x-www-form-urlencoded"


# --------------------------------------------------------------------------- responses


def test_strip_prefix():
    assert strip_prefix('for (;;);{"a":1}') == '{"a":1}'
    assert strip_prefix('{"a":1}') == '{"a":1}'


@pytest.mark.parametrize(
    "status, text, kind",
    [
        (200, fixture("search_page1.json"), Kind.OK),
        (200, "for (;;);" + fixture("search_page1.json"), Kind.OK),
        (200, fixture("rate_limited_1675004.json"), Kind.RATE_LIMITED),
        (200, fixture("data_null.json"), Kind.DATA_NULL),
        (200, '{"data": {"something_else": {}}}', Kind.DATA_NULL),
        (200, fixture("html_200.html"), Kind.HTML),
        (200, "not json at all", Kind.BAD_JSON),
        (200, "[1, 2]", Kind.BAD_JSON),
        (400, "<html>Sorry, something went wrong.</html>", Kind.HTML),
        (403, "<html>forbidden</html>", Kind.HTML),
        (302, "", Kind.HTML),
        (500, "<html>error</html>", Kind.TRANSIENT),
        (502, "", Kind.TRANSIENT),
    ],
)
def test_classify_table(status, text, kind):
    assert classify(status, text)[0] is kind


def test_extract_ads_flattens_collations_and_reads_the_cursor():
    ads, cursor = extract_ads(json.loads(fixture("search_page1.json")))
    assert len(ads) == 3 and cursor == "AQHRfixturecursor1"
    assert ads[0]["snapshot"]["caption"] == "shaktimat.com" and ads[0]["page_name"] == "Shakti Mat"
    ads, cursor = extract_ads(json.loads(fixture("search_page2_last.json")))
    assert len(ads) == 3 and cursor is None
    assert extract_ads(json.loads(fixture("search_empty.json"))) == ([], None)


def test_error_summary():
    body = json.loads(fixture("rate_limited_1675004.json"))
    assert wire.error_summary(body).startswith("1675004: Rate limit exceeded")
    assert wire.error_summary(None) == "" and wire.error_summary({"data": None}) == ""


# --------------------------------------------------------------------------- search orchestration


def run(pool, sleeps=None, **kw):
    kw.setdefault("query", "acupressure mat for back pain")
    kw.setdefault("country", "NZ")
    kw.setdefault("max_items", 80)
    rec = sleeps if sleeps is not None else []
    return search(pool=pool, sleep=lambda s: rec.append(s), **kw)


def test_search_walks_pages_until_the_cursor_ends():
    fb = site()
    result = run(make_pool([fb]))
    assert result.pages_fetched == 2 and len(result.ads) == 6 and not result.truncated and not result.partial
    assert result.country == "NZ" and result.session_swaps == 0
    calls = fb.graphql_calls
    assert len(calls) == 2
    v1, v2 = (json.loads(c[2]["variables"]) for c in calls)
    assert v1["cursor"] is None and v2["cursor"] == "AQHRfixturecursor1"
    assert v1["collationToken"] == v2["collationToken"] and v1["sessionID"] == v2["sessionID"]
    assert calls[0][2]["__req"] == "1" and calls[1][2]["__req"] == "2"
    assert calls[0][3]["X-FB-LSD"] == "FIXTURELSDTOKEN0000000000"


def test_search_stops_at_max_items():
    fb = site([json_resp("search_page1.json")] * 5)
    result = run(make_pool([fb]), max_items=2)
    assert len(result.ads) == 2 and result.pages_fetched == 1


def test_search_dedupes_repeated_ads_across_pages():
    fb = site([json_resp("search_page1.json"), json_resp("search_page1.json"), json_resp("search_page2_last.json")])
    result = run(make_pool([fb]))
    assert result.pages_fetched == 3 and len(result.ads) == 6  # page 2 repeated page 1's three ads


def test_search_stops_at_max_pages():
    set_frozen(settings, "max_pages", 2)
    fb = site([json_resp("search_page1.json")] * 5)
    result = run(make_pool([fb]))
    assert result.pages_fetched == 2 and not result.truncated  # a cap, not a deadline


def test_search_returns_empty_list_for_a_keyword_with_no_ads():
    fb = site([json_resp("search_empty.json")])
    result = run(make_pool([fb]))
    assert result.ads == [] and result.pages_fetched == 1


def test_search_rejects_bad_input():
    pool = make_pool([site()])
    with pytest.raises(ValueError):
        run(pool, query="  ")
    with pytest.raises(ValueError):
        run(pool, country="USA")
    with pytest.raises(ValueError):
        run(pool, active_status="paused")


def test_budget_truncates_before_starting_a_page_it_cannot_finish(budget):
    budget(1)  # far below one page's worst case
    fb = site([json_resp("search_page1.json")] * 5)
    result = run(make_pool([fb]))
    assert result.pages_fetched == 1 and result.truncated and len(result.ads) == 3


def test_budget_never_skips_the_first_page(budget):
    budget(0)
    fb = site()
    result = run(make_pool([fb]))
    assert result.pages_fetched == 1 and len(result.ads) == 3


def test_rate_limit_sleeps_once_then_retries_the_same_page():
    set_frozen(settings, "rate_limit_sleep_s", 60)
    fb = site([json_resp("rate_limited_1675004.json"), json_resp("search_page1.json"), json_resp("search_page2_last.json")])
    sleeps = []
    result = run(make_pool([fb]), sleeps=sleeps)
    assert sleeps == [60] and result.pages_fetched == 2 and len(result.ads) == 6 and result.session_swaps == 0
    cursors = [json.loads(c[2]["variables"])["cursor"] for c in fb.graphql_calls]
    assert cursors == [None, None, "AQHRfixturecursor1"]


def test_second_rate_limit_retires_and_swaps_sessions():
    set_frozen(settings, "rate_limit_sleep_s", 60)
    throttled = site([json_resp("rate_limited_1675004.json")])
    fresh = site()
    sleeps = []
    result = run(make_pool([throttled, fresh]), sleeps=sleeps)
    assert sleeps == [60] and result.session_swaps == 1 and len(result.ads) == 6
    assert len(throttled.graphql_calls) == 2 and len(fresh.graphql_calls) == 2


def test_rate_limit_with_no_budget_left_raises(budget):
    budget(5)
    set_frozen(settings, "rate_limit_sleep_s", 60)
    fb = site([json_resp("rate_limited_1675004.json")])
    with pytest.raises(RateLimited):
        run(make_pool([fb]))


def test_dead_session_on_page_one_swaps_and_restarts():
    dead = site([Resp(200, fixture("html_200.html"))])
    fresh = site()
    result = run(make_pool([dead, fresh]))
    assert result.session_swaps == 1 and result.pages_fetched == 2 and len(result.ads) == 6
    assert json.loads(fresh.graphql_calls[0][2]["variables"])["cursor"] is None


def test_two_dead_sessions_in_a_row_is_a_block():
    with pytest.raises(ScrapeBlocked, match="two fresh sessions"):
        run(make_pool([site([Resp(200, fixture("html_200.html"))]), site([Resp(403, "<html>nope</html>")])]))


def test_failure_after_a_good_page_returns_the_ads_in_hand():
    fb = site([json_resp("search_page1.json"), Resp(200, fixture("html_200.html"))])
    result = run(make_pool([fb]))
    assert result.partial and result.pages_failed == 1 and result.pages_fetched == 1 and len(result.ads) == 3
    assert result.session_swaps == 0


def test_stale_doc_id_twice_raises_docid_stale():
    with pytest.raises(DocIdStale):
        run(make_pool([site([json_resp("data_null.json")]), site([json_resp("data_null.json")])]))


def test_transient_errors_retry_with_backoff_then_fail():
    fb = site([Resp(502, ""), Resp(503, ""), json_resp("search_page1.json"), json_resp("search_page2_last.json")])
    sleeps = []
    pool = make_pool([fb], sleeps=sleeps)
    result = run(pool)
    assert sleeps == [1.0, 3.0] and result.pages_fetched == 2
    always_down = site([Resp(500, "")])
    fb2 = site([Resp(500, "")])
    with pytest.raises(ScrapeFailed):
        run(make_pool([always_down, fb2]))


def test_page_cost_prices_the_worst_case():
    set_frozen(settings, "spacing_max_s", 5)
    set_frozen(settings, "rate_limit_per_min", 4)
    set_frozen(settings, "request_timeout_s", 30)
    assert wire.page_cost_s() == 50
