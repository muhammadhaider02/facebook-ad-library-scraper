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
    # Optional proxy for every request. Not needed on the primary path; see .env.example.
    proxy: str | None = os.environ.get("SCRAPER_PROXY", "").strip() or None
    # Persisted-query id override. Empty = discovered from the page's JS bundles at session mint.
    doc_id: str = os.environ.get("FB_DOC_ID", "").strip()
    # JSON object merged over the built-in GraphQL variables template. The drift escape hatch.
    variables_json: str = os.environ.get("FB_VARIABLES_JSON", "").strip()
    # curl_cffi impersonation target. Meta answers non-browser TLS with 400 error pages.
    impersonate: str = os.environ.get("FB_IMPERSONATE", "chrome").strip() or "chrome"
    request_timeout_s: float = _env_float("REQUEST_TIMEOUT_S", 30)
    # Ads per GraphQL page; 30 is what the site's own frontend asks for.
    page_size: int = _env_int("FB_PAGE_SIZE", 30)
    # Hard cap on pages per search regardless of `maxItems`. 80 ads = 3 pages.
    max_pages: int = _env_int("MAX_PAGES", 3)
    max_concurrency: int = _env_int("MAX_CONCURRENCY", 2)
    session_pool_size: int = _env_int("SESSION_POOL_SIZE", 2)
    # Session retirement thresholds: calls made and age in seconds, whichever first.
    session_max_requests: int = _env_int("SESSION_MAX_REQUESTS", 200)
    session_max_age_s: float = _env_float("SESSION_MAX_AGE_S", 7200)
    # Random gap between two calls on one session.
    spacing_min_s: float = _env_float("SPACING_MIN_S", 2)
    spacing_max_s: float = _env_float("SPACING_MAX_S", 5)
    # Global GraphQL calls per minute from this host, across sessions.
    rate_limit_per_min: int = _env_int("RATE_LIMIT_PER_MIN", 4)
    # Sleep on GraphQL error 1675004 before the single retry.
    rate_limit_sleep_s: float = _env_float("RATE_LIMIT_SLEEP_S", 60)
    # Consecutive empty first pages after which a session is retired as suspect.
    empty_streak_retire: int = _env_int("EMPTY_STREAK_RETIRE", 5)
    # Wall-clock budget for one search. Must stay under Stage 0's 300 s node timeout; see .env.example
    # for the arithmetic. Nothing cancels a request once it starts, so this is what bounds it.
    scrape_budget_s: float = _env_float("SCRAPE_BUDGET_S", 240)
    cache_ttl_s: float = _env_float("CACHE_TTL_S", 86400)
    cache_empty_ttl_s: float = _env_float("CACHE_EMPTY_TTL_S", 3600)
    cache_max_entries: int = _env_int("CACHE_MAX_ENTRIES", 2000)
    host: str = os.environ.get("HOST", "0.0.0.0")
    # 8000 is trustpilot-reviews, 8001 is reddit-reviews, on the same Docker network.
    port: int = _env_int("PORT", 8002)


settings = Settings()
