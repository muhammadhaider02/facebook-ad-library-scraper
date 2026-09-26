"""Batch jobs: one submission carries a whole sourcing run (up to JOB_MAX_ITEMS searches and
counts), the lanes spread it out, and the caller polls for the results paired back by id.

Submit + poll rather than one long request, so n8n's 300 s node timeout never cuts a run in
half: `POST /jobs` answers at once with the id, `GET /jobs/{id}?wait_s=45` long-polls. Results
come back in submission order and every one echoes its `id`, `query` and `country` (or the page
id), so the caller can assert `results.length == items.length` and join by id - a pairing defect in
an earlier caller, which matched results by position, is the reason that is spelled out.

Everything is in memory and bounded: JOB_STORE_MAX live jobs, JOB_TTL_S after a job finishes,
oldest-finished evicted first. A restart loses running jobs; the caller sees a 404 and treats the
run as an outage.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Callable

from . import fetch as fetch_mod
from . import service
from .config import settings
from .lanes import Dispatcher, Item, Outcome
from .scraper import Busy

log = logging.getLogger(__name__)

STATUSES = ("ok", "no_ads", "blocked", "error", "not_found")
FETCH_STATUSES = ("ok", "failed", "timeout", "error")
# A retry on another exit is worth it when the site never answered, refused a bot wall or was
# overloaded; a 404 or a parked domain answers the same from every exit.
_RETRY_STATUS = {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_MIN_TRY_S = 3.0


@dataclass
class FetchOutcome:
    """A homepage fetch item's answer. `status`: ok | failed | timeout | error (cancelled)."""
    kind: str
    id: str
    status: str
    url: str
    http_status: int | None = None
    final_url: str | None = None
    summary: dict | None = None
    error: str | None = None
    lanes: list = field(default_factory=list)
    tries: list = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class Job:
    id: str
    order: list[str]
    specs: dict[str, dict]  # id -> the validated spec (kind, query, country, ... / resolver, value, ...)
    submitted_at: float
    status: str = "queued"  # queued | running | done | cancelled
    started_at: float | None = None
    finished_at: float | None = None
    results: dict[str, Outcome] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    cancelled: bool = False
    kind: str = "lanes"  # lanes (searches and counts) | fetch (homepages)
    deadline_at: float | None = None  # fetch jobs: unfinished items answer `timeout` after this

    @property
    def total(self) -> int:
        return len(self.order)

    @property
    def done(self) -> int:
        return len(self.results)

    def counts(self) -> dict:
        c = {"total": self.total, "done": self.done}
        for s in (FETCH_STATUSES if self.kind == "fetch" else STATUSES):
            c[s] = 0
        for o in self.results.values():
            c[o.status] = c.get(o.status, 0) + 1
        return c


class JobStore:
    def __init__(self, dispatcher: Dispatcher, clock: Callable[[], float] = time.time,
                 fetcher: Callable[[str, float, str | None], dict] | None = None, executor: Executor | None = None) -> None:
        """`fetcher(url, timeout_s, exclude_lane)` reads one homepage and returns fetch.fetch_page's
        dict plus `lane`; `executor` runs the fetch items (api.py hands in its fetch pool)."""
        self.dispatcher = dispatcher
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.fetcher = fetcher
        self.executor = executor

    # ----------------------------------------------------------------- submit

    def submit(self, specs: list[dict], max_tries: int = 0, deadline_s: float | None = None) -> Job:
        """`specs` are validated dicts from the API: `{"id", "kind": "search", "query", "country",
        "status", "max_ads"}`, `{"id", "kind": "count", "resolver", "value", "status", "media",
        "page_id"}`, or - a job of its own - `{"id", "kind": "fetch", "url", "invalid"}`. Raises
        Busy when the store is full."""
        self._evict()
        fetch_job = bool(specs) and specs[0]["kind"] == "fetch"
        with self._lock:
            live = sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))
            if live >= settings.job_store_max:
                raise Busy(f"{live} job(s) already queued or running; try later")
            job = Job(id=f"j_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}", order=[s["id"] for s in specs],
                      specs={s["id"]: s for s in specs}, submitted_at=self._clock())
            if fetch_job:
                job.kind = "fetch"
                job.deadline_at = job.submitted_at + (deadline_s if deadline_s and deadline_s > 0 else settings.fetch_job_deadline_s)
            self._jobs[job.id] = job
        if fetch_job:
            job.status = "running"
            job.started_at = self._clock()
            for s in specs:
                self._submit_fetch(job, s)
            self._maybe_finish(job)
            return job
        deadline = self._clock() + settings.job_item_max_wait_s
        for s in specs:
            self._submit_one(job, s, deadline, max_tries)
        if not job.results or job.done < job.total:
            job.status = "running"
            job.started_at = job.started_at or self._clock()
        self._maybe_finish(job)
        return job

    def _submit_one(self, job: Job, s: dict, deadline: float, max_tries: int) -> None:
        if s["kind"] == "search":
            deep = int(s.get("max_pages", 1) or 1) > 1
            # A deep item asks for more than the rendered page holds, so the cache (a rendered
            # page's 30 ads) is never an answer to it and its result is never cached over one.
            cached = None if deep else service.cached_search(s["query"], s["country"], s["status"])
            if cached is not None:
                self._record(job, s["id"], Outcome("search", s["id"], "ok" if cached else "no_ads", items=cached, cached=True))
                return
            item = service.search_item(s["query"], s["country"], s["status"], id=s["id"], priority=2, deadline=deadline,
                                       max_ads=s.get("max_ads", 0), max_tries=max_tries)
            if deep:
                item.max_pages = int(s["max_pages"])
                item.novelty_stop = int(s.get("novelty_stop", settings.page_novelty_stop))
                item.empty_tol = int(s.get("empty_tol", settings.page_empty_tol))
                item.budget_s = min(settings.page_budget_s, max(30.0, deadline - time.time()))
        else:
            page_id, cached, _ = service.cached_count(s["resolver"], s["value"], s["status"], s["media"])
            if cached is not None:
                self._record(job, s["id"], Outcome("count", s["id"], "ok" if cached.found else "not_found", count=cached.count, brand=cached, cached=True))
                return
            item = service.count_item(s["resolver"], s["value"], s["status"], s["media"], page_id=page_id, id=s["id"],
                                      priority=2, deadline=deadline, max_tries=max_tries)
        item.on_done = lambda it, out, job=job: self._record(job, it.id, out)
        job.items[s["id"]] = item
        try:
            self.dispatcher.submit(item)
        except Busy as e:
            self._record(job, s["id"], Outcome(item.kind, s["id"], "error", error=str(e), error_type="Busy"))

    # ----------------------------------------------------------------- fetch items

    def _submit_fetch(self, job: Job, s: dict) -> None:
        if s.get("invalid"):
            self._record(job, s["id"], FetchOutcome("fetch", s["id"], "failed", s["url"], error=s["invalid"]))
            return
        if self.fetcher is None or self.executor is None:
            self._record(job, s["id"], FetchOutcome("fetch", s["id"], "error", s["url"], error="no fetcher configured"))
            return
        self.executor.submit(self._run_fetch, job, s)

    def _run_fetch(self, job: Job, s: dict) -> None:
        """At most two tries, the second on another exit (and on www. after a connection error),
        each inside what is left of the job's deadline. Never raises: the item always answers."""
        started = self._clock()
        url, exclude, tries, lanes, last = s["url"], None, [], [], None
        try:
            for attempt in range(2):
                left = (job.deadline_at or started + settings.fetch_job_deadline_s) - self._clock()
                if job.cancelled or s["id"] in job.results or left < _MIN_TRY_S:
                    break
                out = self.fetcher(url, min(settings.fetch_timeout_s, left), exclude)
                last = out
                lanes.append(out.get("lane"))
                tries.append({"url": url, "lane": out.get("lane"), "status": out.get("status"), "ok": bool(out.get("ok")),
                              "error": out.get("error"), "seconds": out.get("seconds")})
                if out.get("ok"):
                    break
                connection = fetch_mod.is_connection_error(out.get("error"))
                if not connection and out.get("status") not in _RETRY_STATUS:
                    break
                exclude = out.get("lane")
                if connection and attempt == 0:
                    url = fetch_mod.www_variant(url) or url
        except Exception as e:  # noqa: BLE001 - a fetch item always answers
            last = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        if last is None:
            status, error = ("error", "cancelled") if job.cancelled else ("timeout", "deadline reached before a try")
            outcome = FetchOutcome("fetch", s["id"], status, s["url"], error=error, seconds=round(self._clock() - started, 2))
        else:
            outcome = FetchOutcome("fetch", s["id"], "ok" if last.get("ok") else "failed", s["url"], http_status=last.get("status"),
                                   final_url=last.get("final_url"), summary=last.get("summary"), error=last.get("error"),
                                   lanes=lanes, tries=tries, seconds=round(self._clock() - started, 2))
        self._record(job, s["id"], outcome)

    def expire(self, job: Job) -> None:
        """A fetch job past its deadline: every unfinished item answers `timeout` and the job ends.
        A try still running finishes in its thread; its late answer is ignored by `_record`."""
        if job.kind != "fetch" or job.deadline_at is None or job.status in ("done", "cancelled") or self._clock() < job.deadline_at:
            return
        for item_id in job.order:
            if item_id not in job.results:
                s = job.specs[item_id]
                self._record(job, item_id, FetchOutcome("fetch", item_id, "timeout", s["url"], error="job deadline reached"))

    # ----------------------------------------------------------------- record

    def _record(self, job: Job, item_id: str, outcome) -> None:
        with self._lock:
            if item_id in job.results:
                return  # a fetch that finished after its item timed out
            job.results[item_id] = outcome
        s = job.specs[item_id]
        if outcome.kind == "search":
            if int(s.get("max_pages", 1) or 1) <= 1:
                service.remember_search(outcome, s["query"], s["country"], s["status"])
        elif outcome.kind == "count":
            service.remember_count(outcome, s["resolver"], s["value"], s["status"], s["media"])
        self._maybe_finish(job)

    def _maybe_finish(self, job: Job) -> None:
        with self._lock:
            if job.done >= job.total and job.status not in ("done", "cancelled"):
                job.status = "cancelled" if job.cancelled else "done"
                job.finished_at = self._clock()
                c = job.counts()
                log.info("job %s finished: %s in %.0fs", job.id, {k: v for k, v in c.items() if k != "total"},
                         job.finished_at - job.submitted_at)

    # ----------------------------------------------------------------- read

    def get(self, job_id: str) -> Job | None:
        self._evict()
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            self.expire(job)
        return job

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.cancelled = True
        if job.kind == "fetch":
            for item_id in job.order:
                if item_id not in job.results:
                    self._record(job, item_id, FetchOutcome("fetch", item_id, "error", job.specs[item_id]["url"], error="cancelled"))
            return job
        for item_id, item in list(job.items.items()):
            if item_id not in job.results:
                self.dispatcher.cancel(item)
        return job

    def _evict(self) -> None:
        now = self._clock()
        with self._lock:
            for jid, j in list(self._jobs.items()):
                if j.finished_at is not None and now - j.finished_at > settings.job_ttl_s:
                    del self._jobs[jid]
            finished = sorted((j for j in self._jobs.values() if j.finished_at is not None), key=lambda j: j.finished_at)
            while len(self._jobs) > settings.job_store_max and finished:
                del self._jobs[finished.pop(0).id]

    def snapshot(self) -> dict:
        with self._lock:
            jobs = list(self._jobs.values())
        return {
            "queued": sum(1 for j in jobs if j.status == "queued"),
            "running": sum(1 for j in jobs if j.status == "running"),
            "done": sum(1 for j in jobs if j.status in ("done", "cancelled")),
            "queue_depth": self.dispatcher.queue_depth,
            "store": len(jobs),
        }


# --------------------------------------------------------------------------- shaping


def result_payload(job: Job, item_id: str, include_items: str) -> dict:
    """`include_items`: "0" (no ad arrays or envelopes), "1" (everything), "lite" (service.lite_item per ad),
    "brands" (no ads at all: service.brand_lines per search, one line per advertiser page and domain)."""
    s = job.specs[item_id]
    o = job.results.get(item_id)
    if o is None:
        base = {"id": item_id, "kind": s["kind"], "status": "pending"}
        if s["kind"] == "search":
            base.update(query=s["query"], country=s["country"])
        elif s["kind"] == "fetch":
            base.update(url=s["url"])
        else:
            base.update({s["resolver"]: s["value"]})
        return base
    if isinstance(o, FetchOutcome):
        return {"id": item_id, "kind": "fetch", "status": o.status, "url": o.url, "http_status": o.http_status, "final_url": o.final_url,
                "summary": o.summary, "error": o.error, "lanes": o.lanes, "tries": o.tries, "seconds": o.seconds}
    common = {
        "id": item_id, "kind": o.kind, "status": o.status, "lane": f"lane-{o.lane}" if o.lane else None, "exit_ip": o.exit_ip,
        "tries": o.tries, "seconds": o.seconds, "decoded_bytes": o.decoded_bytes, "cached": o.cached,
        "error": (f"{o.error_type}: {o.error}" if o.error_type and o.error else (o.error or None)),
    }
    if o.kind == "search":
        items = service.search_items(o, s["query"], s["country"])
        return {
            **common, "query": s["query"], "country": s["country"], "ads_found": len(items),
            "reported_total": o.count, "direct_skipped": o.direct_skipped,
            "items": ([service.lite_item(i) for i in items] if include_items == "lite" else items) if include_items in ("1", "lite") else None,
            "brands": service.brand_lines(items) if include_items == "brands" else None,
        }
    res = o.brand
    return {
        **common, s["resolver"]: s["value"],
        "number_of_ads": res.count if res is not None and res.found else None,
        "page_id": res.page_id if res is not None else None,
        "page_name": res.page_name if res is not None else None,
        "envelope": service.envelope(res, s.get("max_results", 10)) if res is not None and include_items != "0" else None,
    }


def job_payload(job: Job, include_items: str, partial: bool, lanes: dict) -> dict:
    finished = job.status in ("done", "cancelled")
    return {
        "job_id": job.id, "status": job.status,
        "submitted_at": job.submitted_at, "started_at": job.started_at, "finished_at": job.finished_at,
        "elapsed_s": round((job.finished_at or time.time()) - job.submitted_at, 1),
        "counts": job.counts(),
        "results": [result_payload(job, i, include_items) for i in job.order] if (finished or partial) else None,
        "lanes": lanes,
    }
