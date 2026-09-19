"""The Ad Library wire protocol, and the search that drives it.

The Meta Ad Library is a logged-out React site. Its frontend loads ads with `POST /api/graphql/`
using the persisted query `AdLibrarySearchPaginationQuery`; this module replays that call. The
pure functions here (URL, token extraction, form body, response classification) know nothing
about HTTP, so the whole protocol is testable against saved pages. `search()` at the bottom is
the orchestration: pagination, the wall-clock budget, and the recovery rules for each failure.

Verified 2026-09-19 against the live site, which is where these regexes and field lists come from:
  - the bootstrap GET answers `403 Client challenge` with a RELATIVE `/__rd_verify_...` POST URL
  - the page carries the session tokens but no server-rendered ads and no doc_id
  - the doc_id lives in one of the page's JS bundles, exported as a plain string
  - plain (non-Chrome-TLS) clients get a 400 error page on every request after the challenge
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Iterable
from urllib.parse import urlencode, urljoin

from .config import settings

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, session imports this module
    from .session import FbSession, _SessionPool

log = logging.getLogger(__name__)

ORIGIN = "https://www.facebook.com"
AD_LIBRARY = ORIGIN + "/ads/library/"
GRAPHQL = ORIGIN + "/api/graphql/"
FRIENDLY = "AdLibrarySearchPaginationQuery"
RATE_LIMIT_CODE = 1675004
JSON_PREFIX = "for (;;);"
CHALLENGE_MARKER = "__rd_verify"

# The challenge page's own script: fetch('/__rd_verify_<token>?challenge=3', {method:'POST'}).
CHALLENGE_RE = re.compile(r"""fetch\(\s*['"]([^'"]*__rd_verify[^'"]*)['"]""")
# Second chance in case the script shape changes: any quoted URL or path carrying the marker.
CHALLENGE_FALLBACK_RE = re.compile(r"""['"]((?:https?://[^'"]*|/)[^'"]*__rd_verify[^'"]*)['"]""")

# Each pattern matched the live page on 2026-09-19. jazoest has no form field when logged out, so
# it is matched loosely. DTSGInitialData (fb_dtsg) is absent when logged out and is not needed.
TOKEN_PATTERNS = {
    "lsd": r'\["LSD",\[\],\{"token":"([^"]+)"',
    "jazoest": r'jazoest["=:]+"?(\d+)',
    "rev": r'"server_revision":(\d+)',
    "hsi": r'"hsi":"(\d+)"',
    "spin_t": r'"__spin_t":(\d+)',
    "spin_r": r'"__spin_r":(\d+)',
    "spin_b": r'"__spin_b":"([a-z]+)"',
    "haste_session": r'"haste_session":"([^"]+)"',
    "connection_class": r'"connectionClass":"([^"]+)"',
}

BUNDLE_RE = re.compile(r'https://static\.xx\.fbcdn\.net/rsrc\.php/[^"\s<>]+\.js[^"\s<>]*')
BUNDLE_ESCAPED_RE = re.compile(r'https:\\/\\/static\.xx\.fbcdn\.net\\/rsrc\.php\\/[^"\s<>]+\.js[^"\s<>]*')
# `__d("AdLibrarySearchPaginationQuery_facebookRelayOperation",[],(function(t,n,r,o,a,i){a.exports="249..."}),null)`
# The exports variable is whatever the minifier chose (`a` today, `e` in 2025 dumps), hence [a-z].
DOC_ID_RE = re.compile(
    r'__d\("AdLibrarySearchPaginationQuery_facebookRelayOperation",\[\],'
    r'\(function\([^)]*\)\{(?:"use strict";)?[a-z]\.exports="(\d+)"'
)

# Every key the live query accepted on 2026-09-19: facebook.md §4.2 plus the ones a 2025 public
# implementation carried (audienceTimeframe, country, fetchPageInfo, fetchSharedDisclaimers).
# The per-search keys are overwritten by build_variables; FB_VARIABLES_JSON is merged on top of
# this for drift. Order matters for nothing but readability.
DEFAULT_VARIABLES: dict = {
    "activeStatus": "ACTIVE",
    "adType": "ALL",
    "audienceTimeframe": "LAST_7_DAYS",
    "bylines": [],
    "collationToken": None,
    "contentLanguages": [],
    "countries": ["US"],
    "country": "US",
    "cursor": None,
    "excludedIDs": [],
    "fetchPageInfo": False,
    "fetchSharedDisclaimers": False,
    "first": 30,
    "isTargetedCountry": False,
    "location": None,
    "mediaType": "ALL",
    "multiCountryFilterMode": None,
    "pageIDs": [],
    "potentialReachInput": None,
    "publisherPlatforms": [],
    "queryString": "",
    "regions": None,
    "searchType": "KEYWORD_UNORDERED",
    "sessionID": None,
    "sortData": None,
    "source": None,
    "startDate": None,
    "viewAllPageID": "0",
}

# The form body the site's frontend sends, in its order. Pinned by a test: a field that quietly
# disappears is the kind of change that turns into "Meta blocked us" a week later.
FORM_FIELDS = (
    "av", "__user", "__a", "__req", "__hs", "dpr", "__ccg", "__rev", "__s", "__hsi", "__comet_req",
    "lsd", "jazoest", "__spin_r", "__spin_b", "__spin_t", "__jssesw",
    "fb_api_caller_class", "fb_api_req_friendly_name", "server_timestamps", "doc_id", "variables",
)

ACTIVE_STATUS = {"active": "ACTIVE", "inactive": "INACTIVE", "all": "ALL"}


# --------------------------------------------------------------------------- errors


class FacebookError(Exception):
    """Base class. `status` is the HTTP status the API answers with. Every subclass is a vendor
    failure from the caller's point of view: nothing about the request was wrong."""

    status = 503


class ScrapeBlocked(FacebookError):
    """Meta refused the session outright: the challenge would not clear, a 403 without the
    challenge marker, a 400 error page (the TLS-fingerprint symptom), or a page with no lsd."""


class RateLimited(FacebookError):
    """GraphQL error 1675004. The session went too fast; waiting helps once, then a fresh one."""


class SessionDead(FacebookError):
    """A 200 with an HTML body, an unexpected status, or unparseable JSON: the cookie jar or the
    lsd is no longer accepted. Internal: search() swaps sessions, and only a second failure
    escapes, as ScrapeBlocked."""


class DocIdStale(FacebookError):
    """`data` came back null without a rate-limit code, or no doc_id could be found at all.
    Meta shipped a frontend build that changed the persisted query. Needs a human look."""


class ScrapeFailed(FacebookError):
    """Network failure or 5xx that survived the retries."""


# --------------------------------------------------------------------------- data


@dataclass
class Tokens:
    lsd: str
    jazoest: str = ""
    rev: str = ""
    hsi: str = ""
    spin_t: str = ""
    spin_r: str = ""
    spin_b: str = "trunk"
    haste_session: str = ""
    connection_class: str = "GOOD"


class Kind(Enum):
    OK = "ok"
    HTML = "html"
    RATE_LIMITED = "rate_limited"
    DATA_NULL = "data_null"
    TRANSIENT = "transient"
    BAD_JSON = "bad_json"


@dataclass
class SearchResult:
    query: str
    country: str
    ads: list[dict]
    pages_fetched: int
    pages_failed: int
    seconds: float
    truncated: bool = False
    partial: bool = False
    session_swaps: int = 0


# --------------------------------------------------------------------------- pure functions


def normalise_country(raw: str) -> str:
    c = str(raw or "").strip().upper()
    if c == "ALL":
        return c
    if not re.fullmatch(r"[A-Z]{2}", c):
        raise ValueError(f"invalid country: {raw!r} (two-letter code or ALL)")
    return c


def normalise_active_status(raw: str) -> str:
    key = str(raw or "active").strip().lower()
    if key not in ACTIVE_STATUS:
        raise ValueError(f"invalid activeStatus: {raw!r} (active, inactive or all)")
    return ACTIVE_STATUS[key]


def bootstrap_url(query: str, country: str, active_status: str = "active") -> str:
    """The search page URL the site itself uses. Also the Referer for every GraphQL call after it."""
    params = [
        ("active_status", str(active_status).lower()),
        ("ad_type", "all"),
        ("country", country),
        ("q", query),
        ("search_type", "keyword_unordered"),
        ("media_type", "all"),
    ]
    return AD_LIBRARY + "?" + urlencode(params)


def challenge_url(html: str) -> str | None:
    """The challenge POST URL, absolute. None when the page is not a challenge page."""
    if CHALLENGE_MARKER not in html:
        return None
    m = CHALLENGE_RE.search(html) or CHALLENGE_FALLBACK_RE.search(html)
    return urljoin(ORIGIN + "/", m.group(1)) if m else None


def _title_of(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def extract_tokens(html: str) -> Tokens:
    found = {}
    for name, pattern in TOKEN_PATTERNS.items():
        m = re.search(pattern, html)
        if m:
            found[name] = m.group(1)
    if "lsd" not in found:
        raise ScrapeBlocked(f"page carried no session token (lsd); title={_title_of(html)!r}, {len(html)} bytes")
    tokens = Tokens(lsd=found["lsd"])
    for name, value in found.items():
        setattr(tokens, name, value)
    if not tokens.spin_r:
        tokens.spin_r = tokens.rev
    if not tokens.spin_t:
        tokens.spin_t = str(int(time.time()))
    return tokens


def bundle_urls(html: str) -> list[str]:
    urls = BUNDLE_RE.findall(html) + [u.replace("\\/", "/") for u in BUNDLE_ESCAPED_RE.findall(html)]
    return sorted(set(urls))


def discover_doc_id(bundles: Iterable[str]) -> str | None:
    """The persisted-query id, from the first bundle text that defines the relay operation."""
    for text in bundles:
        m = DOC_ID_RE.search(text)
        if m:
            return m.group(1)
    return None


def b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = digits[r] + s
    return s or "0"


def variables_extra() -> dict:
    """FB_VARIABLES_JSON parsed, or {}. A malformed value is a config fault and raises."""
    raw = settings.variables_json
    if not raw:
        return {}
    extra = json.loads(raw)
    if not isinstance(extra, dict):
        raise ValueError("FB_VARIABLES_JSON must be a JSON object")
    return extra


def build_variables(
    *,
    query: str,
    country: str,
    cursor: str | None,
    collation_token: str,
    session_id: str,
    first: int = 30,
    active_status: str = "ACTIVE",
    extra: dict | None = None,
) -> dict:
    v = dict(DEFAULT_VARIABLES)
    v.update(extra or {})
    v.update(
        {
            "activeStatus": active_status,
            "collationToken": collation_token,
            "countries": [country],
            "country": country,
            "cursor": cursor,
            "first": int(first),
            "queryString": query,
            "sessionID": session_id,
        }
    )
    return v


def build_form(tokens: Tokens, doc_id: str, variables: dict, req_n: int) -> dict[str, str]:
    values = {
        "av": "0",
        "__user": "0",
        "__a": "1",
        "__req": b36(req_n),
        "__hs": tokens.haste_session,
        "dpr": "1",
        "__ccg": tokens.connection_class or "GOOD",
        "__rev": tokens.rev,
        "__s": "",
        "__hsi": tokens.hsi,
        "__comet_req": "1",
        "lsd": tokens.lsd,
        "jazoest": tokens.jazoest,
        "__spin_r": tokens.spin_r,
        "__spin_b": tokens.spin_b or "trunk",
        "__spin_t": tokens.spin_t,
        "__jssesw": "1",
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": FRIENDLY,
        "server_timestamps": "true",
        "doc_id": doc_id,
        "variables": json.dumps(variables, separators=(",", ":")),
    }
    return {k: values[k] for k in FORM_FIELDS}


def graphql_headers(tokens: Tokens, referer: str) -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-FB-LSD": tokens.lsd,
        "X-FB-Friendly-Name": FRIENDLY,
        "Origin": ORIGIN,
        "Referer": referer,
        "Accept": "*/*",
    }


def strip_prefix(text: str) -> str:
    return text[len(JSON_PREFIX):] if text.startswith(JSON_PREFIX) else text


def classify(status: int, text: str) -> tuple[Kind, dict | None]:
    """Sort a GraphQL response into the one of six things it can be. Pure; the caller raises."""
    if status >= 500:
        return Kind.TRANSIENT, None
    body_text = strip_prefix(text or "")
    if status != 200 or body_text.lstrip().startswith("<"):
        return Kind.HTML, None
    try:
        body = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return Kind.BAD_JSON, None
    if not isinstance(body, dict):
        return Kind.BAD_JSON, None
    errors = body.get("errors") or []
    if any(isinstance(e, dict) and e.get("code") == RATE_LIMIT_CODE for e in errors):
        return Kind.RATE_LIMITED, body
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("ad_library_main"), dict):
        return Kind.DATA_NULL, body
    return Kind.OK, body


def extract_ads(body: dict) -> tuple[list[dict], str | None]:
    """Flatten `collated_results` across edges; the cursor only if Meta says there is a next page."""
    conn = ((body.get("data") or {}).get("ad_library_main") or {}).get("search_results_connection") or {}
    ads: list[dict] = []
    for edge in conn.get("edges") or []:
        node = (edge or {}).get("node") or {}
        ads.extend(a for a in (node.get("collated_results") or []) if isinstance(a, dict))
    info = conn.get("page_info") or {}
    cursor = info.get("end_cursor") if info.get("has_next_page") else None
    return ads, cursor


def error_summary(body: dict | None) -> str:
    if not body:
        return ""
    errs = body.get("errors") or []
    parts = []
    for e in errs[:3]:
        if isinstance(e, dict):
            parts.append(f"{e.get('code')}: {str(e.get('message') or e.get('description') or '')[:80]}")
    return "; ".join(parts)


def page_cost_s() -> float:
    """Worst case for one more page: the pacing gap, the global limiter, and a full timeout."""
    return settings.spacing_max_s + 60.0 / max(1, settings.rate_limit_per_min) + settings.request_timeout_s


# --------------------------------------------------------------------------- orchestration


def _ad_key(ad: dict) -> str:
    return str(ad.get("ad_archive_id") or ad.get("collation_id") or id(ad))


def _paginate(
    session: "FbSession",
    query: str,
    country: str,
    max_items: int,
    active_status: str,
    deadline: float,
    collected: list[dict],
    seen: set[str],
    sleep: Callable[[float], None],
) -> tuple[int, bool, bool]:
    """Walk pages on one session, appending into `collected`.

    Returns (pages_ok, truncated, partial). A failure on the first page raises the session's
    error so the caller can swap sessions; a failure after a good page returns partial=True
    instead, because the ads already in hand are worth more than a 503.
    """
    cursor: str | None = None
    collation = str(uuid.uuid4())
    pages_ok = 0
    slept_for_rate_limit = False
    while True:
        if pages_ok >= settings.max_pages or len(collected) >= max_items:
            return pages_ok, False, False
        # Price a whole page before starting it; page 1 is never skipped (returning nothing
        # would be worse than returning late).
        if pages_ok >= 1 and time.time() + page_cost_s() > deadline:
            log.info("%s/%s budget exhausted after %d page(s), returning %d ad(s) early", query, country, pages_ok, len(collected))
            return pages_ok, True, False
        try:
            page_ads, cursor = session.search_page(query, country, cursor, collation, settings.page_size, active_status)
        except RateLimited as e:
            if not slept_for_rate_limit and time.time() + settings.rate_limit_sleep_s + page_cost_s() <= deadline:
                slept_for_rate_limit = True
                log.warning("%s: %s; sleeping %.0fs then retrying page %d once", session.label, e, settings.rate_limit_sleep_s, pages_ok + 1)
                sleep(settings.rate_limit_sleep_s)
                continue
            session.retire("rate_limited")
            if pages_ok:
                return pages_ok, False, True
            raise
        except SessionDead:
            session.retire("session_dead")
            if pages_ok:
                return pages_ok, False, True
            raise
        except DocIdStale:
            session.retire("docid_stale")
            if pages_ok:
                return pages_ok, False, True
            raise
        except ScrapeFailed:
            session.retire("failed")
            if pages_ok:
                return pages_ok, False, True
            raise
        pages_ok += 1
        for ad in page_ads:
            key = _ad_key(ad)
            if key in seen:
                continue
            seen.add(key)
            collected.append(ad)
            if len(collected) >= max_items:
                break
        if not page_ads or cursor is None:
            return pages_ok, False, False


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
    """Collect up to `max_items` ads for one keyword-and-country pair, the way the site's own
    infinite scroll would. One session swap is allowed when the first session fails before
    producing anything; the search then restarts from page 1 on the fresh session."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query is required")
    country = normalise_country(country)
    status = normalise_active_status(active_status)
    max_items = max(1, min(int(max_items), 300))
    started = time.time()
    deadline = deadline if deadline is not None else started + settings.scrape_budget_s

    ads: list[dict] = []
    seen: set[str] = set()
    with pool.lease(query, country) as lease:
        try:
            pages, truncated, partial = _paginate(lease.session, query, country, max_items, status, deadline, ads, seen, sleep)
        except (RateLimited, SessionDead, DocIdStale, ScrapeFailed) as first:
            if time.time() + page_cost_s() * 2 > deadline:
                raise
            log.warning("%s/%s: %s on %s; minting a fresh session and restarting", query, country, type(first).__name__, lease.session.label)
            lease.replace(query, country)
            try:
                pages, truncated, partial = _paginate(lease.session, query, country, max_items, status, deadline, ads, seen, sleep)
            except SessionDead as e:
                raise ScrapeBlocked(f"two fresh sessions were refused in a row: {e}") from e
        swaps = lease.swaps

    return SearchResult(
        query=query,
        country=country,
        ads=ads[:max_items],
        pages_fetched=pages,
        pages_failed=1 if partial else 0,
        seconds=round(time.time() - started, 1),
        truncated=truncated,
        partial=partial,
        session_swaps=swaps,
    )
