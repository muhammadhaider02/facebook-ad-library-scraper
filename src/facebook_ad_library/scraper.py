"""The Ad Library wire protocol, and the search that drives it.

The Meta Ad Library is a logged-out React site. When its search page is requested, Meta runs the
search on the server and embeds the first page of results in the HTML as a prefetched Relay
stream (`RelayPrefetchedStreamCache`), which is exactly what the frontend's own GraphQL call
would have returned. This module reads that: one GET per keyword-and-country pair, no GraphQL.

Why not GraphQL, which is what the frontend uses to scroll past the first page: measured
2026-09-19 (docs/architecture.md, "Measured against Apify"), Meta answers `POST /api/graphql/` from datacenter
addresses with error 1675004 on the very first call of a fresh session, keyed on the source
address, while the page GET is served to the same address without a throttle. A residential
address gets both. The page carries up to 30 ads, which is also what three GraphQL pages gave.

Three things a 200 page can be, all seen from every address:
  - ads:   the results blob is present with edges, up to 30 ads
  - empty: the results blob is present with no edges: the keyword really has no ads
  - miss:  the results blob is absent (~573 KB page): Meta skipped the prefetch; about 1 in 4
           requests, per request not per keyword; a retry usually carries it

Verified 2026-09-19 against the live site, from a laptop and from the VPS:
  - the first GET answers `403 Client challenge` with a RELATIVE `/__rd_verify_...` POST URL; the
    `rd_challenge` cookie it sets lasts 24 h and the next GETs on that jar are not challenged
  - plain (non-Chrome-TLS) clients clear the challenge and then get a 400 error page on every request
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlencode, urljoin

from .config import settings

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, session imports this module
    from .session import _SessionPool

log = logging.getLogger(__name__)

ORIGIN = "https://www.facebook.com"
AD_LIBRARY = ORIGIN + "/ads/library/"
CHALLENGE_MARKER = "__rd_verify"
# fetch('/__rd_verify_<token>?challenge=3', {method:'POST'}) - the path is relative in the wild.
CHALLENGE_RE = re.compile(r"""fetch\(\s*['"]([^'"]*__rd_verify[^'"]*)['"]""")
CHALLENGE_FALLBACK_RE = re.compile(r"""['"]((?:https?://[^'"]*|/)[^'"]*__rd_verify[^'"]*)['"]""")
# Present on every real Ad Library page, results or not; absent from Meta's generic error pages.
APP_MARKER = '["LSD",[],{"token":"'
RESULTS_MARKER = "search_results_connection"
JSON_BLOB_RE = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)

ACTIVE_STATUS = {"active": "ACTIVE", "inactive": "INACTIVE", "all": "ALL"}


# --------------------------------------------------------------------------- errors


class FacebookError(Exception):
    """Base class. `status` is the HTTP status the API answers with. Every subclass is a vendor
    failure from the caller's point of view: nothing about the request was wrong."""

    status = 503


class ScrapeBlocked(FacebookError):
    """Meta refused the session outright: the challenge would not clear, a 403 without the
    challenge marker, or a 400 error page (the TLS-fingerprint symptom)."""


class RateLimited(FacebookError):
    """HTTP 429 on the page. Waiting helps once, then a fresh session."""


class SessionDead(FacebookError):
    """An unexpected status, or a 200 that is not the Ad Library page: the cookie jar is no
    longer accepted. Internal: search() swaps sessions, and only a second failure escapes,
    as ScrapeBlocked."""


class ResultsMissing(FacebookError):
    """Every attempt came back as the page without the results blob. Meta skips the server-side
    prefetch now and then; if it does so for every retry inside the budget, that is this. It is
    an error and not an empty list on purpose: Stage 0 reads an empty list as "no inventory" and
    retires keywords on it."""


class ScrapeFailed(FacebookError):
    """Network failure or 5xx that survived the retries."""


# --------------------------------------------------------------------------- data


class Page(Enum):
    ADS = "ads"
    EMPTY = "empty"
    MISS = "miss"


@dataclass
class SearchResult:
    query: str
    country: str
    ads: list[dict]
    attempts: int  # page GETs that answered with the Ad Library page
    misses: int  # of those, pages without the results blob
    seconds: float
    session_swaps: int = 0


# --------------------------------------------------------------------------- pure functions


def normalise_country(raw: str) -> str:
    c = str(raw or "").strip().upper()
    if c == "ALL":
        return c
    if len(c) != 2 or not c.isalpha():
        raise ValueError(f"invalid country {raw!r}: expected a two-letter code or ALL")
    return c


def normalise_active_status(raw: str) -> str:
    key = str(raw or "active").strip().lower()
    if key not in ACTIVE_STATUS:
        raise ValueError(f"invalid activeStatus {raw!r}: expected active, inactive or all")
    return key


def bootstrap_url(query: str, country: str, active_status: str = "active") -> str:
    """The search page the site's own address bar shows for a keyword search."""
    params = {
        "active_status": normalise_active_status(active_status),
        "ad_type": "all",
        "country": country,
        "q": query,
        "search_type": "keyword_unordered",
        "media_type": "all",
    }
    return AD_LIBRARY + "?" + urlencode(params)


def challenge_url(html: str) -> str | None:
    """The __rd_verify URL out of a challenge page, made absolute."""
    m = CHALLENGE_RE.search(html) or CHALLENGE_FALLBACK_RE.search(html)
    if not m:
        return None
    return urljoin(ORIGIN + "/", m.group(1))


def _title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else ""


def is_app_page(html: str) -> bool:
    """True for the Ad Library page in any of its shapes, false for Meta's generic error pages."""
    return APP_MARKER in (html or "")


def find_results(html: str) -> dict | None:
    """The `search_results_connection` object embedded in the page, or None when the prefetch
    is absent. Only the JSON blobs that mention it are parsed; the page has ~40 of them."""
    for blob in JSON_BLOB_RE.findall(html or ""):
        if RESULTS_MARKER not in blob:
            continue
        try:
            doc = json.loads(blob)
        except ValueError:
            continue
        found = _find_key(doc, RESULTS_MARKER)
        if isinstance(found, dict):
            return found
    return None


def _find_key(o, key: str):
    if isinstance(o, dict):
        if key in o:
            return o[key]
        for v in o.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _ad_key(ad: dict) -> str:
    return str(ad.get("ad_archive_id") or ad.get("collation_id") or id(ad))


def extract_ads(conn: dict) -> list[dict]:
    """Flatten `collated_results` across edges, unique by ad_archive_id, page order kept."""
    ads: list[dict] = []
    seen: set[str] = set()
    for edge in conn.get("edges") or []:
        node = (edge or {}).get("node") or {}
        for ad in node.get("collated_results") or []:
            if not isinstance(ad, dict):
                continue
            key = _ad_key(ad)
            if key in seen:
                continue
            seen.add(key)
            ads.append(ad)
    return ads


def classify_page(html: str) -> tuple[Page, list[dict]]:
    """Which of the three shapes a 200 Ad Library page is, and its ads. Pure."""
    conn = find_results(html)
    if conn is None:
        return Page.MISS, []
    ads = extract_ads(conn)
    return (Page.ADS if ads else Page.EMPTY), ads


def page_cost_s() -> float:
    """Worst case for one more GET: the pacing gap, the global limiter, and a full timeout."""
    return settings.spacing_max_s + 60.0 / max(1, settings.rate_limit_per_min) + settings.request_timeout_s


# --------------------------------------------------------------------------- orchestration


def search(
    query: str,
    country: str = "US",
    max_items: int = 80,
    active_status: str = "active",
    *,
    pool: "_SessionPool",
    deadline: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SearchResult:
    """The ads the Ad Library's search page shows for one keyword-and-country pair.

    One GET normally; up to SSR_RETRIES more when the page comes without its results; one
    session swap when the session is refused; a single sleep on a 429. Everything is priced
    against the deadline before it starts, and the first GET is never skipped.
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("query is required")
    country = normalise_country(country)
    status = normalise_active_status(active_status)
    max_items = max(1, min(int(max_items), 300))
    started = time.time()
    deadline = deadline if deadline is not None else started + settings.scrape_budget_s

    ads: list[dict] = []
    attempts = misses = 0
    with pool.lease() as lease:
        slept = False
        while True:
            if attempts and time.time() + page_cost_s() > deadline:
                raise ResultsMissing(f"no results in {attempts} attempt(s) for {query!r} {country} and no budget left for another")
            try:
                page, page_ads = lease.session.fetch(query, country, status)
            except RateLimited as e:
                if not slept and time.time() + settings.rate_limit_sleep_s + page_cost_s() <= deadline:
                    slept = True
                    log.warning("%s: %s; sleeping %.0fs then retrying once", lease.session.label, e, settings.rate_limit_sleep_s)
                    sleep(settings.rate_limit_sleep_s)
                    continue
                lease.session.retire("rate_limited")
                if lease.swaps or time.time() + page_cost_s() > deadline:
                    raise
                log.warning("%s/%s: %s again on %s; swapping sessions", query, country, type(e).__name__, lease.session.label)
                lease.replace()
                continue
            except (SessionDead, ScrapeFailed) as e:
                lease.session.retire("session_dead" if isinstance(e, SessionDead) else "failed")
                if lease.swaps or time.time() + page_cost_s() > deadline:
                    if isinstance(e, SessionDead):
                        raise ScrapeBlocked(f"two fresh sessions were refused in a row: {e}") from e
                    raise
                log.warning("%s/%s: %s on %s; swapping sessions", query, country, type(e).__name__, lease.session.label)
                lease.replace()
                continue
            attempts += 1
            if page is Page.MISS:
                misses += 1
                if misses > settings.ssr_retries:
                    raise ResultsMissing(f"the page came without results {misses} time(s) in a row for {query!r} {country}")
                log.info("%s/%s: page without results (%d of %d), retrying", query, country, misses, settings.ssr_retries + 1)
                continue
            ads = page_ads
            break
        swaps = lease.swaps

    return SearchResult(
        query=query,
        country=country,
        ads=ads[:max_items],
        attempts=attempts,
        misses=misses,
        seconds=round(time.time() - started, 1),
        session_swaps=swaps,
    )
