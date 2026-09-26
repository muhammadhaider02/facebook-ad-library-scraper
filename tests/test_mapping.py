import json

from conftest import fixture

from facebook_ad_library.mapping import to_item
from facebook_ad_library.scraper import classify_page

# Every path the sourcing workflow's extraction node reads, top level and under snapshot.
SOURCING_TOP = {"page_name", "page_id", "page_profile_uri", "page_url", "page_alias", "page_category", "page_like_count", "page_likes", "snapshot", "_details"}
SOURCING_SNAPSHOT = {"caption", "link_url", "page_like_count", "page_categories", "page_profile_uri", "page_alias", "page_name", "page_id"}


def ads():
    return classify_page(fixture("ssr_ads.html"))[1]


def test_item_carries_every_field_sourcing_reads():
    item = to_item(ads()[0], "acupressure mat for back pain", "NZ")
    assert SOURCING_TOP <= set(item) and SOURCING_SNAPSHOT <= set(item["snapshot"])
    assert item["snapshot"]["caption"] == "shaktimat.com" and "shaktimat.com" in item["snapshot"]["link_url"]
    assert item["page_name"] == "Shakti Mat" and item["page_id"] == "775991435791863" and isinstance(item["page_id"], str)
    assert item["page_like_count"] > 200000 and item["page_like_count"] == item["page_likes"] == item["snapshot"]["page_like_count"]
    assert item["page_categories"] == ["Health/beauty"] and item["page_category"] == "Health/beauty"
    assert item["page_profile_uri"] == item["page_url"] == item["snapshot"]["page_profile_uri"]
    assert item["page_alias"] == "" and item["snapshot"]["page_alias"] == ""
    assert item["_details"] is None and "error" not in item
    assert item["query"] == "acupressure mat for back pain" and item["country"] == "NZ"
    assert item["is_active"] is True and "FACEBOOK" in item["publisher_platform"] and item["ad_archive_id"].isdigit()


def test_missing_values_are_null_not_absent():
    item = to_item({"ad_archive_id": 1, "snapshot": {}})
    assert item["page_name"] is None and item["page_id"] is None and item["page_category"] is None
    assert item["page_categories"] == [] and item["snapshot"]["caption"] is None and item["snapshot"]["cards"] == []
    assert item["ad_archive_id"] == "1" and item["publisher_platform"] == []
    assert SOURCING_TOP <= set(item)


def test_link_url_falls_back_to_the_first_card():
    ad = {"snapshot": {"link_url": None, "cards": [{"link_url": None}, {"link_url": "https://shop.example/x", "caption": "shop.example"}]}}
    item = to_item(ad)
    assert item["snapshot"]["link_url"] == "https://shop.example/x"
    assert item["snapshot"]["cards"][1] == {"link_url": "https://shop.example/x", "caption": "shop.example", "title": None, "body": None}


def test_snapshot_fallbacks_fill_top_level_fields():
    item = to_item({"snapshot": {"page_id": 42, "page_name": "Snap Only"}})
    assert item["page_id"] == "42" and item["page_name"] == "Snap Only" and item["snapshot"]["page_id"] == "42"


def test_items_are_json_serialisable():
    assert len(ads()) == 5
    for ad in ads():
        json.dumps(to_item(ad))
