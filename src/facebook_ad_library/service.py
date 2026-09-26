"""What the single-call endpoints and the batch jobs share: the two result caches, how a request
becomes a lane item, and how an outcome is remembered and shaped for the caller."""

from __future__ import annotations

import time

from .adyntel_mapping import to_envelope
from .brand import BrandResult
from .cache import TTLCache
from .config import settings
from .lanes import Item, Outcome
from .mapping import to_item
from .scraper import normalise_active_status, normalise_country, normalise_media_type, registrable_domain

# One GET answers every size up to the page's 30, so the cache holds the whole mapped page and
# `maxItems` is applied on the way out. The sourcing workflow re-searches an exhausted keyword's remaining
# country slots and retries on error; the cache makes those free.
cache = TTLCache(settings.cache_ttl_s, settings.cache_empty_ttl_s, settings.cache_max_entries)
# Brand lookups: the page view under (page id, status, media), and a domain's resolved page id.
# Short-lived so a re-run measures the site, not the cache; long enough for qualification -> research hand-offs.
brand_cache = TTLCache(settings.adyntel_cache_ttl_s, settings.cache_empty_ttl_s, settings.cache_max_entries)

BY_RESOLVER = {"page_id": "adyntel_by_page_id", "facebook_url": "adyntel_by_url", "company_domain": "adyntel_by_domain"}


# --------------------------------------------------------------------------- searches


def search_params(query, country, active_status) -> tuple[str, str, str]:
    """Validated (query, country, status); ValueError names the fault the way the API always has."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("invalid request: `query` is required")
    return query, normalise_country(country), normalise_active_status(active_status)


def search_item(query: str, country: str, status: str, *, id: str, priority: int, deadline: float, max_ads: int = 0, max_tries: int = 0) -> Item:
    return Item("search", id, priority, deadline, query=query, country=country, status=status, max_ads=max_ads, max_tries=max_tries)


def cached_search(query: str, country: str, status: str) -> list | None:
    return cache.get(TTLCache.key(query, country, status))


def search_items(outcome: Outcome, query: str, country: str) -> list[dict]:
    """The /facebook items for an outcome: the cached page, or the raw ads mapped."""
    if outcome.items is not None:
        return outcome.items
    return [to_item(ad, query, country) for ad in outcome.ads]


LITE_SNAPSHOT = ("caption", "link_url", "title", "link_description", "page_like_count", "page_profile_uri", "page_categories")


def lite_item(item: dict) -> dict:
    """The fields sourcing reads off an ad and nothing else: the page identity, the domain
    carriers and the ad text. A full item is 3-5 KB; a deep job of 200 pairs x 150 ads serialised
    in full is hundreds of MB and killed the 768 MB container (25 Sep 2026)."""
    snap = item.get("snapshot") or {}
    body = snap.get("body")
    text = body.get("text") if isinstance(body, dict) else body
    lite = {k: item.get(k) for k in ("ad_archive_id", "page_id", "page_name", "page_url", "page_profile_uri", "page_alias", "page_category", "page_like_count", "is_active", "start_date")}
    lite["snapshot"] = {**{k: snap.get(k) for k in LITE_SNAPSHOT if k in snap}, "body": {"text": str(text or "")[:400]}}
    return lite


def _pick(*values) -> str:
    for v in values:
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


def _ad_domain(snap: dict) -> str:
    """The landing domain of one ad, the way the sourcing workflow's brand extraction reads it: the `caption`
    when it is a bare host, else the `link_url`; "" when neither carries one."""
    caption = str(snap.get("caption") or "").strip()
    if caption and " " not in caption and "." in caption:
        d = registrable_domain(caption)
        if "." in d:
            return d
    link = str(snap.get("link_url") or "").strip()
    if link:
        d = registrable_domain(link)
        if "." in d:
            return d
    return ""


AD_TEXT_MAX = 400
AD_TEXT_CANDIDATES = 10


def _ad_text(snap: dict) -> tuple[str, str]:
    """The ad's copy as `title | body | link description` (AD_TEXT_MAX chars) and its normalised body,
    the dedupe key: one body under several headlines is one text. A catalog ad's `{{product.name}}`
    placeholder part carries nothing, so it is dropped (18 % of the sourcing workflow's texts on 26 Sep 2026)."""
    body = snap.get("body")
    text = body.get("text") if isinstance(body, dict) else body
    parts = [" ".join(str(x or "").split()) for x in (snap.get("title"), text, snap.get("link_description"))]
    parts = [p for p in parts if p and "{{" not in p]
    key = " ".join(str(text or "").lower().split()) if text and "{{" not in str(text) else " | ".join(parts).lower()
    return " | ".join(parts)[:AD_TEXT_MAX], key


def brand_lines(items: list[dict], texts_per_brand: int = 3) -> list[dict]:
    """The ads of one search grouped by advertiser page and landing domain: one line per group
    with the page identity, the ad count and up to `texts_per_brand` distinct ad texts. This is
    the grouping the sourcing workflow's brand extraction did over every ad; done here, a 900-ad search
    becomes a few dozen lines and n8n never holds the ads (26 Sep 2026). A `no_domain`
    group (domain "") keeps the count of ads that carried no landing domain."""
    groups: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    skipped = 0
    for ad in items:
        if not isinstance(ad, dict) or ad.get("error") or (not ad.get("snapshot") and not ad.get("page_name")):
            skipped += 1
            continue
        snap = ad.get("snapshot") or {}
        page_id = _pick(snap.get("page_id"), ad.get("page_id"))
        domain = _ad_domain(snap)
        key = (page_id, domain)
        g = groups.get(key)
        if g is None:
            cats = snap.get("page_categories")
            g = groups[key] = {
                "page_id": page_id,
                "page_name": _pick(ad.get("page_name"), snap.get("page_name")),
                "page_url": _pick(ad.get("page_url")),
                "page_profile_uri": _pick(snap.get("page_profile_uri"), ad.get("page_profile_uri")),
                "page_alias": _pick(snap.get("page_alias"), ad.get("page_alias")),
                "page_category": _pick(snap.get("page_category"), ad.get("page_category"), cats[0] if isinstance(cats, list) and cats else ""),
                "page_like_count": None, "domain": domain, "ad_count": 0, "ad_texts": [], "_texts": {},
            }
            order.append(key)
        g["ad_count"] += 1
        for v in (ad.get("page_like_count"), snap.get("page_like_count")):
            try:
                n = int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if g["page_like_count"] is None or n > g["page_like_count"]:
                g["page_like_count"] = n
        if len(g["_texts"]) < AD_TEXT_CANDIDATES:
            t, k = _ad_text(snap)
            if t and k not in g["_texts"]:
                g["_texts"][k] = t
        for k, v in (("page_profile_uri", _pick(snap.get("page_profile_uri"), ad.get("page_profile_uri"))),
                     ("page_alias", _pick(snap.get("page_alias"), ad.get("page_alias"))),
                     ("page_category", _pick(snap.get("page_category"), ad.get("page_category"))),
                     ("page_url", _pick(ad.get("page_url")))):
            if not g[k] and v:
                g[k] = v
    lines = [groups[k] for k in order]
    for g in lines:
        # The longest texts say the most about what the brand sells; first-seen order breaks ties.
        texts = list(g.pop("_texts").values())
        g["ad_texts"] = sorted(texts, key=lambda t: -len(t))[:texts_per_brand]
    if skipped:
        lines.append({"page_id": "", "page_name": "", "page_url": "", "page_profile_uri": "", "page_alias": "", "page_category": "",
                      "page_like_count": None, "domain": "", "ad_count": skipped, "ad_texts": [], "skipped": True})
    return lines


def remember_search(outcome: Outcome, query: str, country: str, status: str) -> list[dict]:
    """Map and, for an answer Facebook actually gave, cache. A blocked or errored search is never
    cached: the next caller must re-ask rather than re-read a throttled answer."""
    items = search_items(outcome, query, country)
    if outcome.status in ("ok", "no_ads") and not outcome.cached:
        cache.put(TTLCache.key(query, country, status), items)
    return items


# --------------------------------------------------------------------------- counts


def count_params(page_id, facebook_url, company_domain, active_status, media_type) -> tuple[str, str, str, str]:
    """Validated (resolver, value, status, media) in the API's order of preference."""
    status = normalise_active_status(active_status or "active")
    media = normalise_media_type(media_type or "all")
    if media == "video":
        status = "active"
    page_id = str(page_id).strip() if page_id not in (None, "") else ""
    facebook_url = str(facebook_url).strip() if facebook_url not in (None, "") else ""
    company_domain = str(company_domain).strip() if company_domain not in (None, "") else ""
    if page_id:
        if not page_id.isdigit():
            raise ValueError(f"invalid page_id {page_id!r}: expected digits")
        return "page_id", page_id, status, media
    if facebook_url:
        return "facebook_url", facebook_url, status, media
    if company_domain:
        value = registrable_domain(company_domain)
        if "." not in value:
            raise ValueError(f"invalid company_domain {company_domain!r}")
        return "company_domain", value, status, media
    raise ValueError("invalid request: one of `page_id`, `facebook_url` or `company_domain` is required")


def cached_count(resolver: str, value: str, status: str, media: str) -> tuple[str | None, BrandResult | None, bool]:
    """(page id if known, the cached result if any, whether the id came from the resolve cache)."""
    page_id = value if resolver == "page_id" else None
    resolved = False
    if page_id is None:
        hit = brand_cache.get(("resolve", resolver, value.lower()))
        if hit:
            page_id, resolved = hit, True
    if page_id is not None:
        cached = brand_cache.get(("adyntel", page_id, status, media))
        if isinstance(cached, BrandResult):
            cached.resolver, cached.query = resolver, value
            return page_id, cached, resolved
    return page_id, None, resolved


def count_item(resolver: str, value: str, status: str, media: str, *, page_id: str | None, id: str, priority: int, deadline: float, max_tries: int = 0) -> Item:
    """A lookup item. A domain or URL already resolved to its page id is looked up by that id."""
    kw = {"page_id": page_id} if page_id is not None else {resolver: value}
    return Item("count", id, priority, deadline, status=status, media=media, max_tries=max_tries, **kw)


def remember_count(outcome: Outcome, resolver: str, value: str, status: str, media: str) -> BrandResult | None:
    """Cache what Facebook actually answered. A withheld page (blocked) is never cached."""
    res = outcome.brand
    if res is None or outcome.cached:
        return res
    res.resolver, res.query = resolver, value
    if outcome.status == "ok" and res.found:
        brand_cache.put(("adyntel", res.page_id, status, media), res, settings.adyntel_cache_ttl_s)
        if resolver != "page_id":
            brand_cache.put(("resolve", resolver, value.lower()), res.page_id, settings.cache_ttl_s)
    elif outcome.status == "not_found" and resolver == "page_id":
        # An id Meta does not know stays unknown; a vanity or a domain is not pinned, in case
        # the plugin or the keyword search had an off moment.
        brand_cache.put(("adyntel", value, status, media), res, settings.cache_empty_ttl_s)
    return res


def envelope(res: BrandResult, max_results: int) -> dict:
    return to_envelope(res, max_results)


def now() -> float:
    return time.time()
