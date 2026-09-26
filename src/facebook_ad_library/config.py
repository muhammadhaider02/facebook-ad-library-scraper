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
    # --- Lanes (0.4.0, 25 Sep 2026): every request to Facebook leaves through a lane, and every
    # lane is one sticky residential exit with its own cookie jars, pacing and limiter. The VPS's
    # own address is never used: the same service answers 01's 50+ checks and 02's research ads,
    # so a block on that address would stop the whole pipeline, not just sourcing. See lanes.py.
    lane_count: int = _env_int("LANE_COUNT", 1)
    # `http://user__cr.us:pass@gw.dataimpulse.com:{port}`; `{port}` is filled from LANE_PROXY_PORTS.
    lane_proxy_template: str = os.environ.get("LANE_PROXY_TEMPLATE", "").strip()
    # Sticky ports, ranges and lists: `11510-11529,11540`. The first LANE_COUNT become lanes, the
    # rest are the reserve a blocked lane rotates onto for a fresh exit IP.
    lane_proxy_ports: str = os.environ.get("LANE_PROXY_PORTS", "").strip()
    # Escape hatch: full proxy specs, comma-separated, one per lane, used when the template is empty.
    lane_proxies: str = os.environ.get("LANE_PROXIES", "").strip()
    # A lane without a proxy refuses to start. Only CI, which has no proxy and no production
    # traffic, sets this false.
    lane_require_proxy: bool = _env_bool("LANE_REQUIRE_PROXY", True)
    # Where a lane learns its exit IP (a ~60 byte GET through its own proxy), and how often it
    # re-checks between items. Empty disables the check; 0 checks only at mint.
    lane_ip_check_url: str = os.environ.get("LANE_IP_CHECK_URL", "https://api.ipify.org?format=json").strip()
    lane_ip_check_s: float = _env_float("LANE_IP_CHECK_S", 600)
    # A hard block moves the lane to the next reserve port (a new IP) when set.
    lane_rotate_on_block: bool = _env_bool("LANE_ROTATE_ON_BLOCK", True)
    # Cooldown after a block; doubles while the probe after it keeps blocking, up to the max; a
    # lane whose probes fail three times in a row is `blocked` until the retry interval passes.
    lane_cooldown_s: float = _env_float("LANE_COOLDOWN_S", 900)
    lane_cooldown_max_s: float = _env_float("LANE_COOLDOWN_MAX_S", 3600)
    lane_blocked_retry_s: float = _env_float("LANE_BLOCKED_RETRY_S", 3600)
    lane_error_cooldown_after: int = _env_int("LANE_ERROR_COOLDOWN_AFTER", 3)
    lane_error_cooldown_s: float = _env_float("LANE_ERROR_COOLDOWN_S", 120)
    # Tries per item across different lanes, and withheld pages in a row that retire a lane's
    # jars and move it to a fresh port (the IP is what is throttled, not the jar).
    lane_max_tries: int = _env_int("LANE_MAX_TRIES", 3)
    lane_withheld_rotate: int = _env_int("LANE_WITHHELD_ROTATE", 3)
    # Per-try budgets, seconds. Short on purpose: a 429 must move the search to another lane, not
    # nap 60 s on the exit that just refused it (scraper.search skips the nap when it cannot fit).
    lane_search_budget_s: float = _env_float("LANE_SEARCH_BUDGET_S", 90)
    lane_count_budget_s: float = _env_float("LANE_COUNT_BUDGET_S", 20)
    # --- Batch jobs (POST /jobs): bounds on the in-memory store and queue. ---
    job_max_items: int = _env_int("JOB_MAX_ITEMS", 200)
    job_store_max: int = _env_int("JOB_STORE_MAX", 50)
    job_queue_max: int = _env_int("JOB_QUEUE_MAX", 1000)
    job_item_max_wait_s: float = _env_float("JOB_ITEM_MAX_WAIT_S", 600)
    job_ttl_s: float = _env_float("JOB_TTL_S", 7200)
    job_poll_max_wait_s: float = _env_float("JOB_POLL_MAX_WAIT_S", 50)
    # Retired in 0.4.0 and read only to warn at startup: lanes replaced the direct path and the
    # single recovery exit. `proxy_url()` still parses these forms for the CLI and the tests.
    proxy: str | None = os.environ.get("SCRAPER_PROXY", "").strip() or None
    fallback_proxy: str | None = os.environ.get("FALLBACK_PROXY", "").strip() or None
    # curl_cffi impersonation target. Meta answers non-browser TLS with 400 error pages.
    impersonate: str = os.environ.get("FB_IMPERSONATE", "chrome").strip() or "chrome"
    request_timeout_s: float = _env_float("REQUEST_TIMEOUT_S", 30)
    # Extra page GETs allowed when the page arrives without its results blob (about 1 in 4).
    ssr_retries: int = _env_int("SSR_RETRIES", 2)
    # Session retirement thresholds: page GETs made and age in seconds, whichever first. Keep the
    # age at or below the proxy vendor's sticky rotation interval (DataImpulse: 120 min set in
    # the dashboard), so a jar and its exit IP live and die together.
    session_max_requests: int = _env_int("SESSION_MAX_REQUESTS", 200)
    session_max_age_s: float = _env_float("SESSION_MAX_AGE_S", 7200)
    # Random gap between two requests on one session.
    spacing_min_s: float = _env_float("SPACING_MIN_S", 2)
    spacing_max_s: float = _env_float("SPACING_MAX_S", 5)
    # Gap between two GraphQL pages on one session. The site's own scroll pagination is this
    # fast; the rendered-page gap above would make an 8-page recovery take ~40 s.
    gql_spacing_min_s: float = _env_float("GQL_SPACING_MIN_S", 1)
    gql_spacing_max_s: float = _env_float("GQL_SPACING_MAX_S", 2)
    # GETs per minute PER LANE (one exit IP), across both endpoints.
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
    # POST /fetch: homepage reads through a lane exit with the Chrome profile (fetch.py). Fetches
    # run beside the lanes, not on them, so this is their own ceiling; 15 s is generous for a
    # storefront and short enough that a dead host does not hold a batch.
    # One thread pool serves POST /fetch and the fetch items of jobs (8 threads, well inside the
    # container's pids limit).
    fetch_concurrency: int = _env_int("FETCH_CONCURRENCY", 8)
    fetch_timeout_s: float = _env_float("FETCH_TIMEOUT_S", 15)
    fetch_max_bytes: int = _env_int("FETCH_MAX_BYTES", 300_000)
    # A job of homepage fetches (00's DTC check, 26 Sep 2026): at most this many items, and after
    # `deadline_s` (the job's, else this) every unfinished item answers `timeout` and the job ends.
    fetch_job_max_items: int = _env_int("FETCH_JOB_MAX_ITEMS", 600)
    fetch_job_deadline_s: float = _env_float("FETCH_JOB_DEADLINE_S", 150)
    # Pages the proxied fallback may take when this address is being throttled. Small on purpose:
    # the fallback exists to answer the search Stage 0 asked for, not to page deeply. 8 pages is
    # ~80 ads, above Stage 0's 80-item ask, for roughly 20 KB of billed wire.
    fallback_max_pages: int = _env_int("FALLBACK_MAX_PAGES", 8)
    # How long one withheld page suppresses the direct GET on later searches. While Meta is
    # throttling, that GET costs ~3 s and ~1 MB to be told what the previous search already
    # established: measured 24 Sep 2026, 24 of 24 searches with ads to give came back empty in one
    # cycle. Minutes, not hours - while this is set every search pays the proxy, and one direct GET
    # per window is the price of noticing Meta has stopped. 0 disables the shortcut entirely.
    throttle_memory_s: float = _env_float("THROTTLE_MEMORY_S", 600)
    # Sessions minted in a row that never returned a page, after which no more are minted until
    # the cooldown passes. A mint is the most expensive call this service makes; on 24 Sep 2026
    # Meta answered 1675004 on the first GraphQL call of every fresh session from the proxy exit
    # and nothing stopped the next keyword minting another - 7 in 20 minutes, 136 MB, zero ads.
    # 0 disables the breaker, which is how that incident was configured.
    mint_failure_limit: int = _env_int("MINT_FAILURE_LIMIT", 2)
    mint_cooldown_s: float = _env_float("MINT_COOLDOWN_S", 900)
    # Lookup results live this long; short, so a re-run measures the site and not the cache.
    adyntel_cache_ttl_s: float = _env_float("ADYNTEL_CACHE_TTL_S", 600)
    cache_ttl_s: float = _env_float("CACHE_TTL_S", 86400)
    cache_empty_ttl_s: float = _env_float("CACHE_EMPTY_TTL_S", 3600)
    cache_max_entries: int = _env_int("CACHE_MAX_ENTRIES", 2000)
    host: str = os.environ.get("HOST", "0.0.0.0")
    # 8000 is trustpilot-reviews, 8001 is reddit-reviews, on the same Docker network.
    port: int = _env_int("PORT", 8002)


settings = Settings()
