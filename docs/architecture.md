# Architecture

## What it replaces

Stage 0 of the SmartLead pipeline (`00 · Find Brands`, `CtyWWHDoi0316VU7`, on the VPS n8n) calls the Apify actor `igolaizola~facebook-ad-library-scraper` once per keyword-and-country pair, from the node `Apify: Facebook Ad Library`:

| | |
|---|---|
| Endpoint | `POST https://api.apify.com/v2/acts/igolaizola~facebook-ad-library-scraper/run-sync-get-dataset-items?maxTotalChargeUsd=1` |
| Body | `{"maxItems": 80, "query": "<keyword>", "country": "<US\|GB\|CA\|AU\|NZ>", "category": "all", "mediaType": "all", "activeStatus": "active", "advertisers": [], "fetchDetails": true}` |
| Cadence | hourly at :45, up to 40 pairs a run, so up to 960 calls a day |
| Cost | $188.84 over 13,530 runs in the last billing cycle, about $0.014 a run, plus the actor rental |
| Node timeout | 300 s |

These facts are recorded in `facebook.md` (19 Sep 2026) from the live workflow; this service was written against that record, and the workflow itself has not been edited.

What `Extract Dedupe And Filter` does with the rows decides what this service has to return. It reads, with fallbacks because the actor moved fields between versions: `page_name`, `page_id`, `page_profile_uri` or `page_url`, `page_alias`, `page_category` or `page_categories[0]`, `page_like_count` or `page_likes` (in practice `snapshot.page_like_count`), `snapshot.caption` (the bare domain, the source of the brand's domain on nearly every ad), `snapshot.link_url` (second choice), and `_details.advertiser.page.about.text` (third choice, never used in production). An item carrying `error` is counted as an error, not as "no ads". Everything else the actor emitted (creative text, images, video, CTA, dates, impressions) is not read.

Three consequences for this code:

1. Every one of those paths is present on every item, `null` when Meta has no value, and the ones that live in two places (`page_like_count`/`page_likes`, `page_profile_uri`/`page_url`, top level and `snapshot`) carry the same value in both.
2. `_details` is always `null` and `page_alias` is always `""`. The actor filled them from a second per-ad query (`fetchDetails: true`) that costs a request per ad; the page does not carry them, and the workflow's third-choice domain fallback never fired in production. `page_alias` empty is what the node saw from the actor too.
3. A failure is an HTTP status with one `{"error": {...}}` object, never an empty array, so the node's error check fires. `200 []` is reserved for a search that genuinely found nothing.

## How a search is made

The Ad Library is a logged-out React site. When its search page is requested, Meta runs the search on the server and embeds the first page of results in the HTML, as a prefetched Relay stream that is byte-for-byte what the frontend's own first GraphQL call would return. This service reads that and nothing else: **one GET per keyword-and-country pair**. Measured against the live site on 19 Sep 2026 from a laptop and from the VPS ([address-classification.md](address-classification.md) has the runs):

1. **The GET.** `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=NZ&q=<keyword>&search_type=keyword_unordered&media_type=all`, the URL the site's own address bar shows. On a fresh cookie jar it answers `403 Client challenge` with a 481-byte page whose script does `fetch('/__rd_verify_<token>?challenge=3', {method: 'POST'})` and reloads. The path is relative. One POST to it returns `200` and a `rd_challenge` cookie (`Max-Age=86400`); the re-GET returns the page and sets `datr`. Later GETs on that jar are not challenged: 41 in a row were measured without one.
2. **The page.** 0.6 to 1.7 MB of HTML with about 40 `<script type="application/json">` blobs. One of them, when present, holds `RelayPrefetchedStreamCache` → `result.data.ad_library_main.search_results_connection` with `edges[].node.collated_results[]` and a `page_info`. The service parses only the blob that mentions `search_results_connection` and flattens the collated results, unique by `ad_archive_id`, in page order. Up to 30 ads per page; 15 to 20 distinct advertisers among them on the productive keywords measured.
3. **Three shapes.** The page comes in three shapes, from every address, decided per request on Meta's side:

   | Shape | Size | Marker | Meaning | What the service does |
   |---|---|---|---|---|
   | ads | 0.6 to 1.7 MB | results blob with edges | the result | `200` with the items |
   | empty | ~582 KB | results blob with no edges | the keyword has no active ads in that country | `200 []` |
   | miss | ~573 KB | no results blob | Meta skipped the server-side prefetch; about 1 in 4 requests | retry, up to `SSR_RETRIES` (2) more GETs; if every attempt misses, `503 ResultsMissing` |

   A miss is never turned into an empty list, because Stage 0 reads an empty list as "no inventory" and retires a keyword after two of them. The real empty result is distinguishable, so the retry never loops on a keyword that is simply dry.

Why not the GraphQL endpoint the frontend uses to scroll past the first page: Meta answers `POST /api/graphql/` from datacenter addresses (Hostinger, GitHub's runners) with error `1675004` on the very first call of a fresh session, keyed on the source address, while it serves the page to the same address without a throttle. From a residential address it gave about 10 ads a call regardless of the `first` asked for, so three calls, which is what the previous design made, gave the same 30 the page gives in one. Nothing is lost.

## Why no browser, and why Chrome TLS

The Ad Library's only gate is the challenge above plus a TLS-fingerprint check. Measured 19 Sep 2026: plain `curl` clears the challenge and receives the cookie, and is then answered with a `400 Sorry, something went wrong` error page on every request after it, including the homepage. `curl_cffi` with `impersonate="chrome"` is answered normally. So there is no Chromium in this service: one Python process, one curl handle per session.

`FB_IMPERSONATE` is `chrome`, an alias for the newest Chrome profile the installed `curl_cffi` knows (`chrome150` in 0.16.3). A `400` after a clean challenge is the fingerprint being rejected; the service names that symptom in its `ScrapeBlocked` message.

## Sessions

The unit of identity is a session, not a request: one cookie jar (`datr`, `rd_challenge`), created empty and filled by its first GET. `FbSession` in `session.py` holds one; `_SessionPool` keeps `SESSION_POOL_SIZE` (2) of them warm, hands them out round-robin under a `MAX_CONCURRENCY` (2) semaphore, creates lazily on the first lease, and drops a session on its way back if it is retired or expired. A session expires after `SESSION_MAX_REQUESTS` (200) GETs or `SESSION_MAX_AGE_S` (7,200 s), both starting points rather than measured limits; `/health` reports `retired_by_reason` so the limit that actually bites can be seen. A session that gets `MISS_STREAK_RETIRE` (5) pages without results in a row is retired as suspect; a real empty result resets the streak.

Pacing has two layers: a random gap of `SPACING_MIN_S` to `SPACING_MAX_S` (2 to 5 s) between GETs on one session, and a process-wide ceiling of `RATE_LIMIT_PER_MIN` (4) GETs a minute across all sessions. Stage 0 needs one GET per pair, 40 pairs an hour, so under one a minute even with retries; 4 is the pace the measured runs were made at.

## Retries and the error ladder

`fetch` classifies every answer and raises a typed error for anything that is not an Ad Library page. `search` then applies the recovery rules:

| Answer | Raised | What `search` does |
|---|---|---|
| `5xx` or a network error | `ScrapeFailed` after in-call retries at 1 s and 3 s | retire the session, swap once |
| `429` | `RateLimited` | sleep `RATE_LIMIT_SLEEP_S` (60) once and retry on the same session; a second one retires it and swaps once |
| `400` (the TLS symptom), `403` without the challenge marker, a challenge page with no URL | `ScrapeBlocked` | raise; a new jar would not help |
| any other non-`200`, or a `200` that is not the Ad Library page (no `LSD` token blob) | `SessionDead` | retire the session, swap once |
| a page without the results blob | (not an error yet) | retry, up to `SSR_RETRIES` more GETs on the same session |

One session swap is allowed per search. A second failure escapes as the API's `503`; two `SessionDead`s in a row become `ScrapeBlocked`. Every retry, swap and sleep is priced against the deadline before it starts; the first GET is never skipped.

| Status | Type | Cause |
|---|---|---|
| `400` | `ValueError` | no `query`, a country that is not two letters or `ALL`, or an unknown `activeStatus` |
| `503` | `ScrapeBlocked` | challenge would not clear, `403` without the challenge marker, `400` after the challenge, or two dead sessions in a row |
| `503` | `RateLimited` | `429` again after the one sleep, on a fresh session too |
| `503` | `ResultsMissing` | every attempt inside the budget came back without the results blob |
| `503` | `ScrapeFailed` | network failure or `5xx` that survived the retries |

## The time budget

The numbers only make sense together, and `.env.example` carries the arithmetic:

| | |
|---|---|
| one GET, worst case | `SPACING_MAX_S` + 60 / `RATE_LIMIT_PER_MIN` + `REQUEST_TIMEOUT_S` = 5 + 15 + 30 = 50 s |
| one search, worst case | (1 + `SSR_RETRIES`) × 50 s + `RATE_LIMIT_SLEEP_S` 60 s = 210 s |
| Stage 0 node timeout | 300 s |
| measured, laptop, no contention | 3.6 to 6.4 s for one GET including the challenge; 45 s when a fifth GET inside a minute waited for the limiter |

`SCRAPE_BUDGET_S` (240) is what protects the workflow. Nothing cancels a request once it starts: Starlette does not cancel a handler when the client disconnects and the search runs in a thread, so a caller that gives up does not free the session. Before each GET after the first the search checks that a whole GET's worst case still fits before the deadline; if not, a search that has only misses so far is a `503`. `stop_grace_period` in `docker-compose.yml` is 250 s so a redeploy cannot kill a search mid-budget.

## Cache

`api.py` keeps the whole page's items for `CACHE_TTL_S` (86,400 s), keyed on the normalised query, the upper-cased country and `activeStatus`; `maxItems` is applied on the way out, so a request for 30 and one for 80 share one entry. Stage 0 re-searches an exhausted keyword's remaining country slots and retries a pair on error, so the same request arrives more than once a day; a hit costs nothing and is answered in a few milliseconds with `X-Cache: hit`. Empty results live `CACHE_EMPTY_TTL_S` (3,600 s). The cache is bounded at `CACHE_MAX_ENTRIES` (2,000) and lives in memory, so a restart clears it.

## Memory and concurrency

One Python process with no browser. Each in-flight search holds one page of up to 1.7 MB while it is parsed; the cache is the only thing that grows, and it is bounded. The compose limits (512m) are a blast-radius guard for the host, not a working budget.

`MAX_CONCURRENCY` (2) bounds searches in flight. Stage 0 sends one pair at a time; two lets a slow pair overlap the next run without doubling the request rate from the address. The global limiter, not the semaphore, is what shapes the traffic Meta sees.

## Layout

| File | Role |
|---|---|
| `src/facebook_ad_library/scraper.py` | the search URL, the challenge URL, the page markers and the results parser, and the search with its budget and recovery rules |
| `src/facebook_ad_library/session.py` | one session (the GET, the challenge, pacing, classification of the answer), the global limiter and the pool; the transport seam the tests replace |
| `src/facebook_ad_library/mapping.py` | turns a collated result into the item shape Stage 0 reads |
| `src/facebook_ad_library/cache.py` | the TTL result cache |
| `src/facebook_ad_library/api.py` | FastAPI surface: request aliases, bearer check, cache, error bodies, headers, counters |
| `src/facebook_ad_library/proxy.py` | `SCRAPER_PROXY` parsing and the rotating-gateway guard |
| `src/facebook_ad_library/config.py` | environment variables, read once |
| `src/facebook_ad_library/__init__.py` | the `facebook-ad-library` CLI (`serve`, `search`, `diag`) |
| `tests/` | 94 tests against the saved fixtures; no network. `tests/fixtures/README.md` says how each fixture was cut from a live page and how to refresh it |
| `docs/address-classification.md` | the measurements that led to this design |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `API_TOKEN` | *(empty)* | bearer token callers must send; empty disables auth, for local testing only |
| `SCRAPER_PROXY` | *(empty)* | proxy for every request, as a URL or `host:port:user:pass`; not needed; rotating gateway ports (Decodo 7000, DataImpulse 823) are refused at startup |
| `FB_IMPERSONATE` | `chrome` | `curl_cffi` TLS profile |
| `REQUEST_TIMEOUT_S` | `30` | per-request timeout |
| `SSR_RETRIES` | `2` | extra GETs when the page comes without its results blob |
| `MAX_CONCURRENCY` | `2` | searches in flight |
| `SESSION_POOL_SIZE` | `2` | warm sessions |
| `SESSION_MAX_REQUESTS` | `200` | GETs before a session is retired |
| `SESSION_MAX_AGE_S` | `7200` | age before a session is retired |
| `SPACING_MIN_S` / `SPACING_MAX_S` | `2` / `5` | random gap between GETs on one session |
| `RATE_LIMIT_PER_MIN` | `4` | GETs a minute from this process, all sessions |
| `RATE_LIMIT_SLEEP_S` | `60` | sleep before the one retry on a `429` |
| `MISS_STREAK_RETIRE` | `5` | consecutive pages without results that retire a session |
| `SCRAPE_BUDGET_S` | `240` | wall-clock ceiling per search; must stay under Stage 0's 300 s |
| `CACHE_TTL_S` / `CACHE_EMPTY_TTL_S` | `86400` / `3600` | cache life for results with ads and without |
| `CACHE_MAX_ENTRIES` | `2000` | cache size |
| `HOST` | `0.0.0.0` | bind address |
| `PORT` | `8002` | bind port; `8000` and `8001` are the siblings |
