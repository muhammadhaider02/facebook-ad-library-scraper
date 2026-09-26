"""HTTP surface n8n calls in place of two vendors, and since 0.4.0 the batch jobs.

POST /facebook  -> JSON array of ad items, in place of Apify's run-sync-get-dataset-items (the
                   names the sourcing workflow's extraction node reads)
POST /adyntel   -> one brand's ads and total ad count, in place of the Adyntel API's /facebook
                   (the envelope the seven call sites in the qualification and research
                   workflows read)
POST /jobs      -> a whole sourcing run at once (searches and counts); GET /jobs/{id} long-polls
                   for the results paired back by id; DELETE /jobs/{id} cancels what is queued
GET  /health    -> counters, the lanes and their exits, the jobs; unauthenticated

Both vendor bodies are accepted exactly as the workflows send them (including the vendor's own
fields, ignored here), so the n8n change is the URL and the credential. A 200 is always a complete
answer. `/facebook` answers `200 []` when the Ad Library shows no ads for the pair; `/adyntel`
answers `200 {}` when there is no page for the input, and an envelope with `number_of_ads: 0` for
a page that exists and runs no ads, because the workflows route on that difference.

Every request to Facebook runs on a lane (lanes.py): single calls jump the queue ahead of batch
items, brand lookups first. Nothing leaves on this host's own address.

Error contract, matched to the siblings:
  400 {"error": {...}}  invalid request (no query, bad country, no page id / url / domain)
  401 {"error": {...}}  bad or missing bearer token
  503 {"error": {...}}  blocked on every lane that could take it, rate limited, results missing,
                        the budget ran out, or no lane was free in time; `error.kind` says
                        `blocked` or `error`
  500 {"error": {...}}  unexpected failure, logged with a traceback
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from . import __version__, service
from .brand import BrandResult
from .config import settings
from .fetch import fetch_page, validate_public_url
from .jobs import JobStore, job_payload
from .lanes import Dispatcher, Item, Outcome, build_dispatcher
from .scraper import Busy, FacebookError
from .service import brand_cache, cache  # noqa: F401  (re-exported: tests and the CLI clear them here)
from .session import counters as session_counters

log = logging.getLogger("facebook_ad_library.api")


class _Counters(dict):
    """The request counters, safe to bump from lane workers as well as the event loop."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._lock = threading.Lock()

    def bump(self, name: str, by: int = 1) -> None:
        with self._lock:
            self[name] = self.get(name, 0) + by


counters = _Counters({
    "requests": 0, "ok": 0, "empty": 0, "retried": 0, "cache_hits": 0,
    "bad_request": 0, "blocked": 0, "rate_limited": 0, "results_missing": 0, "failed": 0, "in_flight": 0,
    # POST /adyntel
    "adyntel_requests": 0, "adyntel_found": 0, "adyntel_not_found": 0, "adyntel_cache_hits": 0, "adyntel_resolve_hits": 0,
    "adyntel_by_page_id": 0, "adyntel_by_url": 0, "adyntel_by_domain": 0, "adyntel_short_counts": 0, "busy": 0, "budget_exceeded": 0,
    # POST /facebook with `max_pages` > 1: explicit GraphQL paging on a lane. Bytes are decoded, not wire.
    "paged_requests": 0, "paged_pages": 0, "paged_decoded_bytes": 0,
    # Searches where Meta reported ads and served none on a lane, and how many GraphQL on the same lane recovered.
    "throttled_pages": 0, "throttled_recovered": 0, "throttle_skipped_direct": 0,
    # Brand lookups whose page carried a total and no ads on a lane (retried on another lane).
    "brand_withheld": 0, "brand_recovered": 0,
    # Single calls that needed a second lane, and jobs submitted.
    "lane_retries": 0, "jobs_submitted": 0, "job_items": 0,
    # POST /fetch: homepage reads through a lane exit (fetch.py).
    "fetch_requests": 0, "fetch_ok": 0, "fetch_failed": 0,
})
_FAILURE_COUNTER = {
    "RateLimited": "rate_limited", "ResultsMissing": "results_missing", "ScrapeBlocked": "blocked",
    "Busy": "busy", "BudgetExceeded": "budget_exceeded",
}

dispatcher: Dispatcher | None = None
jobs: JobStore | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global dispatcher, jobs
    if not settings.api_token:
        log.warning("API_TOKEN is not set - the scraper endpoint is unauthenticated")
    if settings.proxy or settings.fallback_proxy:
        log.warning("SCRAPER_PROXY / FALLBACK_PROXY are retired: lanes carry every request now; set LANE_PROXY_TEMPLATE + LANE_PROXY_PORTS")
    # Fail at startup, not on the first call, if the lanes cannot be built from the environment.
    dispatcher = build_dispatcher()
    dispatcher.start()
    jobs = JobStore(dispatcher, fetcher=lambda url, timeout_s, exclude: _read_homepage(url, timeout_s, exclude), executor=fetch_pool)
    log.info(
        "lanes: %d up on ports %s (%d in reserve); this host's own address is never used",
        len(dispatcher.lanes), [l.port for l in dispatcher.lanes],
        len(dispatcher.allocator.snapshot()["reserve"]) if dispatcher.allocator else 0,
    )
    try:
        yield
    finally:
        dispatcher.stop()


app = FastAPI(title="facebook-ad-library", version=__version__, lifespan=lifespan)


# --------------------------------------------------------------------------- request bodies


class SearchRequest(BaseModel):
    """The sourcing workflow's Apify body verbatim, plus plain names. Unknown fields (`category`, `mediaType`,
    `advertisers`, `fetchDetails`) are ignored: the actor took them, this service has one mode."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    query: str | None = Field(default=None, validation_alias=AliasChoices("query", "q", "keyword"))
    country: str = Field(default="US", validation_alias=AliasChoices("country", "countries"))
    max_items: int = Field(default=80, validation_alias=AliasChoices("maxItems", "max_items", "max"))
    active_status: str = Field(default="active", validation_alias=AliasChoices("activeStatus", "active_status"))
    # Deep paging. `max_pages` 1 (the default) keeps the rendered-page behaviour the sourcing
    # workflow has always had; anything higher pages the search over GraphQL on the lane. See graphql.py for the
    # measurements behind each stop.
    max_pages: int = Field(default=1, validation_alias=AliasChoices("max_pages", "maxPages"))
    novelty_stop: int = Field(default=25, validation_alias=AliasChoices("novelty_stop", "noveltyStop"))
    empty_tol: int = Field(default=8, validation_alias=AliasChoices("empty_tol", "emptyTol"))
    # How many ads are worth paging for, which is NOT `max_items`: that one sizes the response and
    # exists for Apify parity. 0 leaves the stop to the page, novelty and empty limits.
    max_ads: int = Field(default=0, validation_alias=AliasChoices("max_ads", "maxAds"))
    cursor: str | None = Field(default=None, validation_alias=AliasChoices("cursor", "next_cursor", "nextCursor"))
    collation: str | None = Field(default=None, validation_alias=AliasChoices("collation", "collation_token", "collationToken"))
    budget_s: float | None = Field(default=None, validation_alias=AliasChoices("budget_s", "budgetS"))

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
        return max(1, min(n, 5000))

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


class JobItemRequest(BaseModel):
    """One item of a batch: a search (the Apify-shaped body, plus `id`) or a count (the Adyntel body, plus `id`)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    kind: str = "search"
    query: str | None = Field(default=None, validation_alias=AliasChoices("query", "q", "keyword"))
    country: str = Field(default="US", validation_alias=AliasChoices("country", "countries"))
    active_status: str = Field(default="active", validation_alias=AliasChoices("activeStatus", "active_status"))
    max_ads: int = Field(default=0, validation_alias=AliasChoices("max_ads", "maxAds"))
    page_id: str | None = Field(default=None, validation_alias=AliasChoices("page_id", "pageId"))
    facebook_url: str | None = Field(default=None, validation_alias=AliasChoices("facebook_url", "facebookUrl", "page_url"))
    company_domain: str | None = Field(default=None, validation_alias=AliasChoices("company_domain", "companyDomain", "domain"))
    media_type: str = Field(default="all", validation_alias=AliasChoices("media_type", "mediaType"))
    max_results: int = Field(default=10, validation_alias=AliasChoices("max_results", "maxResults"))
    # Deep paging for a search item, as on POST /facebook: GraphQL on the lane, no rendered page,
    # up to `max_pages` x 30 ads. The rendered page's 30-ad limit is what a second pass lifts.
    max_pages: int = Field(default=1, validation_alias=AliasChoices("max_pages", "maxPages"))
    novelty_stop: int = Field(default=25, validation_alias=AliasChoices("novelty_stop", "noveltyStop"))
    empty_tol: int = Field(default=8, validation_alias=AliasChoices("empty_tol", "emptyTol"))
    # A fetch item: the homepage to read (a bare domain is fine; https:// is assumed).
    url: str | None = Field(default=None, validation_alias=AliasChoices("url", "homepage"))

    @field_validator("country", mode="before")
    @classmethod
    def _first_country(cls, v):
        if isinstance(v, (list, tuple)):
            v = v[0] if v else "US"
        return str(v or "US")

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v):
        return str(v).strip()


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[JobItemRequest]
    max_tries: int = Field(default=0, validation_alias=AliasChoices("max_tries", "maxTries"))
    # Fetch jobs only: after this many seconds every unfinished item answers `timeout`.
    deadline_s: float | None = Field(default=None, validation_alias=AliasChoices("deadline_s", "deadlineS"))


# --------------------------------------------------------------------------- plumbing


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not settings.api_token:
        return
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _error_body(exc: Exception, status: int, kind: str | None = None) -> dict:
    body = {"type": type(exc).__name__, "status": status, "message": str(exc), "description": str(exc)}
    if kind:
        body["kind"] = kind
    return {"error": body}


@app.exception_handler(HTTPException)
async def _http_error(_, exc: HTTPException):
    # Same envelope as every other failure, so a 401 reads like a 503 to the caller's error check.
    detail = str(exc.detail)
    body = {"error": {"type": "HTTPException", "status": exc.status_code, "message": detail, "description": detail}}
    return JSONResponse(status_code=exc.status_code, content=body)


async def _dispatch(item: Item, timeout: float) -> Outcome:
    """Run one item on the lanes and wait for its outcome; `Busy` when no lane answered in time."""
    assert dispatcher is not None, "the dispatcher starts with the app"
    fut = dispatcher.submit(item)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
    except asyncio.TimeoutError:
        dispatcher.cancel(item)
        raise Busy(f"no lane finished {item.kind} {item.id} within {timeout:.0f}s") from None


def _failure(exc: FacebookError, outcome: Outcome | None = None) -> JSONResponse:
    if outcome is not None and outcome.unexpected:
        counters.bump("failed")
        body = {"type": outcome.error_type, "status": 500, "message": outcome.error, "description": outcome.error, "kind": "error"}
        return JSONResponse(status_code=500, content={"error": body})
    name = type(exc).__name__
    counters.bump(_FAILURE_COUNTER.get(name, "failed"))
    kind = outcome.status if outcome is not None else ("blocked" if name in ("ScrapeBlocked", "RateLimited") else "error")
    headers = _lane_headers(outcome) if outcome is not None else {"X-Status": kind}
    return JSONResponse(status_code=exc.status, content=_error_body(exc, exc.status, kind), headers=headers)


def _lane_headers(outcome: Outcome) -> dict:
    return {
        "X-Status": outcome.status,
        "X-Lane": f"lane-{outcome.lane}" if outcome.lane else "",
        "X-Exit-IP": outcome.exit_ip or "",
        "X-Tries": str(len(outcome.tries)),
        "X-Decoded-Bytes": str(outcome.decoded_bytes),
    }


# --------------------------------------------------------------------------- POST /facebook


@app.post("/facebook", dependencies=[Depends(require_token)])
async def facebook(req: SearchRequest):
    counters["requests"] += 1
    try:
        query, country, status = service.search_params(req.query, req.country, req.active_status)
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))

    paged = bool(req.max_pages and req.max_pages > 1)
    if not paged:
        cached = service.cached_search(query, country, status)
        if cached is not None:
            items = cached[: req.max_items]
            counters["cache_hits"] += 1
            counters["ok" if items else "empty"] += 1
            log.info("cache hit %r %s -> %d item(s)", query, country, len(items))
            return JSONResponse(content=items, headers={"X-Cache": "hit", "X-Attempts": "0", "X-Scrape-Seconds": "0", "X-Status": "ok" if items else "no_ads"})

    started = time.time()
    budget = (settings.page_budget_s if req.budget_s is None else req.budget_s) if paged else settings.scrape_budget_s
    item = service.search_item(query, country, status, id=f"{query}|{country}", priority=1, deadline=started + (budget or settings.scrape_budget_s), max_ads=req.max_ads)
    if paged:
        counters["paged_requests"] += 1
        item.max_pages, item.novelty_stop, item.empty_tol = req.max_pages, req.novelty_stop, req.empty_tol
        item.budget_s, item.cursor, item.collation = budget, req.cursor, req.collation
    counters["in_flight"] += 1
    try:
        outcome = await _dispatch(item, (budget or settings.scrape_budget_s) + 10)
    except FacebookError as e:
        log.warning("%s %r %s -> %s: %s", e.status, query, country, type(e).__name__, e)
        return _failure(e)
    except Exception as e:  # noqa: BLE001
        counters["failed"] += 1
        log.exception("unexpected failure searching %r %s", query, country)
        return JSONResponse(status_code=500, content=_error_body(e, 500))
    finally:
        counters["in_flight"] -= 1

    _count_search(outcome)
    if outcome.status not in ("ok", "no_ads"):
        e = outcome.exception
        log.warning("%s %r %s -> %s after %d tr%s: %s", e.status, query, country, outcome.status, len(outcome.tries), "y" if len(outcome.tries) == 1 else "ies", e)
        return _failure(e, outcome)

    items = service.remember_search(outcome, query, country, status) if not paged else service.search_items(outcome, query, country)
    items = items[: req.max_items]
    counters["ok" if items else "empty"] += 1
    result = outcome.result
    if result is not None and result.misses:
        counters["retried"] += 1
    headers = {
        **_lane_headers(outcome),
        "X-Scrape-Seconds": str(round(time.time() - started, 1)),
        "X-Attempts": str(result.attempts if result else 0),
        "X-Misses": str(result.misses if result else 0),
        "X-Session-Swaps": str(result.session_swaps if result else 0),
        "X-Direct-Skipped": "1" if outcome.direct_skipped else "0",
        "X-Ads-Found": str(len(items)),
        "X-Reported-Total": "" if outcome.count is None else str(outcome.count),
        "X-Cache": "miss",
    }
    run = outcome.run
    if run is not None:
        counters["paged_pages"] += run["pages"]
        counters["paged_decoded_bytes"] += run["decoded_bytes"]
        headers.update({
            "X-Paged": "1" if paged else "0",
            "X-Pages": str(run["pages"]),
            "X-Ads": str(len(run["ads"])),
            "X-Advertisers": str(run["advertisers"]),
            "X-Empty-Pages": str(run["empty_pages"]),
            "X-Stopped-Because": run["stopped_because"],
            "X-Session-Minted": "1" if run["session"]["minted_now"] else "0",
            "X-Truncated": "1" if run["truncated"] else "0",
            "X-Next-Cursor": run["next_cursor"] or "",
            "X-Collation": run["collation"],
        })
    log.info(
        "%s %r %s ads=%d total=%s lane=%s tries=%d attempts=%d misses=%d %.1fs%s",
        outcome.status, query, country, len(items), outcome.count if outcome.count is not None else "-",
        headers["X-Lane"], len(outcome.tries), result.attempts if result else 0, result.misses if result else 0,
        time.time() - started, " (direct GET skipped: throttled)" if outcome.direct_skipped else "",
    )
    return JSONResponse(content=items, headers=headers)


def _count_search(outcome: Outcome) -> None:
    withheld = [t for t in outcome.tries if t.get("outcome") == "withheld"]
    if withheld or outcome.direct_skipped:
        counters.bump("throttled_pages", max(1, len(withheld)))
    if outcome.direct_skipped:
        counters.bump("throttle_skipped_direct")
    if outcome.status == "ok" and outcome.run is not None:
        counters.bump("throttled_recovered")
    if len(outcome.tries) > 1:
        counters.bump("lane_retries")


# --------------------------------------------------------------------------- POST /adyntel


def _brand_headers(result: BrandResult, cache_state: str, outcome: Outcome | None = None) -> dict:
    h = {
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
    if outcome is not None:
        h.update(_lane_headers(outcome))
    return h


@app.post("/adyntel", dependencies=[Depends(require_token)])
async def adyntel(req: BrandRequest):
    counters["adyntel_requests"] += 1
    try:
        resolver, value, status, media = service.count_params(req.page_id, req.facebook_url, req.company_domain, req.active_status, req.media_type)
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))
    counters[service.BY_RESOLVER[resolver]] += 1

    page_id, cached, resolved = service.cached_count(resolver, value, status, media)
    if resolved:
        counters["adyntel_resolve_hits"] += 1
    if cached is not None:
        counters["adyntel_cache_hits"] += 1
        counters["adyntel_found" if cached.found else "adyntel_not_found"] += 1
        log.info("cache hit adyntel %s=%s page=%s status=%s media=%s count=%d", resolver, value, page_id, status, media, cached.count)
        return JSONResponse(content=service.envelope(cached, req.max_results), headers=_brand_headers(cached, "hit"))

    started = time.time()
    item = service.count_item(resolver, value, status, media, page_id=page_id, id=f"{resolver}={value}", priority=0, deadline=started + settings.brand_budget_s)
    counters["in_flight"] += 1
    try:
        outcome = await _dispatch(item, settings.brand_budget_s + 5)
    except FacebookError as e:
        log.warning("%s adyntel %s=%s -> %s: %s", e.status, resolver, value, type(e).__name__, e)
        return _failure(e)
    except Exception as e:  # noqa: BLE001
        counters["failed"] += 1
        log.exception("unexpected failure looking up %s=%s", resolver, value)
        return JSONResponse(status_code=500, content=_error_body(e, 500))
    finally:
        counters["in_flight"] -= 1

    if any(t.get("outcome") == "withheld" for t in outcome.tries):
        counters["brand_withheld"] += 1
        if outcome.status == "ok":
            counters["brand_recovered"] += 1
    if len(outcome.tries) > 1:
        counters["lane_retries"] += 1
    if outcome.status not in ("ok", "not_found"):
        e = outcome.exception
        log.warning("%s adyntel %s=%s -> %s after %d tr%s: %s", e.status, resolver, value, outcome.status, len(outcome.tries), "y" if len(outcome.tries) == 1 else "ies", e)
        return _failure(e, outcome)

    result = service.remember_count(outcome, resolver, value, status, media)
    assert result is not None
    counters["adyntel_found" if result.found else "adyntel_not_found"] += 1
    if result.misses or result.short_counts:
        counters["retried"] += 1
    if result.short_counts:
        counters["adyntel_short_counts"] += 1
    log.info(
        "%s adyntel %s=%s page=%s status=%s media=%s count=%d ads=%d lane=lane-%s attempts=%d misses=%d short=%d plain=%d swaps=%d queue=%.1fs %.1fs%s",
        "ok" if result.found else "not-found", resolver, value, result.page_id or "-", status, media, result.count,
        len(result.ads or []), outcome.lane, result.attempts, result.misses, result.short_counts, result.plain_gets, result.session_swaps, result.queue_s,
        time.time() - started, f" ({result.note})" if result.note else "",
    )
    return JSONResponse(content=service.envelope(result, req.max_results), headers=_brand_headers(result, "miss", outcome))


# --------------------------------------------------------------------------- /jobs


def _job_specs(req: JobRequest) -> list[dict]:
    if not req.items:
        raise ValueError("invalid request: `items` is empty")
    kinds = {(it.kind or "search").strip().lower() for it in req.items}
    fetch_job = "fetch" in kinds
    if fetch_job and kinds != {"fetch"}:
        raise ValueError("invalid request: a job of `fetch` items cannot also hold searches or counts")
    cap = settings.fetch_job_max_items if fetch_job else settings.job_max_items
    if len(req.items) > cap:
        raise ValueError(f"invalid request: {len(req.items)} items, the cap is {cap}")
    specs: list[dict] = []
    seen: set[str] = set()
    for it in req.items:
        if not it.id:
            raise ValueError("invalid request: every item needs an `id`")
        if it.id in seen:
            raise ValueError(f"invalid request: item id {it.id!r} appears twice")
        seen.add(it.id)
        kind = (it.kind or "search").strip().lower()
        if kind == "fetch":
            # One bad url fails its own item, not the whole job.
            raw = it.url or it.company_domain or ""
            try:
                specs.append({"id": it.id, "kind": "fetch", "url": validate_public_url(raw), "invalid": None})
            except ValueError as e:
                specs.append({"id": it.id, "kind": "fetch", "url": str(raw), "invalid": str(e).replace("invalid request: ", "invalid url: ")})
        elif kind == "search":
            query, country, status = service.search_params(it.query, it.country, it.active_status)
            pages = int(it.max_pages or 1)
            if pages < 1 or pages > settings.page_max_pages:
                raise ValueError(f"invalid request: item {it.id!r} asks for max_pages {pages}; 1..{settings.page_max_pages}")
            specs.append({"id": it.id, "kind": "search", "query": query, "country": country, "status": status, "max_ads": it.max_ads,
                          "max_pages": pages, "novelty_stop": it.novelty_stop, "empty_tol": it.empty_tol})
        elif kind == "count":
            resolver, value, status, media = service.count_params(it.page_id, it.facebook_url, it.company_domain, it.active_status, it.media_type)
            specs.append({"id": it.id, "kind": "count", "resolver": resolver, "value": value, "status": status, "media": media, "max_results": max(1, min(int(it.max_results or 10), 30))})
        else:
            raise ValueError(f"invalid request: item {it.id!r} has kind {it.kind!r}; use `search`, `count` or `fetch`")
    return specs


@app.post("/jobs", dependencies=[Depends(require_token)], status_code=202)
async def submit_job(req: JobRequest):
    assert jobs is not None
    try:
        specs = _job_specs(req)
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))
    try:
        job = jobs.submit(specs, max(0, int(req.max_tries or 0)), req.deadline_s)
    except Busy as e:
        return _failure(e)
    counters["jobs_submitted"] += 1
    counters.bump("job_items", len(specs))
    log.info("job %s submitted: %d item(s)", job.id, len(specs))
    return JSONResponse(status_code=202, content={
        "job_id": job.id, "status": job.status, "items": len(specs),
        "poll": f"/jobs/{job.id}?wait_s={int(settings.job_poll_max_wait_s)}",
    })


@app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
async def poll_job(job_id: str, wait_s: float = 0, include_items: str = "1", partial: int = 0):
    assert jobs is not None and dispatcher is not None
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r} (unknown, expired, or lost to a restart)")
    deadline = time.time() + max(0.0, min(float(wait_s or 0), settings.job_poll_max_wait_s))
    while job.status not in ("done", "cancelled") and time.time() < deadline:
        await asyncio.sleep(0.25)
        jobs.expire(job)
    snap = dispatcher.snapshot()["lanes_summary"]
    mode = str(include_items or "1").strip().lower()
    mode = "lite" if mode in ("lite", "2") else ("brands" if mode in ("brands", "3") else ("0" if mode in ("0", "false", "no") else "1"))
    return JSONResponse(content=job_payload(job, mode, bool(partial), snap))


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_token)])
async def cancel_job(job_id: str):
    assert jobs is not None and dispatcher is not None
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    return JSONResponse(content=job_payload(job, False, True, dispatcher.snapshot()["lanes_summary"]))


# --------------------------------------------------------------------------- GET /health


class FetchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(validation_alias=AliasChoices("url", "domain", "homepage"))
    max_bytes: int | None = Field(default=None, validation_alias=AliasChoices("max_bytes", "maxBytes"))
    timeout_s: float | None = Field(default=None, validation_alias=AliasChoices("timeout_s", "timeoutS"))


# One pool for every homepage read, POST /fetch and fetch jobs alike: FETCH_CONCURRENCY threads.
fetch_pool = ThreadPoolExecutor(max_workers=max(1, settings.fetch_concurrency), thread_name_prefix="fetch")
_fetch_turn = itertools.count()


def _fetch_proxy(exclude: str | None = None) -> tuple[str | None, str | None]:
    """The next lane's exit, round robin (thread-safe), skipping `exclude` when another lane exists.
    Fetches ride the exits, not the lanes: no accounting."""
    if dispatcher is None or not dispatcher.lanes:
        return None, None
    lanes_ = dispatcher.lanes
    for _ in range(len(lanes_)):
        lane = lanes_[next(_fetch_turn) % len(lanes_)]
        if lane.name != exclude or len(lanes_) == 1:
            return lane.proxy_url, lane.name
    return lanes_[0].proxy_url, lanes_[0].name


def _read_homepage(url: str, timeout_s: float, exclude: str | None = None, max_bytes: int | None = None, keep_text: bool = False) -> dict:
    """One homepage read on a lane exit, logged and counted; the fetcher of fetch jobs."""
    proxy, lane_name = _fetch_proxy(exclude)
    out = fetch_page(url, proxy, min(timeout_s, 60), min(max_bytes or settings.fetch_max_bytes, 2_000_000), summarise=True)
    if not keep_text:
        out.pop("text", None)
    out["lane"] = lane_name
    out["proxied"] = bool(proxy)
    counters.bump("fetch_ok" if out["ok"] else "fetch_failed")
    log.info("fetch %s via %s -> %s %s %s in %ss", url, lane_name or "direct", "ok" if out["ok"] else "failed", out.get("status"), out.get("error") or "", out["seconds"])
    return out


@app.post("/fetch", dependencies=[Depends(require_token)])
async def fetch(req: FetchRequest):
    """A homepage through a lane exit with the Chrome profile. Always 200 with an answer: `ok`,
    `status`, `final_url`, `text` (at most `max_bytes`), `summary` (fetch.page_summary), `error`, so a
    caller pairing pages to brands by position never loses a slot. 400 for a malformed or
    non-public `url`."""
    counters["fetch_requests"] += 1
    try:
        url = validate_public_url(req.url)
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))
    timeout = req.timeout_s if req.timeout_s and req.timeout_s > 0 else settings.fetch_timeout_s
    out = await asyncio.get_running_loop().run_in_executor(fetch_pool, lambda: _read_homepage(url, timeout, None, req.max_bytes, keep_text=True))
    return JSONResponse(content=out, headers={"X-Status": "ok" if out["ok"] else "failed", "X-Lane": out["lane"] or "-"})


@app.get("/health")
async def health():
    lanes = dispatcher.snapshot() if dispatcher is not None else {"lanes": [], "lanes_summary": {"total": 0, "up": 0, "cooling": 0, "blocked": 0}, "block_rate_1h": 0.0, "tries_1h": 0, "blocked_tries_1h": 0, "ip_stats": {}, "ports": {}, "queue_depth": 0, "doc_id_stale": False}
    proxied = bool(dispatcher and all(l.proxy_url for l in dispatcher.lanes))
    live = sum(l["session"]["live"] for l in lanes["lanes"])
    warm = sum(l["session"]["warm"] for l in lanes["lanes"])
    return {
        "status": "ok",
        "version": __version__,
        "auth": bool(settings.api_token),
        "proxy": proxied,
        "fallback_proxy": proxied,
        "max_concurrency": len(lanes["lanes"]),
        **counters,
        "sessions": {"live": live, "warm": warm, **session_counters},
        "cache": cache.stats(),
        "brand_cache": brand_cache.stats(),
        "throttle": {"active": any(l["throttle_active"] for l in lanes["lanes"]), "lanes_throttled": sum(1 for l in lanes["lanes"] if l["throttle_active"])},
        "mint_breaker": {"open": any(l["gql"]["breaker_open"] for l in lanes["lanes"]), "lanes_open": sum(1 for l in lanes["lanes"] if l["gql"]["breaker_open"])},
        **lanes,
        "jobs": jobs.snapshot() if jobs is not None else {"queued": 0, "running": 0, "done": 0, "queue_depth": 0, "store": 0},
    }
