"""Shape a brand lookup the way the Adyntel API answered `POST /facebook`, so the seven n8n call
sites in `01 · Find The Founder` and `02 · Learn About The Brand` keep their parsing untouched.

What those nodes read (adyntel.md §2.1, verified against the node code on 22 Sep 2026):
  number_of_ads            present = found; absent (`{}`) = not found; the 50+ gate reads it
  is_result_complete       true here always: the count is the page's total, so nothing pages
  continuation_token       null
  platform                 an array (what the vendor sends today; the nodes also accept a string)
  unique_landing_pages     the ownership check; built over the returned results only
  results[]                plain objects (the nodes also unwrap one-element arrays)
    is_active (bool), start_date / end_date (unix seconds, integers), page_name
    snapshot.body.text, title, link_description, cta_text, cta_type, display_format, link_url
    snapshot.videos[].video_sd_url / video_hd_url, snapshot.cards[] with the same names
Added on top: `duration_s` next to every video URL (decoded from the CDN URL's `efg` parameter, which
is what the video node did by hand) and `page_name` / `source` at the top level.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, unquote, urlsplit

from .brand import BrandResult
from .scraper import AD_LIBRARY

SOURCE = "facebook-ad-library"


def duration_s(url: str | None) -> int | None:
    """The clip length Meta's CDN encodes in the `efg` query parameter (URL-encoded base64 JSON
    with `duration_s`), or None when the URL has none."""
    if not url:
        return None
    try:
        raw = parse_qs(urlsplit(url).query).get("efg", [None])[0]
        if not raw:
            return None
        raw = unquote(raw)
        padded = raw + "=" * (-len(raw) % 4)
        value = json.loads(base64.b64decode(padded).decode("utf-8")).get("duration_s")
        return int(value) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _video(v: dict) -> dict:
    sd, hd = v.get("video_sd_url"), v.get("video_hd_url")
    return {
        "video_sd_url": sd,
        "video_hd_url": hd,
        "video_preview_image_url": v.get("video_preview_image_url"),
        "watermarked_video_sd_url": v.get("watermarked_video_sd_url"),
        "watermarked_video_hd_url": v.get("watermarked_video_hd_url"),
        "duration_s": duration_s(sd or hd),
    }


def _card(c: dict) -> dict:
    sd, hd = c.get("video_sd_url"), c.get("video_hd_url")
    return {
        "body": c.get("body"),
        "title": c.get("title"),
        "caption": c.get("caption"),
        "link_description": c.get("link_description"),
        "link_url": c.get("link_url"),
        "cta_text": c.get("cta_text"),
        "cta_type": c.get("cta_type"),
        "video_sd_url": sd,
        "video_hd_url": hd,
        "video_preview_image_url": c.get("video_preview_image_url"),
        "duration_s": duration_s(sd or hd),
        "original_image_url": c.get("original_image_url"),
        "resized_image_url": c.get("resized_image_url"),
    }


def _text(body) -> dict:
    if isinstance(body, dict):
        return {"text": body.get("text")}
    return {"text": body if isinstance(body, str) else None}


def to_result(ad: dict) -> dict:
    snapshot = ad.get("snapshot") or {}
    page_id = ad.get("page_id") or snapshot.get("page_id")
    ad_id = ad.get("ad_archive_id")
    return {
        "ad_archive_id": str(ad_id) if ad_id is not None else None,
        "page_id": str(page_id) if page_id is not None else None,
        "page_name": ad.get("page_name") or snapshot.get("page_name"),
        "is_active": bool(ad.get("is_active")),
        "start_date": _int(ad.get("start_date")),
        "end_date": _int(ad.get("end_date")),
        "publisher_platform": list(ad.get("publisher_platform") or []),
        "collation_count": _int(ad.get("collation_count")),
        "categories": list(ad.get("categories") or []),
        "url": f"{AD_LIBRARY}?id={ad_id}" if ad_id else None,
        "snapshot": {
            "body": _text(snapshot.get("body")),
            "title": snapshot.get("title"),
            "caption": snapshot.get("caption"),
            "link_description": snapshot.get("link_description"),
            "link_url": snapshot.get("link_url"),
            "cta_text": snapshot.get("cta_text"),
            "cta_type": snapshot.get("cta_type"),
            "display_format": snapshot.get("display_format"),
            "page_name": snapshot.get("page_name") or ad.get("page_name"),
            "page_id": str(page_id) if page_id is not None else None,
            "page_profile_uri": snapshot.get("page_profile_uri"),
            "page_profile_picture_url": snapshot.get("page_profile_picture_url"),
            "page_like_count": _int(snapshot.get("page_like_count")),
            "page_categories": list(snapshot.get("page_categories") or []),
            "images": [dict(i) for i in (snapshot.get("images") or []) if isinstance(i, dict)],
            "videos": [_video(v) for v in (snapshot.get("videos") or []) if isinstance(v, dict)],
            "cards": [_card(c) for c in (snapshot.get("cards") or []) if isinstance(c, dict)],
        },
    }


def landing_pages(results: list[dict]) -> list[str]:
    """Every landing URL across the returned results, first seen first, no repeats."""
    seen: list[str] = []
    for r in results:
        s = r.get("snapshot") or {}
        for url in [s.get("link_url")] + [c.get("link_url") for c in s.get("cards") or []]:
            if url and url not in seen:
                seen.append(url)
    return seen


def to_envelope(res: BrandResult, max_results: int = 10) -> dict:
    """The vendor's response body. `{}` for a brand that was not found, which is exactly what the
    vendor sent and what every call site tests for (`number_of_ads` absent)."""
    if not res.found:
        return {}
    results = [to_result(a) for a in (res.ads or [])[: max(1, int(max_results))]]
    platforms = sorted({str(p).lower() for r in results for p in r.get("publisher_platform") or []})
    pages = landing_pages(results)
    return {
        "number_of_ads": int(res.count),
        "is_result_complete": True,
        "continuation_token": None,
        "page_id": res.page_id,
        "page_name": res.page_name,
        "active_status": res.active_status,
        "media_types": [res.media_type],
        "platform": platforms,
        "unique_landing_pages": pages,
        "count_landing_pages": len(pages),
        "results": results,
        "source": SOURCE,
    }
