"""Deep paging over the Ad Library's own GraphQL endpoint, through a residential exit.

WHY THIS CAME BACK. GraphQL paging was dropped in `6984ab0` because Meta refuses
`/api/graphql/` from the VPS address - measured, and still true. It is restored here for one
reason: measured 24 Sep 2026, it is about FIVE TIMES cheaper per ad than reading the rendered
page, and the pipeline now pays per gigabyte for a residential exit.

    the rendered search page   1 GET   ~740 KB   30 ads   24.7 KB/ad
    GraphQL                    3 calls  145 KB   30 ads    4.8 KB/ad

TWO RULES THIS MODULE EXISTS TO ENFORCE.

1. ONLY WHAT IS BLOCKED GOES THROUGH THE PROXY. Ad *counts* still come back correct from the
   VPS address; it is the ad *payload* Meta withholds. So `/adyntel`'s count-only lookups keep
   using the ordinary unproxied pool, and only this module - which needs the ads themselves -
   pays for the residential exit. The session here is proxied end to end, because the mint, the
   cookies and the GraphQL POSTs must all come from the same address.

2. THE MINT IS NOT FREE. A fresh session costs ~725 KB over 3 calls, about the same as one
   rendered page, so minting per keyword would throw the saving away. Sessions are reused across
   keywords up to `SESSION_MAX_REQUESTS`, which makes the amortised cost ~12 KB a keyword.

Paging stops on whichever comes first (measured on `red light therapy mask`, whose true end is
368 pages / 248 advertisers):
  max_pages     hard ceiling. 150 captured 219 of 248 advertisers, 88%, in 41% of the pages.
  novelty_stop  consecutive pages adding no NEW advertiser. For brand sourcing the advertiser is
                the unit of value, not the ad.
  empty_tol     consecutive EMPTY pages tolerated. An empty page is NOT the end: past page ~100
                Meta alternates empty and one-ad pages, and quitting at the first blank cost 135
                ads and 25 advertisers on that keyword.
Meta dropping the cursor is the true end and always stops.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .config import settings
from .proxy import proxy_url
from .scraper import (
    ORIGIN,
    FacebookError,
    RateLimited,
    ScrapeBlocked,
    ScrapeFailed,
    SessionDead,
    _title_of,
    bootstrap_url,
    challenge_url,
    normalise_active_status,
    normalise_country,
    registrable_domain,
)
from .scraper import CHALLENGE_MARKER

log = logging.getLogger(__name__)

ACTIVE_STATUS_GQL = {"active": "ACTIVE", "inactive": "INACTIVE", "all": "ALL"}
# The measured ceiling: on the one keyword paged to Meta's true end (368 pages, 248
# advertisers) 150 pages captured 219 of them - 88% of the brands for 41% of the pages.
MAX_PAGES_CEILING = 150

GRAPHQL = ORIGIN + "/api/graphql/"
FRIENDLY = "AdLibrarySearchPaginationQuery"
RATE_LIMIT_CODE = 1675004
JSON_PREFIX = "for (;;);"

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
DOC_ID_RE = re.compile(
    r'__d\("AdLibrarySearchPaginationQuery_facebookRelayOperation",\[\],'
    r'\(function\([^)]*\)\{(?:"use strict";)?[a-z]\.exports="(\d+)"'
)

FORM_FIELDS = (
    "av", "__user", "__a", "__req", "__hs", "dpr", "__ccg", "__rev", "__s", "__hsi", "__comet_req",
    "lsd", "jazoest", "__spin_r", "__spin_b", "__spin_t", "__jssesw",
    "fb_api_caller_class", "fb_api_req_friendly_name", "server_timestamps", "doc_id", "variables",
)


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


class DocIdStale(FacebookError):
    """`data` came back null without a rate-limit code, or no doc_id could be found at all.
    Meta shipped a frontend build that changed the persisted query. Needs a human look."""


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


# --------------------------------------------------------------------------- the proxied session


class GraphSession:
    """One minted, proxied browser: cookie jar, tokens, doc_id, reused across keywords.

    Proxied end to end on purpose. The challenge cookie, the `lsd` token and the GraphQL POSTs are
    all bound to the address that minted them, so a session that mints direct and pages through the
    proxy is refused - and one that mints through the proxy pays the ~725 KB mint only once.
    """

    BOOTSTRAP_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(self, label: str = "gql") -> None:
        from .session import CurlTransport

        proxy = proxy_url()
        if not proxy:
            raise ScrapeBlocked(
                "graphql paging needs SCRAPER_PROXY: Meta refuses /api/graphql/ from this address "
                "(measured 2026-09-22, commit c81d0a3)"
            )
        self.transport = CurlTransport(proxy=proxy)
        self.label = f"{label}-{uuid.uuid4().hex[:6]}"
        self.tokens = None
        self.doc_id = ""
        self.session_id = str(uuid.uuid4())
        self.referer = ""
        self.req_n = 0
        self.requests_made = 0
        self.decoded_bytes = 0
        self.minted_at = 0.0
        self.last_call = 0.0
        self.retired = False

    @property
    def ready(self) -> bool:
        return self.tokens is not None and bool(self.doc_id) and not self.expired

    @property
    def expired(self) -> bool:
        return (
            self.retired
            or self.requests_made >= settings.session_max_requests
            or bool(self.minted_at and time.time() - self.minted_at > settings.session_max_age_s)
        )

    def _pace(self) -> None:
        if not self.last_call:
            return
        wait = self.last_call + random.uniform(settings.spacing_min_s, settings.spacing_max_s) - time.time()
        if wait > 0:
            time.sleep(wait)

    def _count(self, text: str) -> None:
        self.decoded_bytes += len(text or "")

    def mint(self, query: str, country: str, active_status: str = "active") -> None:
        """GET the search page through the proxy, clear the challenge, read the tokens and doc_id."""
        url = bootstrap_url(query, country, active_status)
        self.referer = url
        try:
            r = self.transport.get(url, headers=self.BOOTSTRAP_HEADERS)
            self._count(r.text)
            if r.status == 403 and CHALLENGE_MARKER in r.text:
                challenge = challenge_url(r.text)
                if not challenge:
                    raise ScrapeBlocked("challenge page had no parseable __rd_verify URL")
                p = self.transport.post(challenge, headers={"Referer": url, "Origin": ORIGIN})
                self._count(p.text)
                r = self.transport.get(url, headers=self.BOOTSTRAP_HEADERS)
                self._count(r.text)
        except FacebookError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ScrapeFailed(f"graphql bootstrap failed: {type(e).__name__}: {e}"[:300]) from e

        if r.status != 200:
            raise ScrapeBlocked(f"graphql bootstrap answered HTTP {r.status}, title={_title_of(r.text)!r}")
        self.tokens = extract_tokens(r.text)
        self.doc_id = settings.doc_id or self._discover_doc_id(r.text)
        if not self.doc_id:
            raise ScrapeBlocked("no AdLibrarySearchPaginationQuery doc_id in the page bundles; set FB_DOC_ID")
        self.minted_at = self.last_call = time.time()
        log.info("%s minted via proxy for %s/%s: doc_id %s, %d KB",
                 self.label, query, country, self.doc_id, self.decoded_bytes // 1024)

    def _discover_doc_id(self, html: str) -> str:
        """The persisted-query id, from the page's own JS bundles. They are fetched one at a
        time through a generator so the search stops at the first bundle that defines the
        operation: the id has lived in an early one every time, and a bundle is ~100 KB of
        billed proxy traffic. One bad bundle must not sink the mint."""
        urls = bundle_urls(html)

        def texts():
            for u in urls:
                try:
                    r = self.transport.get(u, headers={"Referer": self.referer})
                    self._count(r.text)
                    yield r.text
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: bundle fetch failed: %s", self.label, e)

        return discover_doc_id(texts()) or ""

    def search_page(self, query, country, cursor, collation_token, first=30, active_status="ACTIVE"):
        """One GraphQL page of ads and the cursor for the next. Typed errors for anything else."""
        if not self.tokens or not self.doc_id:
            raise SessionDead(f"{self.label}: search_page before mint")
        self.req_n += 1
        variables = build_variables(
            query=query, country=country, cursor=cursor, collation_token=collation_token,
            session_id=self.session_id, first=first, active_status=active_status, extra=variables_extra(),
        )
        form = build_form(self.tokens, self.doc_id, variables, self.req_n)
        headers = graphql_headers(self.tokens, self.referer)

        delays = (1.0, 3.0)
        for attempt in range(len(delays) + 1):
            self._pace()
            try:
                r = self.transport.post(GRAPHQL, data=form, headers=headers)
            except Exception as e:  # noqa: BLE001
                kind, body, status, text = Kind.TRANSIENT, None, 0, f"{type(e).__name__}: {e}"
            else:
                status, text = r.status, r.text
                kind, body = classify(status, text)
            self.last_call = time.time()
            self.requests_made += 1
            self._count(text)
            if kind is Kind.TRANSIENT:
                if attempt < len(delays):
                    log.warning("%s: transient (http %s), retrying in %.0fs", self.label, status or "-", delays[attempt])
                    time.sleep(delays[attempt])
                    continue
                raise ScrapeFailed(f"graphql failed after {attempt + 1} attempts: http {status or '-'} {text[:120]!r}")
            break

        if kind is Kind.RATE_LIMITED:
            self.retired = True
            raise RateLimited(f"rate limited after {self.requests_made} call(s) on {self.label}: {error_summary(body)}")
        if kind in (Kind.HTML, Kind.BAD_JSON):
            self.retired = True
            raise SessionDead(f"graphql answered http {status} with a non-JSON body on {self.label}")
        if kind is Kind.DATA_NULL:
            self.retired = True
            raise DocIdStale(f"graphql returned no data on {self.label}: {error_summary(body) or 'no error given'}")
        return extract_ads(body)


_session = None


def _live_session(query: str, country: str, status: str) -> "GraphSession":
    """The shared session, minted on first use and re-minted when it expires. Reuse is what makes
    GraphQL cheaper than the rendered page: the mint is amortised over every keyword it serves."""
    global _session
    if _session is None or not _session.ready:
        _session = GraphSession()
        _session.mint(query, country, status)
    return _session


def reset_session() -> None:
    """Drop the shared session, so a test can prove the mint cost."""
    global _session
    _session = None


# --------------------------------------------------------------------------- paging


def page_search(
    query: str,
    country: str = "US",
    active_status: str = "active",
    max_pages: int = 150,
    novelty_stop: int = 25,
    empty_tol: int = 8,
    max_ads: int = 0,
) -> dict:
    """Page one keyword and report what it cost. An ordinary empty result is not an error."""
    started = time.time()
    country = normalise_country(country)
    status = normalise_active_status(active_status)
    gql_status = ACTIVE_STATUS_GQL[status]
    max_pages = max(1, min(int(max_pages), MAX_PAGES_CEILING))

    s = _live_session(query, country, status)
    before_bytes = s.decoded_bytes
    minted_now = s.requests_made == 0
    collation = str(uuid.uuid4())
    cursor = None
    ads: list[dict] = []
    seen_ads: set[str] = set()
    advertisers: set[str] = set()
    pages: list[dict] = []
    empties = dry = 0
    stopped = f"hit the {max_pages}-page cap"

    for n in range(1, max_pages + 1):
        t0, b0 = time.time(), s.decoded_bytes
        page_ads, cursor = s.search_page(query, country, cursor, collation, 30, gql_status)
        new = set()
        for a in page_ads:
            pid = str(a.get("page_id") or (a.get("snapshot") or {}).get("page_id") or "")
            if pid and pid not in advertisers:
                new.add(pid)
            key = str(a.get("ad_archive_id") or id(a))
            if key not in seen_ads:
                seen_ads.add(key)
                ads.append(a)
        advertisers |= new
        pages.append({
            "page": n, "ads": len(page_ads), "new_advertisers": len(new),
            "advertisers_total": len(advertisers), "decoded_bytes": s.decoded_bytes - b0,
            "seconds": round(time.time() - t0, 1), "has_next": cursor is not None,
        })

        # 1. Meta says there is nothing more. The only true end.
        if cursor is None:
            stopped = "Meta dropped the cursor (true end)"
            break
        # 2. An empty page is NOT the end unless it keeps happening.
        if not page_ads:
            empties += 1
            if empty_tol and empties > empty_tol:
                stopped = f"{empties} empty pages in a row"
                break
            if not empty_tol:
                stopped = "first empty page (tolerance off)"
                break
        else:
            empties = 0
        # 3. Novelty: the tail is the same brands' duplicate ads.
        dry = 0 if new else dry + 1
        if novelty_stop and dry >= novelty_stop:
            stopped = f"{dry} pages with no new advertiser"
            break
        # 4. Optional ad target.
        if max_ads and len(ads) >= max_ads:
            stopped = f"reached the {max_ads}-ad target"
            break

    return {
        "query": query, "country": country, "active_status": status,
        "ads": ads, "advertisers": len(advertisers), "pages": len(pages),
        "empty_pages": sum(1 for p in pages if p["ads"] == 0), "stopped_because": stopped,
        "caps": {"max_pages": max_pages, "novelty_stop": novelty_stop, "empty_tol": empty_tol},
        "decoded_bytes": s.decoded_bytes - before_bytes,
        "session": {"label": s.label, "requests_made": s.requests_made, "minted_now": minted_now},
        "seconds": round(time.time() - started, 1), "pages_detail": pages,
    }
