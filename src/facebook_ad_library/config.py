"""Runtime configuration, read once from the environment. `.env.example` documents every field."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Shared secret n8n sends as `Authorization: Bearer <token>`. Empty = no auth (local testing only).
    api_token: str = os.environ.get("API_TOKEN", "").strip()
    # Optional proxy for every request. Not needed; see .env.example.
    proxy: str | None = os.environ.get("SCRAPER_PROXY", "").strip() or None
    # Proxy for the RECOVERY path only: the GraphQL fallback and deep paging. Separate from
    # SCRAPER_PROXY on purpose. The rendered page is ~1 MB and the throttle withholds its ads
    # anyway, so routing it through a residential exit pays a megabyte for a page we already
    # know is empty; GraphQL answers the same search at ~7 KB an ad. Set this and leave
    # SCRAPER_PROXY empty to keep the ordinary path direct. Falls back to SCRAPER_PROXY.
    fallback_proxy: str | None = os.environ.get("FALLBACK_PROXY", "").strip() or None
    # curl_cffi impersonation target. Meta answers non-browser TLS with 400 error pages.
    impersonate: str = os.environ.get("FB_IMPERSONATE", "chrome").strip() or "chrome"
    request_timeout_s: float = _env_float("REQUEST_TIMEOUT_S", 30)
    # Extra page GETs allowed when the page arrives without its results blob (about 1 in 4).
    ssr_retries: int = _env_int("SSR_RETRIES", 2)
    max_concurrency: int = _env_int("MAX_CONCURRENCY", 3)
    session_pool_size: int = _env_int("SESSION_POOL_SIZE", 3)
    # Session retirement thresholds: page GETs made and age in seconds, whichever first.
    session_max_requests: int = _env_int("SESSION_MAX_REQUESTS", 200)
    session_max_age_s: float = _env_float("SESSION_MAX_AGE_S", 7200)
    # Random gap between two requests on one session.
    spacing_min_s: float = _env_float("SPACING_MIN_S", 2)
    spacing_max_s: float = _env_float("SPACING_MAX_S", 5)
    # Global GETs per minute from this host, across sessions and both endpoints.
    rate_limit_per_min: int = _env_int("RATE_LIMIT_PER_MIN", 20)
    # Sleep on an HTTP 429 before the single retry.
    rate_limit_sleep_s: float = _env_float("RATE_LIMIT_SLEEP_S", 60)
    # Consecutive pages without results after which a session is retired as suspect.
    miss_streak_retire: int = _env_int("MISS_STREAK_RETIRE", 5)
    # Wall-clock budget for one search. Must stay under Stage 0's 300 s node timeout; see .env.example
    # for the arithmetic. Nothing cancels a request once it starts, so this is what bounds it.
    scrape_budget_s: float = _env_float("SCRAPE_BUDGET_S", 240)
    # Brand lookups (POST /adyntel), called from n8n Code nodes with 30 s and 45 s ceilings: the
    # lookup answers a 503 inside this budget rather than letting n8n's timeout hide the reason.
    brand_budget_s: float = _env_float("BRAND_BUDGET_S", 25)
    # Extra page-view GETs allowed when the page arrives without its results blob.
    brand_ssr_retries: int = _env_int("BRAND_SSR_RETRIES", 2)
    # Resolve a vanity URL the page plugin does not know by fetching the profile page too. Off until
    # the deploy check has shown that page class is served to the VPS address; see .env.example.
    brand_profile_fallback: bool = _env_bool("BRAND_PROFILE_FALLBACK", False)
    # --- GraphQL deep paging (POST /facebook with `max_pages`), restored 24 Sep 2026 ---
    # The persisted-query id for AdLibrarySearchPaginationQuery. Discovered from the page bundles
    # when empty; pin it here if Meta stops shipping it where we look.
    doc_id: str = os.environ.get("FB_DOC_ID", "").strip()
    # Extra GraphQL variables as a JSON object, for a schema change that needs a field we do not
    # send. A malformed value is a config fault and raises at first use, not silently.
    variables_json: str = os.environ.get("FB_VARIABLES_JSON", "").strip()
    # Paging stops, all overridable per request. See graphql.py for what each one measured.
    page_max_pages: int = _env_int("PAGE_MAX_PAGES", 150)
    page_novelty_stop: int = _env_int("PAGE_NOVELTY_STOP", 25)
    page_empty_tol: int = _env_int("PAGE_EMPTY_TOL", 8)
    # Wall-clock ceiling for ONE paged call, so it answers inside the caller's node timeout instead
    # of being cut off by it. 150 pages measured 783 s, which Stage 0's 300 s node would never see.
    # A run that stops here says `truncated` and hands back a cursor; the caller pages again with
    # it. 0 removes the ceiling, which is only ever right for a probe nothing is waiting on.
    page_budget_s: float = _env_float("PAGE_BUDGET_S", 240)
    # Pages the proxied fallback may take when this address is being throttled. Small on purpose:
    # the fallback exists to answer the search Stage 0 asked for, not to page deeply. 8 pages is
    # ~80 ads, above Stage 0's 80-item ask, for roughly 20 KB of billed wire.
    fallback_max_pages: int = _env_int("FALLBACK_MAX_PAGES", 20)
    # How long one withheld page suppresses the direct GET on later searches. While Meta is
    # throttling, that GET costs ~3 s and ~1 MB to be told what the previous search already
    # established: measured 24 Sep 2026, 24 of 24 searches with ads to give came back empty in one
    # cycle. Minutes, not hours - while this is set every search pays the proxy, and one direct GET
    # per window is the price of noticing Meta has stopped. 0 disables the shortcut entirely.
    throttle_memory_s: float = _env_float("THROTTLE_MEMORY_S", 600)
    # Lookup results live this long; short, so a re-run measures the site and not the cache.
    adyntel_cache_ttl_s: float = _env_float("ADYNTEL_CACHE_TTL_S", 600)
    cache_ttl_s: float = _env_float("CACHE_TTL_S", 86400)
    cache_empty_ttl_s: float = _env_float("CACHE_EMPTY_TTL_S", 3600)
    cache_max_entries: int = _env_int("CACHE_MAX_ENTRIES", 2000)
    host: str = os.environ.get("HOST", "0.0.0.0")
    # 8000 is trustpilot-reviews, 8001 is reddit-reviews, on the same Docker network.
    port: int = _env_int("PORT", 8002)


settings = Settings()
