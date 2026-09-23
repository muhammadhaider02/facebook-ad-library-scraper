"""HTTP surface n8n calls in place of two vendors.

POST /facebook  -> JSON array of ad items, in place of Apify's run-sync-get-dataset-items (the
                   names Stage 0's Extract Dedupe And Filter node reads)
POST /adyntel   -> one brand's ads and total ad count, in place of the Adyntel API's /facebook
                   (the envelope the seven call sites in workflows 01 and 02 read)
GET  /health    -> counters, useful for Pipeline Health Check

Both bodies are accepted exactly as the workflows send them to the vendors today (including the
vendor's own fields, ignored here), so the n8n change is the URL and the credential. A 200 is
always a complete answer. `/facebook` answers `200 []` when the Ad Library shows no ads for the
pair; `/adyntel` answers `200 {}` when there is no page for the input, and an envelope with
`number_of_ads: 0` for a page that exists and runs no ads, because the workflows route on that
difference (adyntel.md §2.3).

Error contract, matched to the siblings:
  400 {"error": {...}}  invalid request (no query, bad country, no page id / url / domain)
  401 {"error": {...}}  bad or missing bearer token
  503 {"error": {...}}  we were blocked, rate limited, the session died twice, every attempt came
                        back without results (ResultsMissing: not an empty list, on purpose), the
                        lookup ran out of its budget, or every slot was busy
  500 {"error": {...}}  unexpected failure, logged with a traceback
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .adyntel_mapping import to_envelope
from .brand import BrandResult, lookup
from .cache import TTLCache
from .config import settings
from .mapping import to_item
from .proxy import proxy_url
from .scraper import FacebookError, normalise_active_status, normalise_country, normalise_media_type, registrable_domain, search
from .session import pool

log = logging.getLogger("facebook_ad_library.api")

counters = {
    "requests": 0, "ok": 0, "empty": 0, "retried": 0, "cache_hits": 0,
    "bad_request": 0, "blocked": 0, "rate_limited": 0, "results_missing": 0, "failed": 0, "in_flight": 0,
    # POST /adyntel
    "adyntel_requests": 0, "adyntel_found": 0, "adyntel_not_found": 0, "adyntel_cache_hits": 0, "adyntel_resolve_hits": 0,
    "adyntel_by_page_id": 0, "adyntel_by_url": 0, "adyntel_by_domain": 0, "adyntel_short_counts": 0, "busy": 0, "budget_exceeded": 0,
}
_FAILURE_COUNTER = {
    "RateLimited": "rate_limited", "ResultsMissing": "results_missing", "ScrapeBlocked": "blocked",
    "Busy": "busy", "BudgetExceeded": "budget_exceeded",
}
_BY_RESOLVER = {"page_id": "adyntel_by_page_id", "facebook_url": "adyntel_by_url", "company_domain": "adyntel_by_domain"}
cache = TTLCache(settings.cache_ttl_s, settings.cache_empty_ttl_s, settings.cache_max_entries)
# Brand lookups: the page view under (page id, status, media), and a domain's resolved page id.
# Short-lived so a re-run measures the site, not the cache; long enough for 01 -> 02 hand-offs.
brand_cache = TTLCache(settings.adyntel_cache_ttl_s, settings.cache_empty_ttl_s, settings.cache_max_entries)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_token:
        log.warning("API_TOKEN is not set - the scraper endpoint is unauthenticated")
    # Fail at startup, not on the first call, if the proxy credential is a rotating gateway.
    proxy_url()
    yield


app = FastAPI(title="facebook-ad-library", version=__version__, lifespan=lifespan)


class SearchRequest(BaseModel):
    """Stage 0's Apify body verbatim, plus plain names. Unknown fields (`category`, `mediaType`,
    `advertisers`, `fetchDetails`) are ignored: the actor took them, this service has one mode."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    query: str | None = Field(default=None, validation_alias=AliasChoices("query", "q", "keyword"))
    country: str = Field(default="US", validation_alias=AliasChoices("country", "countries"))
    max_items: int = Field(default=80, validation_alias=AliasChoices("maxItems", "max_items", "max"))
    active_status: str = Field(default="active", validation_alias=AliasChoices("activeStatus", "active_status"))

    @field_validator("country", mode="before")
    @classmethod
    def _first_country(cls, v):
        if isinstance(v, (list, tuple)):
            v = v[0] if v else "US"
        return str(v or "US")

    @field_validator("max_items", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 80
        return max(1, min(n, 300))

    @field_validator("active_status", mode="before")
    @classmethod
    def _status(cls, v):
        return str(v or "active")


class BrandRequest(BaseModel):
    """The Adyntel body verbatim: `company_domain` | `facebook_url` (+ `page_id`, which the vendor
    did not take), `active_status`, `media_type`, `continuation_token`. `api_key`, `email`,
    `webhook_url`, `all_ads` and `country_code` are ignored."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    page_id: str | None = Field(default=None, validation_alias=AliasChoices("page_id", "pageId"))
    facebook_url: str | None = Field(default=None, validation_alias=AliasChoices("facebook_url", "facebookUrl", "page_url"))
    company_domain: str | None = Field(default=None, validation_alias=AliasChoices("company_domain", "companyDomain", "domain"))
    active_status: str = Field(default="active", validation_alias=AliasChoices("active_status", "activeStatus"))
    media_type: str = Field(default="all", validation_alias=AliasChoices("media_type", "mediaType"))
    continuation_token: str | None = Field(default=None, validation_alias=AliasChoices("continuation_token", "continuationToken"))
    max_results: int = Field(default=10, validation_alias=AliasChoices("max_results", "maxResults", "max"))

    @field_validator("page_id", "facebook_url", "company_domain", mode="before")
    @classmethod
    def _text(cls, v):
        s = str(v).strip() if v is not None else ""
        return s or None

    @field_validator("active_status", "media_type", mode="before")
    @classmethod
    def _keep_text(cls, v):
        return str(v).strip() if v not in (None, "") else v

    @field_validator("max_results", mode="before")
    @classmethod
    def _clamp_results(cls, v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 10
        return max(1, min(n, 30))


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not settings.api_token:
        return
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _error_body(exc: Exception, status: int) -> dict:
    return {"error": {"type": type(exc).__name__, "status": status, "message": str(exc), "description": str(exc)}}


@app.exception_handler(HTTPException)
async def _http_error(_, exc: HTTPException):
    # Same envelope as every other failure, so a 401 reads like a 503 to the caller's error check.
    detail = str(exc.detail)
    body = {"error": {"type": "HTTPException", "status": exc.status_code, "message": detail, "description": detail}}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.post("/facebook", dependencies=[Depends(require_token)])
async def facebook(req: SearchRequest):
    counters["requests"] += 1
    try:
        query = str(req.query or "").strip()
        if not query:
            raise ValueError("invalid request: `query` is required")
        country = normalise_country(req.country)
        status = normalise_active_status(req.active_status)
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))

    # One GET answers every size up to the page's 30, so the cache holds the whole page and
    # `maxItems` is applied on the way out.
    key = TTLCache.key(query, country, status)
    cached = cache.get(key)
    if cached is not None:
        items = cached[: req.max_items]
        counters["cache_hits"] += 1
        counters["ok" if items else "empty"] += 1
        log.info("cache hit %r %s -> %d item(s)", query, country, len(items))
        return JSONResponse(content=items, headers={"X-Cache": "hit", "X-Attempts": "0", "X-Scrape-Seconds": "0"})

    started = time.time()
    counters["in_flight"] += 1
    try:
        result = await asyncio.to_thread(search, query, country, 300, status, pool=pool)
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))
    except FacebookError as e:
        name = type(e).__name__
        counters[_FAILURE_COUNTER.get(name, "failed")] += 1
        log.warning("%s %r %s -> %s: %s", e.status, query, country, name, e)
        return JSONResponse(status_code=e.status, content=_error_body(e, e.status))
    except Exception as e:  # noqa: BLE001
        counters["failed"] += 1
        log.exception("unexpected failure searching %r %s", query, country)
        return JSONResponse(status_code=500, content=_error_body(e, 500))
    finally:
        counters["in_flight"] -= 1

    page_items = [to_item(ad, result.query, result.country) for ad in result.ads]
    cache.put(key, page_items)
    items = page_items[: req.max_items]
    counters["ok" if items else "empty"] += 1
    if result.misses:
        counters["retried"] += 1
    log.info(
        "ok %r %s ads=%d attempts=%d misses=%d swaps=%d %.1fs",
        query, country, len(items), result.attempts, result.misses, result.session_swaps, time.time() - started,
    )
    headers = {
        "X-Scrape-Seconds": str(result.seconds),
        "X-Attempts": str(result.attempts),
        "X-Misses": str(result.misses),
        "X-Session-Swaps": str(result.session_swaps),
        "X-Cache": "miss",
    }
    return JSONResponse(content=items, headers=headers)


def _brand_headers(result: BrandResult, cache_state: str) -> dict:
    return {
        "X-Resolver": result.resolver,
        "X-Resolved-Page-Id": result.page_id or "",
        "X-Found": "1" if result.found else "0",
        "X-Scrape-Seconds": str(result.seconds),
        "X-Queue-Seconds": str(result.queue_s),
        "X-Attempts": str(result.attempts),
        "X-Misses": str(result.misses),
        "X-Short-Counts": str(result.short_counts),
        "X-Plain-Gets": str(result.plain_gets),
        "X-Session-Swaps": str(result.session_swaps),
        "X-Cache": cache_state,
    }


@app.post("/adyntel", dependencies=[Depends(require_token)])
async def adyntel(req: BrandRequest):
    counters["adyntel_requests"] += 1
    try:
        status = normalise_active_status(req.active_status or "active")
        media = normalise_media_type(req.media_type or "all")
        if media == "video":
            status = "active"
        if req.page_id:
            resolver, value = "page_id", req.page_id
            if not value.isdigit():
                raise ValueError(f"invalid page_id {value!r}: expected digits")
        elif req.facebook_url:
            resolver, value = "facebook_url", req.facebook_url
        elif req.company_domain:
            resolver, value = "company_domain", registrable_domain(req.company_domain)
            if "." not in value:
                raise ValueError(f"invalid company_domain {req.company_domain!r}")
        else:
            raise ValueError("invalid request: one of `page_id`, `facebook_url` or `company_domain` is required")
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))
    counters[_BY_RESOLVER[resolver]] += 1

    # A domain or URL already resolved to its page id is looked up by that id; the page view of
    # (page id, status, media) is then answered from memory while it lives.
    page_id = value if resolver == "page_id" else None
    if page_id is None:
        resolved = brand_cache.get(("resolve", resolver, value.lower()))
        if resolved:
            page_id = resolved
            counters["adyntel_resolve_hits"] += 1
    if page_id is not None:
        cached = brand_cache.get(("adyntel", page_id, status, media))
        if isinstance(cached, BrandResult):
            counters["adyntel_cache_hits"] += 1
            counters["adyntel_found" if cached.found else "adyntel_not_found"] += 1
            cached.resolver, cached.query = resolver, value
            log.info("cache hit adyntel %s=%s page=%s status=%s media=%s count=%d", resolver, value, page_id, status, media, cached.count)
            return JSONResponse(content=to_envelope(cached, req.max_results), headers=_brand_headers(cached, "hit"))

    started = time.time()
    counters["in_flight"] += 1
    kwargs = {"page_id": page_id} if page_id is not None else {resolver: value}
    try:
        result = await asyncio.to_thread(lookup, active_status=status, media_type=media, pool=pool, **kwargs)
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))
    except FacebookError as e:
        name = type(e).__name__
        counters[_FAILURE_COUNTER.get(name, "failed")] += 1
        log.warning("%s adyntel %s=%s -> %s: %s", e.status, resolver, value, name, e)
        return JSONResponse(status_code=e.status, content=_error_body(e, e.status))
    except Exception as e:  # noqa: BLE001
        counters["failed"] += 1
        log.exception("unexpected failure looking up %s=%s", resolver, value)
        return JSONResponse(status_code=500, content=_error_body(e, 500))
    finally:
        counters["in_flight"] -= 1

    result.resolver, result.query = resolver, value
    if result.found:
        counters["adyntel_found"] += 1
        brand_cache.put(("adyntel", result.page_id, status, media), result, settings.adyntel_cache_ttl_s)
        if resolver != "page_id":
            brand_cache.put(("resolve", resolver, value.lower()), result.page_id, settings.cache_ttl_s)
    else:
        counters["adyntel_not_found"] += 1
        if resolver == "page_id":
            # An id Meta does not know stays unknown; a vanity or a domain is not pinned, in case
            # the plugin or the keyword search had an off moment.
            brand_cache.put(("adyntel", value, status, media), result, settings.cache_empty_ttl_s)
    if result.misses or result.short_counts:
        counters["retried"] += 1
    if result.short_counts:
        counters["adyntel_short_counts"] += 1
    log.info(
        "%s adyntel %s=%s page=%s status=%s media=%s count=%d ads=%d attempts=%d misses=%d short=%d plain=%d swaps=%d queue=%.1fs %.1fs%s",
        "ok" if result.found else "not-found", resolver, value, result.page_id or "-", status, media, result.count,
        len(result.ads or []), result.attempts, result.misses, result.short_counts, result.plain_gets, result.session_swaps, result.queue_s,
        time.time() - started, f" ({result.note})" if result.note else "",
    )
    return JSONResponse(content=to_envelope(result, req.max_results), headers=_brand_headers(result, "miss"))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "auth": bool(settings.api_token),
        "proxy": bool(settings.proxy),
        "max_concurrency": settings.max_concurrency,
        **counters,
        "sessions": pool.snapshot(),
        "cache": cache.stats(),
        "brand_cache": brand_cache.stats(),
    }
