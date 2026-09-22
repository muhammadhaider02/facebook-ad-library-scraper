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
from collections import Counter
from typing import TYPE_CHECKING, Callable, NamedTuple
from urllib.parse import quote, urlencode, urljoin, urlsplit

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
MEDIA_TYPES = ("all", "video")

# The page view (`view_all_page_id`) embeds the page's own record next to the results; a page id
# Meta does not know gives `{"page_info": null}` there, which is how "unknown page" is told apart
# from "known page, no ads". A smaller blob earlier in the document carries a `page_info` without
# `page_name`, so the record is always read from the same blob as the results.
PAGE_INFO_KEY = "ad_library_page_info"
# The public page plugin (`/plugins/page.php?href=<page url>`) is the cheapest way to turn a vanity
# URL into the numeric page id: ~45 KB, no challenge, and the id sits in one link. Measured
# 2026-09-22 (docs/architecture.md, "How a brand lookup is made").
PLUGIN_URL = ORIGIN + "/plugins/page.php"
PLUGIN_MARKER = "?ref=embed_page"
PLUGIN_ID_RE = re.compile(r"facebook\.com/(\d{6,})\?ref=embed_page")
# The profile page of a New Page Experience page is a `user` document; `userID` and `al:android:url`
# there are the user id, NOT the page id the Ad Library takes. Only `delegate_page` carries it.
DELEGATE_ID_RE = re.compile(r'"delegate_page":\{"id":"(\d+)"')
_PAGE_URL_RE = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)?facebook\.com/(.*)$", re.I)
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{2,}$")
_NOT_A_PAGE = {
    "sharer", "sharer.php", "share", "share.php", "plugins", "tr", "login", "login.php", "dialog", "help",
    "policies", "privacy", "ads", "watch", "marketplace", "groups", "events", "hashtag", "photo", "photo.php",
    "video.php", "search", "public", "settings", "home.php", "reel", "stories", "gaming", "business",
}
# Second-level labels under a two-letter TLD that are registries, not brands (co.uk, com.au, co.nz).
_SECOND_LEVEL = {"co", "com", "net", "org", "ac", "gov", "edu", "or", "ne"}


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


class Busy(FacebookError):
    """Every concurrency slot was taken for longer than the caller could wait. Answered at once
    rather than queueing past the caller's own timeout."""


class BudgetExceeded(FacebookError):
    """The lookup would need another GET that cannot finish inside its budget (the limiter's
    next slot, the pacing gap and a page fetch priced together)."""


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


class PageView(NamedTuple):
    """What a `view_all_page_id` page carries: the total, the first page of ads, and the page's
    own record (`None` when Meta does not know the page id)."""

    count: int
    ads: list[dict]
    info: dict | None

    @property
    def known(self) -> bool:
        return self.info is not None


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


def normalise_media_type(raw: str) -> str:
    key = str(raw or "all").strip().lower()
    if key not in MEDIA_TYPES:
        raise ValueError(f"invalid media_type {raw!r}: expected all or video")
    return key


def page_view_url(page_id: str, active_status: str = "active", media_type: str = "all") -> str:
    """The Ad Library page for one advertiser page: the URL its "See all ads" link opens."""
    params = {
        "active_status": normalise_active_status(active_status),
        "ad_type": "all",
        "country": "ALL",
        "view_all_page_id": str(page_id),
        "search_type": "page",
        "media_type": normalise_media_type(media_type),
    }
    return AD_LIBRARY + "?" + urlencode(params)


def domain_search_url(domain: str) -> str:
    """A keyword search for a bare domain across every country and every status: how a brand's
    page is found when only its domain is known. Always `all`, as the vendor this replaces did,
    so a brand whose ads are all retired is still found (and then counted as 0 live)."""
    return bootstrap_url(domain, "ALL", "all")


def plugin_url(slug: str) -> str:
    return PLUGIN_URL + "?href=" + quote(f"{ORIGIN}/{slug}", safe="")


def page_ref(facebook_url: str) -> tuple[str, str] | None:
    """What a Facebook page URL names: `("id", digits)` when the id is in the URL itself
    (`/p/<Name>-<id>/`, `/people/<Name>/<id>/`, `/pages/<Name>/<id>/`, `/profile.php?id=<id>`, a bare
    numeric path), `("slug", handle)` for a vanity URL, `None` for links that are not a page
    (sharer, plugins, login...). A numeric ref may still be a user id; the page view tells."""
    raw = str(facebook_url or "").strip()
    m = _PAGE_URL_RE.match(raw)
    if not m:
        return None
    path = m.group(1)
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    path = path.split("#", 1)[0].strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    head = parts[0].lower()
    if head == "profile.php":
        mid = re.search(r"(?:^|&)id=(\d{5,})", query)
        return ("id", mid.group(1)) if mid else None
    if head in ("p", "people", "pages") and len(parts) >= 2:
        tail = re.search(r"(\d{5,})$", parts[-1])
        return ("id", tail.group(1)) if tail else None
    if head in _NOT_A_PAGE:
        return None
    if re.fullmatch(r"\d{5,}", parts[0]):
        return ("id", parts[0])
    if not _SLUG_RE.match(parts[0]):
        return None
    return ("slug", parts[0])


def page_id_from_plugin(html: str) -> str | None:
    """The page id out of the page plugin. `None` means the plugin rendered and knows no such
    page; a page that is not the plugin at all (a wall, a refusal) raises SessionDead."""
    m = PLUGIN_ID_RE.search(html or "")
    if m:
        return m.group(1)
    if not is_app_page(html):
        raise SessionDead(f"the page plugin answered with something else, title={_title_of(html)!r}")
    return None


def page_id_from_profile(html: str) -> str | None:
    """The page id out of a profile page, from `delegate_page` only (never `userID`). `None`
    means Facebook rendered its "content isn't available" page; a wall raises SessionDead."""
    m = DELEGATE_ID_RE.search(html or "")
    if m:
        return m.group(1)
    if not is_app_page(html):
        raise SessionDead(f"the profile page answered with something else, title={_title_of(html)!r}")
    return None


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


def find_doc(html: str) -> dict | None:
    """The parsed JSON blob that carries `search_results_connection`, or None when the prefetch
    is absent. Only the blobs that mention it are parsed; the page has ~40 of them."""
    for blob in JSON_BLOB_RE.findall(html or ""):
        if RESULTS_MARKER not in blob:
            continue
        try:
            doc = json.loads(blob)
        except ValueError:
            continue
        if isinstance(_find_key(doc, RESULTS_MARKER), dict):
            return doc
    return None


def find_results(html: str) -> dict | None:
    """The `search_results_connection` object embedded in the page (`count`, `edges`,
    `page_info`), or None when the prefetch is absent."""
    doc = find_doc(html)
    return _find_key(doc, RESULTS_MARKER) if doc is not None else None


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


def _find_key_present(o, key: str) -> tuple[bool, object]:
    """Like `_find_key`, but tells a key holding `null` apart from a key that is absent."""
    if isinstance(o, dict):
        if key in o:
            return True, o[key]
        for v in o.values():
            found, r = _find_key_present(v, key)
            if found:
                return found, r
    elif isinstance(o, list):
        for v in o:
            found, r = _find_key_present(v, key)
            if found:
                return found, r
    return False, None


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


def find_page_record(html: str) -> dict | None:
    """The page's own record on a `view_all_page_id` page, merged from every blob that carries
    `ad_library_page_info`: one holds `page_name` and `page_is_deleted`, the results blob holds
    `hidden_ads` and `related_pages`. `None` when every copy is `{"page_info": null}` (Meta does
    not know the id) or when the key is absent (a keyword page)."""
    merged: dict = {}
    for blob in JSON_BLOB_RE.findall(html or ""):
        if PAGE_INFO_KEY not in blob:
            continue
        try:
            doc = json.loads(blob)
        except ValueError:
            continue
        present, record = _find_key_present(doc, PAGE_INFO_KEY)
        if present and isinstance(record, dict) and isinstance(record.get("page_info"), dict):
            merged.update(record["page_info"])
    return merged or None


def classify_page_view(html: str) -> tuple[Page, PageView | None]:
    """A `view_all_page_id` page: its shape, the total, its ads and the page's own record. The
    record is `None` when Meta does not know the page id (a bogus id, or a user id). Pure."""
    doc = find_doc(html)
    if doc is None:
        return Page.MISS, None
    conn = _find_key(doc, RESULTS_MARKER)
    ads = extract_ads(conn)
    try:
        count = int(conn.get("count") or 0)
    except (TypeError, ValueError):
        count = len(ads)
    return (Page.ADS if ads else Page.EMPTY), PageView(count=count, ads=ads, info=find_page_record(html))


def registrable_domain(value: str) -> str:
    """`shop.brand.co.uk`, `https://www.brand.com/x`, `Brand.com/path` -> `brand.co.uk` / `brand.com`.
    Two labels, or three under a registry second-level label (co.uk, com.au, co.nz). Good enough
    for an ownership check; not a public-suffix list."""
    s = str(value or "").strip().lower()
    if "://" in s:
        s = urlsplit(s).hostname or ""
    else:
        s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    if s.startswith("www."):
        s = s[4:]
    labels = [p for p in s.split(".") if p]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else s


def _landing_hosts(ad: dict) -> list[str]:
    snapshot = ad.get("snapshot") or {}
    values = [snapshot.get("caption"), snapshot.get("link_url")]
    values += [c.get("link_url") for c in (snapshot.get("cards") or []) if isinstance(c, dict)]
    return [registrable_domain(v) for v in values if v]


def owned_by(ad: dict, domain: str) -> bool:
    """True when the ad lands on the brand's registrable domain: its caption (the bare domain the
    Ad Library shows), its link URL, or any card's link URL."""
    domain = registrable_domain(domain)
    return bool(domain) and domain in _landing_hosts(ad)


def pick_page(ads: list[dict], domain: str) -> str | None:
    """The advertiser page behind a domain, from a keyword search on that domain: the page with
    the most ads landing on it; ties go to a page whose name carries the domain's stem, then to
    the larger page. `None` when no ad lands on the domain."""
    domain = registrable_domain(domain)
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".", 1)[0]) if domain else ""
    owned: Counter[str] = Counter()
    names: dict[str, str] = {}
    likes: dict[str, int] = {}
    for ad in ads:
        page_id = str(ad.get("page_id") or (ad.get("snapshot") or {}).get("page_id") or "")
        if not page_id or not owned_by(ad, domain):
            continue
        owned[page_id] += 1
        snapshot = ad.get("snapshot") or {}
        names.setdefault(page_id, str(ad.get("page_name") or snapshot.get("page_name") or ""))
        likes[page_id] = max(likes.get(page_id, 0), int(snapshot.get("page_like_count") or 0))
    if not owned:
        return None

    def rank(page_id: str) -> tuple:
        name = re.sub(r"[^a-z0-9]", "", names.get(page_id, "").lower())
        return (owned[page_id], 1 if stem and stem in name else 0, likes.get(page_id, 0))

    return max(owned, key=rank)


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
