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
# `maxItems` is applied on the way out. Stage 0 re-searches an exhausted keyword's remaining
# country slots and retries on error; the cache makes those free.
cache = TTLCache(settings.cache_ttl_s, settings.cache_empty_ttl_s, settings.cache_max_entries)
# Brand lookups: the page view under (page id, status, media), and a domain's resolved page id.
# Short-lived so a re-run measures the site, not the cache; long enough for 01 -> 02 hand-offs.
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
