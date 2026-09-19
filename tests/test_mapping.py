import json

from conftest import fixture

from facebook_ad_library.mapping import to_item
from facebook_ad_library.scraper import extract_ads

# Every path Stage 0's `Extract Dedupe And Filter` reads (facebook.md §2.2), top level and under snapshot.
STAGE0_TOP = {"page_name", "page_id", "page_profile_uri", "page_url", "page_alias", "page_category", "page_like_count", "page_likes", "snapshot", "_details"}
STAGE0_SNAPSHOT = {"caption", "link_url", "page_like_count", "page_categories", "page_profile_uri", "page_alias", "page_name", "page_id"}


def ads():
    return extract_ads(json.loads(fixture("search_page1.json")))[0]


def test_item_carries_every_field_stage0_reads():
    item = to_item(ads()[0], "acupressure mat for back pain", "NZ")
    assert STAGE0_TOP <= set(item) and STAGE0_SNAPSHOT <= set(item["snapshot"])
    assert item["snapshot"]["caption"] == "shaktimat.com" and item["snapshot"]["link_url"].startswith("http://shaktimat.com")
    assert item["page_name"] == "Shakti Mat" and item["page_id"] == "775991435791863" and isinstance(item["page_id"], str)
    assert item["page_like_count"] == 208850 == item["page_likes"] == item["snapshot"]["page_like_count"]
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
    assert STAGE0_TOP <= set(item)


def test_link_url_falls_back_to_the_first_card():
    ad = {"snapshot": {"link_url": None, "cards": [{"link_url": None}, {"link_url": "https://shop.example/x", "caption": "shop.example"}]}}
    item = to_item(ad)
    assert item["snapshot"]["link_url"] == "https://shop.example/x"
    assert item["snapshot"]["cards"][1] == {"link_url": "https://shop.example/x", "caption": "shop.example", "title": None, "body": None}


def test_snapshot_fallbacks_fill_top_level_fields():
    item = to_item({"snapshot": {"page_id": 42, "page_name": "Snap Only"}})
    assert item["page_id"] == "42" and item["page_name"] == "Snap Only" and item["snapshot"]["page_id"] == "42"


def test_items_are_json_serialisable():
    for ad in ads():
        json.dumps(to_item(ad))
