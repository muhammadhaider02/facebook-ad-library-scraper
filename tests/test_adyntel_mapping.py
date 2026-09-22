"""The Adyntel-shaped envelope: what the seven n8n call sites read, typed the way they read it."""

import json

from conftest import fixture

from facebook_ad_library.adyntel_mapping import duration_s, landing_pages, to_envelope, to_result
from facebook_ad_library.brand import BrandResult
from facebook_ad_library.scraper import classify_page_view

# The fields adyntel.md §2.1 lists and the node code reads, verified 22 Sep 2026.
NODE_FIELDS = ("is_active", "start_date", "end_date", "page_name", "snapshot")
SNAPSHOT_FIELDS = ("body", "title", "link_description", "cta_text", "cta_type", "display_format", "link_url", "videos", "cards")
CARD_FIELDS = ("body", "title", "link_description", "link_url", "video_sd_url", "video_hd_url")


def view(name="page_view_ads.html"):
    return classify_page_view(fixture(name))[1]


def found(name="page_view_ads.html", **kw) -> BrandResult:
    v = view(name)
    base = dict(resolver="page_id", query="105396194411046", active_status="active", media_type="all", found=True, page_id="105396194411046", count=v.count, ads=v.ads, info=v.info, attempts=1, seconds=2.1)
    base.update(kw)
    return BrandResult(**base)


def test_duration_is_decoded_from_the_cdn_url_including_percent_encoded_efg():
    ads = view().ads
    urls = [v["video_sd_url"] for a in ads for v in a["snapshot"].get("videos") or []]
    urls += [c["video_sd_url"] for a in ads for c in a["snapshot"].get("cards") or [] if c.get("video_sd_url")]
    assert len(urls) == 5 and all(isinstance(duration_s(u), int) and duration_s(u) > 0 for u in urls)
    assert any("efg=eyJ" in u or "efg=ey" in u for u in urls)
    assert duration_s("https://video.fbcdn.net/x.mp4?oe=1") is None and duration_s(None) is None and duration_s("") is None
    assert duration_s("https://x/y.mp4?efg=" + "eyJkdXJhdGlvbl9zIjo3fQ") == 7  # unpadded base64 of {"duration_s":7}


def test_result_is_a_plain_object_with_the_types_the_nodes_use():
    ads = view().ads
    r = to_result(ads[0])
    assert isinstance(r, dict) and all(k in r for k in NODE_FIELDS) and all(k in r["snapshot"] for k in SNAPSHOT_FIELDS)
    assert r["is_active"] is True and isinstance(r["start_date"], int) and isinstance(r["end_date"], int)
    assert r["snapshot"]["body"] == {"text": ads[0]["snapshot"]["body"]["text"]} and r["page_id"] == "105396194411046"
    assert r["url"] == f"https://www.facebook.com/ads/library/?id={r['ad_archive_id']}"
    dco = [to_result(a) for a in ads if a["snapshot"]["display_format"] == "DCO"][0]
    assert dco["snapshot"]["cards"] and all(k in dco["snapshot"]["cards"][0] for k in CARD_FIELDS)
    assert all(isinstance(c["duration_s"], int) for c in dco["snapshot"]["cards"] if c["video_sd_url"])
    video = [to_result(a) for a in ads if a["snapshot"]["display_format"] == "VIDEO"][0]
    assert video["snapshot"]["videos"][0]["video_sd_url"].startswith("https://") and video["snapshot"]["videos"][0]["duration_s"] > 0
    assert r["snapshot"]["body"]["text"] and json.dumps(r)  # serialisable


def test_envelope_carries_the_total_and_is_always_complete():
    env = to_envelope(found())
    assert env["number_of_ads"] == 1783 and env["is_result_complete"] is True and env["continuation_token"] is None
    assert env["page_id"] == "105396194411046" and env["page_name"] == "Muscle Mat" and env["active_status"] == "active" and env["media_types"] == ["all"]
    assert env["platform"] == ["audience_network", "facebook", "instagram", "messenger"] or env["platform"] == sorted(env["platform"])
    assert all(p == p.lower() for p in env["platform"]) and isinstance(env["platform"], list)
    assert len(env["results"]) == 4 and env["count_landing_pages"] == len(env["unique_landing_pages"]) and env["source"] == "facebook-ad-library"


def test_landing_pages_cover_only_the_returned_results():
    env = to_envelope(found(), max_results=1)
    assert len(env["results"]) == 1 and env["unique_landing_pages"] == landing_pages(env["results"])
    assert env["number_of_ads"] == 1783  # the total does not shrink with the slice
    full = to_envelope(found(), max_results=30)
    assert len(full["unique_landing_pages"]) >= len(env["unique_landing_pages"])
    assert len(set(full["unique_landing_pages"])) == len(full["unique_landing_pages"])


def test_found_with_zero_ads_is_an_envelope_and_not_found_is_an_empty_object():
    zero = found("page_view_zero.html", count=0, ads=[])
    env = to_envelope(zero)
    assert env["number_of_ads"] == 0 and env["results"] == [] and env["unique_landing_pages"] == [] and env["platform"] == []
    assert to_envelope(BrandResult("company_domain", "x.com", "active", "all", found=False, note="no")) == {}
