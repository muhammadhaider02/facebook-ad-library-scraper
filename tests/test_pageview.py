"""The page-view side of the wire protocol: the URL for one advertiser page, its total count and
its page record, and the pure resolvers that turn a vanity URL or a domain into a page id."""

import pytest
from conftest import fixture

from facebook_ad_library import scraper as wire
from facebook_ad_library.scraper import (
    Page,
    SessionDead,
    classify_page_view,
    domain_search_url,
    owned_by,
    page_id_from_plugin,
    page_id_from_profile,
    page_ref,
    page_view_url,
    pick_page,
    plugin_url,
    registrable_domain,
)

SHAKTI = "775991435791863"

# --------------------------------------------------------------------------- urls


def test_page_view_url_is_the_see_all_ads_link():
    url = page_view_url(SHAKTI)
    assert url.startswith(wire.AD_LIBRARY + "?")
    assert "view_all_page_id=775991435791863" in url and "search_type=page" in url and "country=ALL" in url
    assert "active_status=active" in url and "media_type=all" in url
    url = page_view_url(SHAKTI, "ALL", "Video")
    assert "active_status=all" in url and "media_type=video" in url
    with pytest.raises(ValueError):
        page_view_url(SHAKTI, "active", "image")
    with pytest.raises(ValueError):
        page_view_url(SHAKTI, "live")


def test_domain_search_is_a_keyword_search_over_every_country_and_status():
    url = domain_search_url("shaktimat.com")
    assert "q=shaktimat.com" in url and "country=ALL" in url and "active_status=all" in url and "search_type=keyword_unordered" in url


def test_plugin_url_encodes_the_page_url():
    assert plugin_url("shaktimats") == wire.PLUGIN_URL + "?href=https%3A%2F%2Fwww.facebook.com%2Fshaktimats"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.facebook.com/shaktimats", ("slug", "shaktimats")),
        ("https://www.facebook.com/shaktimats/", ("slug", "shaktimats")),
        ("http://m.facebook.com/Gymshark?ref=page_internal", ("slug", "Gymshark")),
        ("facebook.com/Gymshark#about", ("slug", "Gymshark")),
        ("https://www.facebook.com/p/Shakti-Mat-775991435791863/", ("id", SHAKTI)),
        ("https://www.facebook.com/people/Shakti-Mat/775991435791863/", ("id", SHAKTI)),
        ("https://www.facebook.com/pages/Shakti-Mat/775991435791863", ("id", SHAKTI)),
        ("https://www.facebook.com/profile.php?id=100064593973677", ("id", "100064593973677")),
        ("https://www.facebook.com/100080022717415/", ("id", "100080022717415")),
        ("https://www.facebook.com/sharer/sharer.php?u=https://brand.com", None),
        ("https://www.facebook.com/plugins/like.php?href=x", None),
        ("https://www.facebook.com/login/", None),
        ("https://www.facebook.com/tr?id=1", None),
        ("https://www.facebook.com/", None),
        ("https://www.instagram.com/shaktimats", None),
        ("", None),
    ],
)
def test_page_ref_reads_ids_out_of_urls_and_keeps_vanities(url, expected):
    assert page_ref(url) == expected


# --------------------------------------------------------------------------- resolvers on saved pages


def test_plugin_page_yields_the_page_id_and_an_unknown_slug_yields_none():
    assert page_id_from_plugin(fixture("plugin_page.html")) == SHAKTI
    assert page_id_from_plugin(fixture("plugin_unknown.html")) is None


def test_profile_page_yields_the_delegate_page_id_never_the_user_id():
    html = fixture("profile_page.html")
    assert '"userID":"100064593973677"' in html  # the decoy is there
    assert page_id_from_profile(html) == SHAKTI
    assert page_id_from_profile(fixture("profile_unknown.html")) is None


def test_a_wall_is_a_dead_session_not_an_unknown_page():
    with pytest.raises(SessionDead):
        page_id_from_plugin(fixture("profile_wall.html"))
    with pytest.raises(SessionDead):
        page_id_from_profile(fixture("profile_wall.html"))


# --------------------------------------------------------------------------- the page view


def test_page_view_with_ads_carries_the_total_and_the_page_record():
    kind, view = classify_page_view(fixture("page_view_ads.html"))
    assert kind is Page.ADS and view.count == 1783 and len(view.ads) == 4
    assert view.known and view.info["page_name"] == "Example Mat" and view.info["page_is_deleted"] is False
    formats = {a["snapshot"]["display_format"] for a in view.ads}
    assert formats == {"IMAGE", "DCO", "VIDEO"}


def test_page_view_video_filter_page_is_all_video():
    kind, view = classify_page_view(fixture("page_view_video.html"))
    assert kind is Page.ADS and view.count == 489
    assert {a["snapshot"]["display_format"] for a in view.ads} == {"VIDEO"}
    assert all(a["snapshot"]["videos"][0]["video_sd_url"] for a in view.ads)


def test_unknown_page_id_is_told_apart_from_a_page_with_no_ads():
    kind, unknown = classify_page_view(fixture("page_view_unknown.html"))
    assert kind is Page.EMPTY and unknown.count == 0 and unknown.ads == [] and not unknown.known
    kind, zero = classify_page_view(fixture("page_view_zero.html"))
    assert kind is Page.EMPTY and zero.count == 0 and zero.ads == [] and zero.known and zero.info["page_name"] == "Example Mat"


def test_miss_and_keyword_pages():
    assert classify_page_view(fixture("ssr_miss.html")) == (Page.MISS, None)
    kind, view = classify_page_view(fixture("ssr_ads.html"))  # a keyword page: no page record at all
    assert kind is Page.ADS and view.count == 3 and len(view.ads) == 5 and not view.known


# --------------------------------------------------------------------------- ownership


@pytest.mark.parametrize(
    "value, expected",
    [
        ("shop.brand.com", "brand.com"),
        ("https://www.brand.co.uk/collections/x?utm=1", "brand.co.uk"),
        ("ExampleMat.com.au/relaxmat", "examplemat.com.au"),
        ("www.examplemat.co.nz", "examplemat.co.nz"),
        ("eu.gymshark.com", "gymshark.com"),
        ("gymsharkusa.myshopify.com", "myshopify.com"),
        ("https://user:pw@Brand.com:443/", "brand.com"),
        ("instagram.com/shopify1percent", "instagram.com"),
        ("localhost", "localhost"),
        ("", ""),
        (None, ""),
    ],
)
def test_registrable_domain(value, expected):
    assert registrable_domain(value) == expected


def _mixed():
    return wire.extract_ads(wire.find_results(fixture("keyword_mixed_owners.html")))


def test_owned_by_reads_caption_link_and_cards_and_tolerates_a_null_caption():
    ads = _mixed()
    assert len(ads) == 31
    assert sum(owned_by(a, "gymshark.com") for a in ads) == 23  # Gymshark 22 + Gymshark Women 1
    null_caption = [a for a in ads if a["snapshot"]["caption"] is None]
    assert len(null_caption) == 1 and owned_by(null_caption[0], "exampleoutfitters.com") and not owned_by(null_caption[0], "gymshark.com")
    assert not owned_by(ads[0], "")


def test_pick_page_takes_the_page_with_most_ads_landing_on_the_domain():
    ads = _mixed()
    assert pick_page(ads, "gymshark.com") == "129669023798560"
    assert pick_page(ads, "https://www.gymshark.com/") == "129669023798560"
    assert pick_page(ads, "examplegym.com") == "100000000000002"
    assert pick_page(ads, "exampleoutfitters.com") == "100000000000003"
    assert pick_page(ads, "nothing-here.com") is None
    assert pick_page([], "gymshark.com") is None


def _ad(page_id, name, caption, likes=0):
    return {"page_id": page_id, "page_name": name, "snapshot": {"caption": caption, "page_like_count": likes, "cards": []}}


def test_pick_page_ties_go_to_the_name_that_carries_the_domain_then_to_the_larger_page():
    reseller = _ad("1", "Best Deals Outlet", "acme.com", likes=90_000)
    brand = _ad("2", "ACME Store", "acme.com", likes=5_000)
    assert pick_page([reseller, brand], "acme.com") == "2"
    small = _ad("3", "Shop A", "acme.com", likes=10)
    big = _ad("4", "Shop B", "acme.com", likes=1_000)
    assert pick_page([small, big], "acme.com") == "4"
