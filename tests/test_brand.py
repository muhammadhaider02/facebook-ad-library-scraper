"""lookup(): the resolver ladder, the page view, and the recovery rules on scripted transports."""

import pytest
from conftest import CHALLENGE, Clock, FakeFacebook, fixture, make_pool, page, set_frozen

from facebook_ad_library import scraper as wire
from facebook_ad_library.brand import lookup
from facebook_ad_library.config import settings
from facebook_ad_library.scraper import BudgetExceeded, ResultsMissing, ScrapeBlocked
from facebook_ad_library.session import RateLimiter, Resp, _SessionPool, counters

SHAKTI = "775991435791863"
GYMSHARK = "129669023798560"


def fb(pages=None, plugin=None, profile=None, slug="shaktimats", challenge=True) -> FakeFacebook:
    """Ad Library pages in order (the last repeats), plus optional plugin and profile answers."""
    script = {
        wire.AD_LIBRARY: ([CHALLENGE] if challenge else []) + list(pages or [page("page_view_ads.html")]),
        wire.ORIGIN + "/__rd_verify": [Resp(200, "")],
    }
    if plugin is not None:
        script[wire.PLUGIN_URL] = list(plugin)
    if profile is not None:
        script[f"{wire.ORIGIN}/{slug}"] = list(profile)
    return FakeFacebook(script)


def urls(transport: FakeFacebook) -> list[str]:
    return [c[1] for c in transport.calls if c[0] == "GET"]


# --------------------------------------------------------------------------- by page id


def test_lookup_by_page_id_is_one_page_view_with_the_total():
    t = fb()
    res = lookup(page_id="105396194411046", pool=make_pool([t]))
    assert res.found and res.resolver == "page_id" and res.page_id == "105396194411046"
    assert res.count == 1783 and len(res.ads) == 4 and res.page_name == "Muscle Mat"
    assert res.attempts == 1 and res.misses == 0 and res.plain_gets == 0 and res.session_swaps == 0
    get = t.page_gets[-1][1]
    assert "view_all_page_id=105396194411046" in get and "search_type=page" in get and "active_status=active" in get and "media_type=all" in get


def test_lookup_accepts_an_int_page_id_and_the_status_filter():
    t = fb()
    res = lookup(page_id=105396194411046, active_status="ALL", pool=make_pool([t]))
    assert res.found and res.active_status == "all" and "active_status=all" in t.page_gets[-1][1]


def test_video_lookups_are_always_live_like_the_vendor():
    t = fb([page("page_view_video.html")])
    res = lookup(page_id=SHAKTI, active_status="all", media_type="video", pool=make_pool([t]))
    assert res.found and res.count == 489 and res.active_status == "active" and res.media_type == "video"
    assert "media_type=video" in t.page_gets[-1][1] and "active_status=active" in t.page_gets[-1][1]


def test_unknown_page_id_is_not_found_not_zero():
    res = lookup(page_id="1234", pool=make_pool([fb([page("page_view_unknown.html")])]))
    assert not res.found and res.count == 0 and "does not know" in res.note and res.attempts == 1


def test_known_page_with_no_ads_is_found_with_zero():
    res = lookup(page_id=SHAKTI, pool=make_pool([fb([page("page_view_zero.html")])]))
    assert res.found and res.count == 0 and res.ads == [] and res.page_name == "Muscle Mat"


@pytest.mark.parametrize("kwargs", [{}, {"page_id": "abc"}, {"company_domain": "nodots"}, {"page_id": "", "facebook_url": ""}])
def test_bad_inputs_are_value_errors_before_any_get(kwargs):
    t = fb()
    with pytest.raises(ValueError):
        lookup(pool=make_pool([t]), **kwargs)
    assert t.calls == []


# --------------------------------------------------------------------------- by facebook url


def test_vanity_url_goes_through_the_plugin_then_the_page_view():
    t = fb(plugin=[Resp(200, fixture("plugin_page.html"))])
    res = lookup(facebook_url="https://www.facebook.com/shaktimats/", pool=make_pool([t]))
    assert res.found and res.resolver == "facebook_url" and res.page_id == SHAKTI and res.plain_gets == 1 and res.attempts == 1
    got = urls(t)
    assert got[0] == wire.plugin_url("shaktimats") and f"view_all_page_id={SHAKTI}" in got[-1]
    assert f"{wire.ORIGIN}/shaktimats" not in got  # no profile GET when the plugin answered


def test_urls_that_carry_the_id_skip_the_resolver():
    for url in (f"https://www.facebook.com/p/Shakti-Mat-{SHAKTI}/", f"https://www.facebook.com/pages/Shakti-Mat/{SHAKTI}", f"https://www.facebook.com/profile.php?id={SHAKTI}"):
        t = fb(challenge=False)
        res = lookup(facebook_url=url, pool=make_pool([t]))
        assert res.found and res.page_id == SHAKTI and res.plain_gets == 0 and len(t.page_gets) == 1


def test_unknown_vanity_is_not_found_and_the_profile_is_not_fetched_by_default():
    t = fb(plugin=[Resp(200, fixture("plugin_unknown.html"))], profile=[Resp(200, fixture("profile_page.html"))])
    res = lookup(facebook_url="https://www.facebook.com/shaktimats", pool=make_pool([t]))
    assert not res.found and "no page for facebook.com/shaktimats" in res.note
    assert res.plain_gets == 1 and t.page_gets == []


def test_profile_fallback_reads_delegate_page_when_enabled():
    set_frozen(settings, "brand_profile_fallback", True)
    t = fb(plugin=[Resp(200, fixture("plugin_unknown.html"))], profile=[Resp(200, fixture("profile_page.html"))])
    res = lookup(facebook_url="https://www.facebook.com/shaktimats", pool=make_pool([t]))
    assert res.found and res.page_id == SHAKTI and res.plain_gets == 2
    assert urls(t)[1] == f"{wire.ORIGIN}/shaktimats"


def test_numeric_url_that_is_a_user_id_is_resolved_through_the_plugin():
    t = fb([page("page_view_unknown.html"), page("page_view_ads.html")], plugin=[Resp(200, fixture("plugin_page.html"))])
    res = lookup(facebook_url="https://www.facebook.com/100064593973677/", pool=make_pool([t]))
    assert res.found and res.page_id == SHAKTI and res.attempts == 2 and res.plain_gets == 1
    got = urls(t)
    assert "view_all_page_id=100064593973677" in got[1] and got[2] == wire.plugin_url("100064593973677") and f"view_all_page_id={SHAKTI}" in got[3]


def test_a_link_that_is_not_a_page_is_not_found_without_a_get():
    t = fb()
    res = lookup(facebook_url="https://www.facebook.com/sharer/sharer.php?u=x", pool=make_pool([t]))
    assert not res.found and res.note == "not a Facebook page URL" and t.calls == []


def test_a_wall_on_the_plugin_swaps_sessions_once():
    a = fb(plugin=[Resp(200, fixture("profile_wall.html"))])
    b = fb(plugin=[Resp(200, fixture("plugin_page.html"))])
    res = lookup(facebook_url="https://www.facebook.com/shaktimats", pool=make_pool([a, b]))
    assert res.found and res.session_swaps == 1 and res.plain_gets == 2 and counters["retired_by_reason"] == {"session_dead": 1}
    a2, b2 = fb(plugin=[Resp(200, fixture("profile_wall.html"))]), fb(plugin=[Resp(200, fixture("profile_wall.html"))])
    with pytest.raises(ScrapeBlocked, match="wall"):
        lookup(facebook_url="https://www.facebook.com/shaktimats", pool=make_pool([a2, b2]))


# --------------------------------------------------------------------------- by domain


def test_domain_is_a_keyword_search_then_the_owned_page_view():
    t = fb([page("keyword_mixed_owners.html"), page("page_view_ads.html")])
    res = lookup(company_domain="https://www.gymshark.com/", pool=make_pool([t]))
    assert res.found and res.resolver == "company_domain" and res.page_id == GYMSHARK and res.attempts == 2
    search, view = t.page_gets[-2][1], t.page_gets[-1][1]
    assert "q=gymshark.com" in search and "active_status=all" in search and "country=ALL" in search
    assert f"view_all_page_id={GYMSHARK}" in view and "active_status=active" in view


def test_domain_with_no_ad_landing_on_it_is_not_found_after_one_get():
    t = fb([page("keyword_mixed_owners.html")])
    res = lookup(company_domain="shop.nothing-here.com", pool=make_pool([t]))
    assert not res.found and "lands on it" in res.note and len(t.page_gets) == 2  # challenge re-GET + 1
    t2 = fb([page("ssr_empty.html")])
    res = lookup(company_domain="nothing-here.com", pool=make_pool([t2]))
    assert not res.found and "no ad mentions" in res.note


# --------------------------------------------------------------------------- misses, budget, swaps


def test_misses_are_retried_then_a_503():
    t = fb([page("ssr_miss.html"), page("page_view_ads.html")])
    res = lookup(page_id=SHAKTI, pool=make_pool([t]))
    assert res.found and res.attempts == 2 and res.misses == 1
    set_frozen(settings, "brand_ssr_retries", 1)
    with pytest.raises(ResultsMissing):
        lookup(page_id=SHAKTI, pool=make_pool([fb([page("ssr_miss.html")])]))


def test_budget_refuses_a_second_get_the_limiter_could_not_serve_in_time():
    clock, sleeps = Clock(), []
    sleep = lambda s: (sleeps.append(s), clock.advance(s))  # noqa: E731
    t = fb([page("keyword_mixed_owners.html"), page("page_view_ads.html")])
    pool = _SessionPool(1, 1, lambda: t, RateLimiter(1, clock, sleep), clock, sleep)
    set_frozen(settings, "brand_budget_s", 20)
    with pytest.raises(BudgetExceeded, match="no budget left"):
        lookup(company_domain="gymshark.com", pool=pool)
    assert sleeps == [] and len(t.page_gets) == 2  # the first GET (and its challenge re-GET) was never refused


def test_page_view_dead_session_swaps_once_then_blocks():
    dead = fb([Resp(302, "", {"Location": "/login"})])
    good = fb()
    res = lookup(page_id=SHAKTI, pool=make_pool([dead, good]))
    assert res.found and res.session_swaps == 1
    with pytest.raises(ScrapeBlocked, match="two fresh sessions"):
        lookup(page_id=SHAKTI, pool=make_pool([fb([Resp(302, "")]), fb([Resp(302, "")])]))


# --------------------------------------------------------------------------- the total served unfilled


def short_page(count: int = 0) -> Resp:
    """The Muscle Mat page view with its total rewritten below the four ads it carries."""
    return Resp(200, fixture("page_view_ads.html").replace('"count":1783', f'"count":{count}', 1), {"X-FB-Rd": "0"})


def test_a_total_below_the_ads_on_the_page_is_refetched():
    t = fb([short_page(0), page("page_view_ads.html")])
    res = lookup(page_id="105396194411046", pool=make_pool([t]))
    assert res.found and res.count == 1783 and len(res.ads) == 4
    assert res.attempts == 2 and res.short_counts == 1 and res.misses == 0


def test_a_total_still_short_after_the_retries_is_floored_to_the_ads_on_the_page():
    set_frozen(settings, "brand_ssr_retries", 1)
    res = lookup(page_id="105396194411046", pool=make_pool([fb([short_page(2)])]))
    assert res.found and res.count == 4 and len(res.ads) == 4
    assert res.attempts == 2 and res.short_counts == 2


def test_a_total_equal_to_or_above_the_ads_is_taken_as_served():
    res = lookup(page_id="105396194411046", pool=make_pool([fb([short_page(4)])]))
    assert res.found and res.count == 4 and res.attempts == 1 and res.short_counts == 0
