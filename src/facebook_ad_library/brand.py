"""One brand's ads from the Ad Library, the way the Adyntel API answered: a page id, a page URL
or a domain in; the page's total ad count and its first page of ads out.

How a lookup is made (docs/architecture.md, "How a brand lookup is made"):
  1. resolve the page id, unless it was given
       facebook_url   /p/<Name>-<id>/ and friends carry the id; a vanity handle goes to the public
                      page plugin (one ~45 KB GET, no challenge), then optionally the profile page
       company_domain a keyword search on the bare domain, every country, every status; the page
                      with the most ads landing on the brand's registrable domain is the brand
  2. GET the page view (`view_all_page_id`) with the requested status and media filter; its
     embedded blob carries the total count and up to 30 ads, sorted by lifetime impressions
Every GET is priced against a budget sized for the n8n Code nodes that call this (30 s), and a
lookup that cannot make its next GET in time answers a 503 instead of running past the caller.

Three outcomes, kept apart on purpose because the workflows route on them:
  found      the page exists; `count` may be 0
  not found  no page for the input (an unknown id, a handle the plugin does not know, a domain
             with no ad landing on it); `BrandResult.found` is False, and the API answers `{}`
  error      a FacebookError; the API answers 503
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from . import scraper as wire
from .config import settings
from .scraper import (
    BudgetExceeded,
    Page,
    PageView,
    RateLimited,
    ResultsMissing,
    ScrapeBlocked,
    ScrapeFailed,
    SessionDead,
)

if TYPE_CHECKING:  # pragma: no cover
    from .session import Resp, _Lease, _SessionPool

log = logging.getLogger(__name__)

RESOLVERS = ("page_id", "facebook_url", "company_domain")


@dataclass
class BrandResult:
    resolver: str
    query: str  # the id, URL or domain as given
    active_status: str
    media_type: str
    found: bool = False
    page_id: str | None = None
    count: int = 0
    ads: list[dict] | None = None
    info: dict | None = None
    note: str = ""  # why not found
    attempts: int = 0  # Ad Library page GETs that answered with the page
    misses: int = 0  # of those, pages without the results blob
    short_counts: int = 0  # of those, pages whose total was below the ads on them (the count served unfilled)
    plain_gets: int = 0  # plugin and profile GETs
    withheld: int = 0  # pages that carried a total above zero and no ads at all
    recovery_gets: int = 0  # of those, GETs re-made through the fallback proxy
    recovered: bool = False  # whether the ads came back that way
    queue_s: float = 0.0  # time spent waiting for a concurrency slot
    seconds: float = 0.0
    session_swaps: int = 0

    @property
    def page_name(self) -> str | None:
        return (self.info or {}).get("page_name")


def lookup_cost_s() -> float:
    """What one more GET is expected to take, without the limiter's wait, which is asked for
    separately: the pacing gap plus a page's typical fetch and parse with slack."""
    return settings.spacing_max_s + 5.0


class _Run:
    """The state of one lookup: its lease, its deadline and the recovery ladder every GET shares."""

    def __init__(self, lease: "_Lease", deadline: float, result: BrandResult, recovery_pool: "_SessionPool | None" = None) -> None:
        self.lease = lease
        self.deadline = deadline
        self.result = result
        self.recovery_pool = recovery_pool
        self.gets = 0

    def _guard(self, what: str) -> None:
        """Refuse a GET that could not finish in time. The first GET is never refused: the caller
        came for an answer, and a slow first page is still an answer."""
        if not self.gets:
            return
        remaining = self.deadline - time.time()
        need = self.lease.session.limiter.eta() + lookup_cost_s()
        if remaining < need:
            raise BudgetExceeded(f"no budget left for the {what} ({remaining:.0f}s left, {need:.0f}s needed) after {self.gets} GET(s)")

    def _recover(self, e: Exception, what: str) -> None:
        """Retire the session and swap once; a second refusal escapes. No sleeping on a 429: the
        budget is 25 s and the nap is 60."""
        session = self.lease.session
        reason = "rate_limited" if isinstance(e, RateLimited) else "session_dead" if isinstance(e, SessionDead) else "failed"
        session.retire(reason)
        if self.lease.swaps or self.deadline - time.time() < lookup_cost_s():
            if isinstance(e, SessionDead):
                raise ScrapeBlocked(f"two fresh sessions were refused in a row on the {what}: {e}") from e
            raise e
        log.warning("%s: %s on %s; swapping sessions", what, type(e).__name__, session.label)
        self.lease.replace()
        self.result.session_swaps = self.lease.swaps

    def page(self, url: str, what: str) -> tuple[Page, PageView, str]:
        """One Ad Library page, with the miss retries and the swap. A page whose total is below the
        ads it carries is refetched like a miss: Meta now and then serves the count unfilled (seen in
        production on 23 Sep 2026: `count: 0` above 30 video ads, 55 on the next fetch), and a total
        of 0 on a page full of ads would fail the 50-ads gate for a brand that clears it. When every
        attempt is short, the ads on the page are the floor."""
        misses = 0
        short = 0
        while True:
            self._guard(what)
            try:
                kind, _, html = self.lease.session.fetch_url(url, self.deadline)
            except (RateLimited, SessionDead, ScrapeFailed) as e:
                self._recover(e, what)
                continue
            self.gets += 1
            self.result.attempts += 1
            if kind is Page.MISS:
                misses += 1
                self.result.misses += 1
                if misses > settings.brand_ssr_retries:
                    raise ResultsMissing(f"the {what} came without results {misses} time(s) in a row")
                log.info("%s: page without results (%d of %d), retrying", what, misses, settings.brand_ssr_retries + 1)
                continue
            _, view = wire.classify_page_view(html)
            if view is not None and view.count < len(view.ads):
                short += 1
                self.result.short_counts += 1
                if short <= settings.brand_ssr_retries:
                    log.info("%s: total %d below the %d ads on the page (%d of %d), retrying", what, view.count, len(view.ads), short, settings.brand_ssr_retries + 1)
                    continue
                log.warning("%s: total still %d below the %d ads on the page; the ads are the floor", what, view.count, len(view.ads))
                view = view._replace(count=len(view.ads))
            # THE THROTTLE. A total above zero with no ads at all is not a page that has none;
            # it is this address being refused the payload. Everything downstream needs the ads:
            # the domain search picks the owning page by where its ads land, and the caller
            # verifies a brand the same way. Without them the brand is rejected and one of its
            # three retries is spent on a fault that was never the brand's.
            if view is not None and view.count > 0 and not view.ads:
                self.result.withheld += 1
                recovered = self.recover(url, what)
                if recovered is not None:
                    return Page.ADS, recovered, html
            return kind, view, html

    def recover(self, url: str, what: str) -> "PageView | None":
        """The same page, fetched through the recovery pool. `None` when there is no recovery
        pool (a lane: the page view is already on a residential exit, so the honest answer is
        "withheld on this exit" and the lane runner retries it on another lane) or the attempt
        did not produce ads.

        Meta serves a throttled address the page, the page name and a correct total, and simply
        omits the ads - no 403, no 429, nothing to catch. Measured on the VPS 24 Sep 2026: page
        775991435791863 answered count=1039 ads=0 direct, and count=1039 ads=30 through a
        residential exit in the same minute. Only a page that showed that signature is refetched,
        so an honestly empty page never costs a billed GET."""
        if self.recovery_pool is None:
            return None
        log.warning("%s: total above zero with no ads; refetching through the fallback proxy", what)
        try:
            with self.recovery_pool.lease(timeout=max(0.5, self.deadline - time.time())) as lease:
                _, _, html = lease.session.fetch_url(url, self.deadline)
        except (RateLimited, SessionDead, ScrapeFailed, ScrapeBlocked, BudgetExceeded) as e:
            log.warning("%s: the fallback proxy did not answer either: %s", what, e)
            return None
        self.result.recovery_gets += 1
        _, view = wire.classify_page_view(html)
        if view is None or not view.ads:
            return None
        self.result.recovered = True
        log.info("%s: recovered %d ad(s) through the fallback proxy", what, len(view.ads))
        return view

    def plain(self, url: str, what: str) -> "Resp":
        """One non-Ad-Library page (the plugin, a profile), with the swap."""
        while True:
            self._guard(what)
            try:
                r = self.lease.session.get_plain(url, self.deadline)
            except (RateLimited, SessionDead, ScrapeFailed) as e:
                self._recover(e, what)
                continue
            self.gets += 1
            self.result.plain_gets += 1
            return r

    def plain_read(self, url: str, reader: Callable[[str], str | None], what: str) -> str | None:
        """One non-Ad-Library page, read by `reader`. A wall (a 200 that is not the page) is a
        dead session: swap once and fetch the same page on the fresh one; a second wall escapes."""
        for attempt in (1, 2):
            r = self.plain(url, what)
            try:
                return reader(r.text)
            except SessionDead as e:
                if attempt == 2:
                    raise ScrapeBlocked(f"two fresh sessions got a wall on the {what}: {e}") from e
                self._recover(e, what)
        return None  # pragma: no cover


def lookup(
    *,
    page_id: str | int | None = None,
    facebook_url: str | None = None,
    company_domain: str | None = None,
    active_status: str = "active",
    media_type: str = "all",
    pool: "_SessionPool",
    deadline: float | None = None,
    recovery_pool: "_SessionPool | None" = None,
) -> BrandResult:
    """The Ad Library's answer for one brand. Exactly one of `page_id`, `facebook_url` and
    `company_domain` is used, in that order of preference. `recovery_pool` is where a withheld
    page view is refetched; a lane passes none and handles the withheld page itself."""
    status = wire.normalise_active_status(active_status)
    media = wire.normalise_media_type(media_type)
    if media == "video":
        status = "active"  # the vendor this replaces counted live video ads only; keep its meaning
    started = time.time()
    deadline = deadline if deadline is not None else started + settings.brand_budget_s

    if page_id not in (None, ""):
        result = BrandResult("page_id", str(page_id).strip(), status, media)
    elif facebook_url:
        result = BrandResult("facebook_url", str(facebook_url).strip(), status, media)
    elif company_domain:
        result = BrandResult("company_domain", str(company_domain).strip(), status, media)
    else:
        raise ValueError("invalid request: one of `page_id`, `facebook_url` or `company_domain` is required")

    # What can be decided without a GET.
    ref: tuple[str, str] | None = None
    domain = ""
    if result.resolver == "page_id":
        if not result.query.isdigit():
            raise ValueError(f"invalid page_id {result.query!r}: expected digits")
    elif result.resolver == "facebook_url":
        ref = wire.page_ref(result.query)
        if ref is None:
            result.note = "not a Facebook page URL"
            result.seconds = round(time.time() - started, 1)
            return result
    else:
        domain = wire.registrable_domain(result.query)
        if not domain or "." not in domain:
            raise ValueError(f"invalid company_domain {result.query!r}")

    queued = time.time()
    with pool.lease(timeout=max(0.5, deadline - queued)) as lease:
        result.queue_s = round(time.time() - queued, 1)
        run = _Run(lease, deadline, result, recovery_pool)

        # 1. resolve
        candidate: str | None = None
        slug: str | None = None
        if result.resolver == "page_id":
            candidate = result.query
        elif result.resolver == "facebook_url":
            kind, value = ref  # type: ignore[misc]
            if kind == "id":
                candidate, slug = value, value  # a numeric path may be a user id; the plugin resolves those too
            else:
                slug = value
        else:
            kind, view, _ = run.page(wire.domain_search_url(domain), f"domain search for {domain}")
            if kind is Page.EMPTY:
                result.note = f"no ad mentions {domain}"
            else:
                candidate = wire.pick_page(view.ads, domain)
                if candidate is None:
                    result.note = f"no ad among the {len(view.ads)} for {domain} lands on it"
            if candidate is None:
                result.seconds = round(time.time() - started, 1)
                return result

        # 2. the page view; a numeric URL that Meta does not know as a page goes through the plugin once
        tried_slug = False
        while True:
            if candidate is None and slug is not None and not tried_slug:
                tried_slug = True
                candidate = _resolve_slug(run, slug)
                if candidate is None:
                    result.note = f"no page for facebook.com/{slug}"
                    break
            if candidate is None:
                break
            kind, view, _ = run.page(wire.page_view_url(candidate, status, media), f"page view of {candidate}")
            if view.known:
                result.found = True
                result.page_id = candidate
                result.count = view.count
                result.ads = view.ads
                result.info = view.info
                break
            if slug is not None and not tried_slug:
                candidate = None  # a user id, most likely: resolve the path as a handle
                continue
            result.note = f"the Ad Library does not know page id {candidate}"
            break

    result.seconds = round(time.time() - started, 1)
    return result


def _resolve_slug(run: _Run, slug: str) -> str | None:
    """A vanity handle to a page id: the public page plugin, then (when enabled) the profile page.
    `None` when both render and neither knows the page."""
    page_id = run.plain_read(wire.plugin_url(slug), wire.page_id_from_plugin, f"page plugin for {slug}")
    if page_id or not settings.brand_profile_fallback:
        return page_id
    return run.plain_read(f"{wire.ORIGIN}/{slug}", wire.page_id_from_profile, f"profile page of {slug}")
