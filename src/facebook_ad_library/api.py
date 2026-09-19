"""HTTP surface n8n calls in place of Apify's run-sync-get-dataset-items endpoint.

POST /facebook  -> JSON array of ad items (the names Stage 0's Extract Dedupe And Filter node reads)
GET  /health    -> counters, useful for Pipeline Health Check

The request body is accepted exactly as Stage 0 sends it to Apify today (`maxItems`, `query`,
`country`, plus fields the actor took and this service ignores), so the n8n change is the URL
and the credential. A 200 is always a complete answer: the page either carries the results or
the search is an error. `200 []` means the Ad Library really shows no ads for that pair.

Error contract, matched to the siblings:
  400 {"error": {...}}  invalid request (no query, bad country)
  401 {"error": {...}}  bad or missing bearer token
  503 {"error": {...}}  we were blocked, rate limited, the session died twice, or every attempt
                        came back without results (ResultsMissing: not an empty list, on purpose)
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
from .cache import TTLCache
from .config import settings
from .mapping import to_item
from .proxy import proxy_url
from .scraper import FacebookError, normalise_active_status, normalise_country, search
from .session import pool

log = logging.getLogger("facebook_ad_library.api")

counters = {
    "requests": 0, "ok": 0, "empty": 0, "retried": 0, "cache_hits": 0,
    "bad_request": 0, "blocked": 0, "rate_limited": 0, "results_missing": 0, "failed": 0, "in_flight": 0,
}
_FAILURE_COUNTER = {"RateLimited": "rate_limited", "ResultsMissing": "results_missing", "ScrapeBlocked": "blocked"}
cache = TTLCache(settings.cache_ttl_s, settings.cache_empty_ttl_s, settings.cache_max_entries)


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
    }
