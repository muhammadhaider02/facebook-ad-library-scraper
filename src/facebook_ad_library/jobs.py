"""Batch jobs: one submission carries a whole sourcing run (up to JOB_MAX_ITEMS searches and
counts), the lanes spread it out, and the caller polls for the results paired back by id.

Submit + poll rather than one long request, so n8n's 300 s node timeout never cuts a run in
half: `POST /jobs` answers at once with the id, `GET /jobs/{id}?wait_s=45` long-polls. Results
come back in submission order and every one echoes its `id`, `query` and `country` (or the page
id), so the caller can assert `results.length == items.length` and join by id - the 20 Sep pairing
defect in Stage 0's `Build DTC Prompt` is the reason that is spelled out.

Everything is in memory and bounded: JOB_STORE_MAX live jobs, JOB_TTL_S after a job finishes,
oldest-finished evicted first. A restart loses running jobs; the caller sees a 404 and treats the
run as an outage.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import service
from .config import settings
from .lanes import Dispatcher, Item, Outcome
from .scraper import Busy

log = logging.getLogger(__name__)

STATUSES = ("ok", "no_ads", "blocked", "error", "not_found")


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

    @property
    def total(self) -> int:
        return len(self.order)

    @property
    def done(self) -> int:
        return len(self.results)

    def counts(self) -> dict:
        c = {"total": self.total, "done": self.done}
        for s in STATUSES:
            c[s] = 0
        for o in self.results.values():
            c[o.status] = c.get(o.status, 0) + 1
        return c


class JobStore:
    def __init__(self, dispatcher: Dispatcher, clock: Callable[[], float] = time.time) -> None:
        self.dispatcher = dispatcher
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- submit

    def submit(self, specs: list[dict], max_tries: int = 0) -> Job:
        """`specs` are validated dicts from the API: `{"id", "kind": "search", "query", "country",
        "status", "max_ads"}` or `{"id", "kind": "count", "resolver", "value", "status", "media",
        "page_id"}`. Raises Busy when the store is full."""
        self._evict()
        with self._lock:
            live = sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))
            if live >= settings.job_store_max:
                raise Busy(f"{live} job(s) already queued or running; try later")
            job = Job(id=f"j_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}", order=[s["id"] for s in specs],
                      specs={s["id"]: s for s in specs}, submitted_at=self._clock())
            self._jobs[job.id] = job
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
            cached = service.cached_search(s["query"], s["country"], s["status"])
            if cached is not None:
                self._record(job, s["id"], Outcome("search", s["id"], "ok" if cached else "no_ads", items=cached, cached=True))
                return
            item = service.search_item(s["query"], s["country"], s["status"], id=s["id"], priority=2, deadline=deadline,
                                       max_ads=s.get("max_ads", 0), max_tries=max_tries)
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

    def _record(self, job: Job, item_id: str, outcome: Outcome) -> None:
        s = job.specs[item_id]
        if outcome.kind == "search":
            service.remember_search(outcome, s["query"], s["country"], s["status"])
        else:
            service.remember_count(outcome, s["resolver"], s["value"], s["status"], s["media"])
        with self._lock:
            job.results[item_id] = outcome
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
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.cancelled = True
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


def result_payload(job: Job, item_id: str, include_items: bool) -> dict:
    s = job.specs[item_id]
    o = job.results.get(item_id)
    if o is None:
        base = {"id": item_id, "kind": s["kind"], "status": "pending"}
        if s["kind"] == "search":
            base.update(query=s["query"], country=s["country"])
        else:
            base.update({s["resolver"]: s["value"]})
        return base
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
            "items": items if include_items else None,
        }
    res = o.brand
    return {
        **common, s["resolver"]: s["value"],
        "number_of_ads": res.count if res is not None and res.found else None,
        "page_id": res.page_id if res is not None else None,
        "page_name": res.page_name if res is not None else None,
        "envelope": service.envelope(res, s.get("max_results", 10)) if res is not None and include_items else None,
    }


def job_payload(job: Job, include_items: bool, partial: bool, lanes: dict) -> dict:
    finished = job.status in ("done", "cancelled")
    return {
        "job_id": job.id, "status": job.status,
        "submitted_at": job.submitted_at, "started_at": job.started_at, "finished_at": job.finished_at,
        "elapsed_s": round((job.finished_at or time.time()) - job.submitted_at, 1),
        "counts": job.counts(),
        "results": [result_payload(job, i, include_items) for i in job.order] if (finished or partial) else None,
        "lanes": lanes,
    }
