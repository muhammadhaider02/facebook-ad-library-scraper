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
    # Lookup results live this long; short, so a re-run measures the site and not the cache.
    adyntel_cache_ttl_s: float = _env_float("ADYNTEL_CACHE_TTL_S", 600)
    cache_ttl_s: float = _env_float("CACHE_TTL_S", 86400)
    cache_empty_ttl_s: float = _env_float("CACHE_EMPTY_TTL_S", 3600)
    cache_max_entries: int = _env_int("CACHE_MAX_ENTRIES", 2000)
    host: str = os.environ.get("HOST", "0.0.0.0")
    # 8000 is trustpilot-reviews, 8001 is reddit-reviews, on the same Docker network.
    port: int = _env_int("PORT", 8002)


settings = Settings()
