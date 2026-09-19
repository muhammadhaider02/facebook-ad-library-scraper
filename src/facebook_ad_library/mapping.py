"""Shape one Ad Library ad the way Stage 0's `Extract Dedupe And Filter` node already reads Apify items.

That node reads, in order of preference (facebook.md §2.2):
  page_name (fallback snapshot.page_name)
  page_id (fallback snapshot.page_id, _details.advertiser.page.page_id)
  page_profile_uri / page_url (fallbacks _details…, snapshot.page_profile_uri)
  page_alias (fallback _details…, snapshot.page_alias)
  page_category (fallback _details…, snapshot.page_category, snapshot.page_categories[0])
  page_like_count / page_likes (fallback snapshot.page_like_count - where it actually lives)
  snapshot.caption            <- the bare domain, first choice for the brand's domain
  snapshot.link_url           <- second choice
  _details.advertiser.page.about.text  <- third choice; never fired in production, emitted as null
  error                       <- never set on a data item; failures are HTTP statuses here
Keep those names stable. Every key is present on every item, null when Meta has no value.
"""

from __future__ import annotations


def _first(*values):
    for v in values:
        if v not in (None, "", []):
            return v
    return None


def _card(card: dict) -> dict:
    return {
        "link_url": card.get("link_url"),
        "caption": card.get("caption"),
        "title": card.get("title"),
        "body": card.get("body"),
    }


def to_item(ad: dict, query: str = "", country: str = "") -> dict:
    snapshot = ad.get("snapshot") or {}
    cards = [c for c in (snapshot.get("cards") or []) if isinstance(c, dict)]
    categories = [c for c in (snapshot.get("page_categories") or []) if c]
    page_id = _first(ad.get("page_id"), snapshot.get("page_id"))
    page_name = _first(ad.get("page_name"), snapshot.get("page_name"))
    profile_uri = snapshot.get("page_profile_uri") or None
    like_count = snapshot.get("page_like_count")
    link_url = _first(snapshot.get("link_url"), *(c.get("link_url") for c in cards))
    return {
        "ad_archive_id": str(ad["ad_archive_id"]) if ad.get("ad_archive_id") is not None else None,
        "page_id": str(page_id) if page_id is not None else None,
        "page_name": page_name,
        "page_profile_uri": profile_uri,
        "page_url": profile_uri,
        "page_alias": "",
        "page_category": categories[0] if categories else None,
        "page_categories": categories,
        "page_like_count": like_count,
        "page_likes": like_count,
        "page_is_deleted": ad.get("page_is_deleted"),
        "start_date": ad.get("start_date"),
        "end_date": ad.get("end_date"),
        "is_active": ad.get("is_active"),
        "publisher_platform": list(ad.get("publisher_platform") or []),
        "snapshot": {
            "caption": snapshot.get("caption") or None,
            "link_url": link_url,
            "title": snapshot.get("title") or None,
            "body": snapshot.get("body"),
            "cta_type": snapshot.get("cta_type"),
            "page_like_count": like_count,
            "page_categories": categories,
            "page_profile_uri": profile_uri,
            "page_alias": "",
            "page_name": page_name,
            "page_id": str(page_id) if page_id is not None else None,
            "page_profile_picture_url": snapshot.get("page_profile_picture_url"),
            "cards": [_card(c) for c in cards],
        },
        "_details": None,
        "query": query,
        "country": country,
    }
