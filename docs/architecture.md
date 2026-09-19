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
2. `_details` is always `null` and `page_alias` is always `""`. The actor filled them from a second per-ad query (`fetchDetails: true`) that costs a request per ad; the search response does not carry them, and the workflow's third-choice domain fallback never fired in production. `page_alias` empty is what the node saw from the actor too.
3. A failure is an HTTP status with one `{"error": {...}}` object, never an empty array, so the node's error check fires. `200 []` is reserved for a search that genuinely found nothing.

## How a search is made

The Ad Library is a logged-out React site. Its frontend loads ads by `POST https://www.facebook.com/api/graphql/` with the persisted query `AdLibrarySearchPaginationQuery`; this service replays that call and nothing else. Measured against the live site on 19 Sep 2026, from a residential IP:

1. **Bootstrap.** `GET https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=NZ&q=<keyword>&search_type=keyword_unordered&media_type=all` answers `403 Client challenge` with a 481-byte page whose script does `fetch('/__rd_verify_<token>?challenge=3', {method: 'POST'})` and reloads. The path is relative. One POST to it returns `200` and a `rd_challenge` cookie (`Max-Age=86400`); the re-GET returns `200`, about 570 KB to 1.1 MB of HTML, and sets `datr`.
2. **Tokens.** The page carries the session tokens the frontend sends with every GraphQL call, each read with one regex: `lsd` (from `["LSD",[],{"token":"…"}]`), `jazoest`, `server_revision` (sent as `__rev` and `__spin_r`), `hsi`, `__spin_t`, `__spin_b`, `haste_session` (sent as `__hs`) and `connectionClass` (sent as `__ccg`). There is no `fb_dtsg` when logged out and none is needed. The page carries **no** server-rendered ads and **no** `doc_id`.
3. **`doc_id`.** The persisted-query id lives in one of the nine JS bundles the page links (about 7.6 MB, fetched in under a second), as `__d("AdLibrarySearchPaginationQuery_facebookRelayOperation",[],(function(t,n,r,o,a,i){a.exports="24922295957467452"}),null)`. A session reads it from there when it is minted, so a new Meta build changes nothing here unless the module name or the export shape changes. `FB_DOC_ID` pins a value instead.
4. **Search.** `POST /api/graphql/`, form-encoded, with the tokens above, `fb_api_req_friendly_name=AdLibrarySearchPaginationQuery`, the `doc_id`, and a `variables` JSON. The variables are the frontend's own: `activeStatus`, `adType`, `audienceTimeframe`, `countries` and `country`, `cursor`, `first: 30`, `queryString`, `searchType: KEYWORD_UNORDERED`, `sessionID`, `collationToken`, and a dozen null or empty filters. `FB_VARIABLES_JSON` merges overrides on top for the day Meta adds a key. `tests/test_scraper.py` pins the form field list.
5. **Pages.** The response is `data.ad_library_main.search_results_connection` with `edges[].node.collated_results[]` and `page_info { end_cursor, has_next_page }`. Page 2 is the same body with the cursor. One call returns about 10 collated results (an ad and its variants count once) regardless of `first: 30`, on every search measured so far. Ads are de-duplicated on `ad_archive_id` across pages.

`MAX_PAGES` (3) therefore yields about 30 ads per pair. Stage 0's `maxItems: 80` would need 8 calls. Measured on ten keyword-and-country pairs on 19 Sep 2026, three pages returned 26 to 30 ads from 12 to 20 distinct advertisers each; whether later pages add advertisers Stage 0 has not seen has not been measured, and `facebook.md` §5.4 records that in the Apify era 120 ads bought about 10 unique advertisers. Raise `MAX_PAGES` and `RATE_LIMIT_PER_MIN` together, and re-do the budget arithmetic below.

## Why no browser, and why Chrome TLS

The Ad Library's only gate is the challenge above plus a TLS-fingerprint check. Measured 19 Sep 2026: plain `curl` clears the challenge and receives the cookie, and is then answered with a `400 Sorry, something went wrong` error page on every request after it, including the homepage. `curl_cffi` with `impersonate="chrome"` is answered normally. So there is no Chromium in this service: one Python process, one curl handle per session.

`FB_IMPERSONATE` is `chrome`, an alias for the newest Chrome profile the installed `curl_cffi` knows (`chrome150` in 0.16.3). A `400` after a clean challenge is the fingerprint being rejected; the service names that symptom in its `ScrapeBlocked` message.

## Sessions

The unit of identity is a session, not a request: one cookie jar (`datr`, `rd_challenge`), one `lsd`, one `sessionID`, one `__req` counter that increments in base 36 like the frontend's. `FbSession` in `session.py` holds one; `_SessionPool` keeps `SESSION_POOL_SIZE` (2) of them warm, hands them out round-robin under a `MAX_CONCURRENCY` (2) semaphore, mints lazily on the first lease, and drops a session on its way back if it is retired or expired. A session expires after `SESSION_MAX_REQUESTS` (200) calls or `SESSION_MAX_AGE_S` (7,200 s), both unverified starting points from `facebook.md`; `/health` reports `retired_by_reason` so the limit that actually bites can be seen.

Pacing has two layers: a random gap of `SPACING_MIN_S` to `SPACING_MAX_S` (2 to 5 s) between calls on one session, and a process-wide ceiling of `RATE_LIMIT_PER_MIN` (4) GraphQL calls a minute across all sessions. At three calls per pair that is Stage 0's 40 pairs an hour with headroom for retries.

## Retries and the error ladder

`search_page` classifies every GraphQL response into one of six kinds and raises a typed error for anything that is not a page of ads. `search` then applies the recovery rules:

| Kind | Raised | What `search` does |
|---|---|---|
| `5xx` or a network error | `ScrapeFailed` after in-call retries at 1 s and 3 s | retire the session |
| GraphQL error code `1675004` | `RateLimited` | sleep `RATE_LIMIT_SLEEP_S` (60) once and retry the same page on the same session; a second one retires it |
| `200` with an HTML body, any other non-`200`, or unparseable JSON | `SessionDead` | retire the session |
| `data` null or without `ad_library_main`, no `1675004` | `DocIdStale` | retire the session |

After a retirement: if any page of this search already succeeded, the ads in hand are returned as a `200` with `X-Pages-Failed: 1`; otherwise one fresh session is minted and the search restarts from page 1 (whether a cursor survives a session change is unknown, and restarting costs at most two extra calls). A second failure escapes as the API's `503`; two `SessionDead`s in a row become `ScrapeBlocked`. A session that returns `EMPTY_STREAK_RETIRE` (5) consecutive empty first pages is retired as suspect, because a soft block and a niche keyword with no ads look identical.

| Status | Type | Cause |
|---|---|---|
| `400` | `ValueError` | no `query`, a country that is not two letters or `ALL`, or an unknown `activeStatus` |
| `503` | `ScrapeBlocked` | challenge would not clear, `403` without the challenge marker, `400` after the challenge, a page with no `lsd`, or two dead sessions in a row |
| `503` | `RateLimited` | `1675004` again after the one sleep, on a fresh session too |
| `503` | `DocIdStale` | no `doc_id` in the bundles, or `data` null twice |
| `503` | `ScrapeFailed` | network failure or `5xx` that survived the retries |

## The time budget

The numbers only make sense together, and `.env.example` carries the arithmetic:

| | |
|---|---|
| one page, worst case | `SPACING_MAX_S` + 60 / `RATE_LIMIT_PER_MIN` + `REQUEST_TIMEOUT_S` = 5 + 15 + 30 = 50 s |
| one search, worst case | 3 pages × 50 s + `RATE_LIMIT_SLEEP_S` 60 s + about 5 s to mint = 215 s |
| Stage 0 node timeout | 300 s |
| measured, three pages, no contention | 16 to 59 s (the spread is the limiter: back-to-back pairs wait for the 4-a-minute window) |

`SCRAPE_BUDGET_S` (240) is what protects the workflow. Nothing cancels a request once it starts: Starlette does not cancel a handler when the client disconnects and the search runs in a thread, so a caller that gives up does not free the session. Before each page after the first the search checks that a whole page's worst case still fits before the deadline; if not it returns what it has with `X-Truncated: true` and counts it. Page 1 is never skipped. The rate-limit sleep is only taken when the sleep plus one more page still fits. `stop_grace_period` in `docker-compose.yml` is 250 s so a redeploy cannot kill a search mid-budget.

## Cache

`api.py` keeps complete results for `CACHE_TTL_S` (86,400 s), keyed on the normalised query, the upper-cased country, `maxItems` and `activeStatus`. Stage 0 re-searches an exhausted keyword's remaining country slots and retries a pair on error, so the same request arrives more than once a day; a hit costs nothing and is answered in a few milliseconds with `X-Cache: hit`. Truncated and partial results are never stored. Empty results live `CACHE_EMPTY_TTL_S` (3,600 s), because a soft block looks exactly like a keyword with no ads. The cache is bounded at `CACHE_MAX_ENTRIES` (2,000) and lives in memory, so a restart clears it.

## Memory and concurrency

One Python process with no browser. Measured serving a three-page search in the container on 19 Sep 2026: 57 MiB resident and 9 PIDs. Each in-flight search holds one response of about 50 to 80 KB at a time; the cache is the only thing that grows, and it is bounded. The compose limits (512m) are a blast-radius guard for the host, not a working budget.

`MAX_CONCURRENCY` (2) bounds searches in flight. Stage 0 sends one pair at a time; two lets a slow pair overlap the next run without doubling the request rate from the address. The global limiter, not the semaphore, is what shapes the traffic Meta sees.

## Layout

| File | Role |
|---|---|
| `src/facebook_ad_library/scraper.py` | URL and token extraction, `doc_id` discovery, the form body and variables, response classification, and the search with its budget and recovery rules |
| `src/facebook_ad_library/session.py` | one session (bootstrap, challenge, pacing, one GraphQL call), the global limiter and the pool; the transport seam the tests replace |
| `src/facebook_ad_library/mapping.py` | turns a collated result into the item shape Stage 0 reads |
| `src/facebook_ad_library/cache.py` | the TTL result cache |
| `src/facebook_ad_library/api.py` | FastAPI surface: request aliases, bearer check, cache, error bodies, headers, counters |
| `src/facebook_ad_library/proxy.py` | `SCRAPER_PROXY` parsing and the rotating-gateway guard |
| `src/facebook_ad_library/config.py` | environment variables, read once |
| `src/facebook_ad_library/__init__.py` | the `facebook-ad-library` CLI (`serve`, `search`, `diag`) |
| `tests/` | 118 tests against the saved fixtures; no network. `tests/fixtures/README.md` says how each fixture was cut from a live dump and how to refresh it |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `API_TOKEN` | *(empty)* | bearer token callers must send; empty disables auth, for local testing only |
| `SCRAPER_PROXY` | *(empty)* | proxy for every request, as a URL or `host:port:user:pass`; rotating gateway ports (Decodo 7000, DataImpulse 823) are refused at startup |
| `FB_DOC_ID` | *(empty)* | persisted-query id override; empty means read it from the page's bundles at mint |
| `FB_VARIABLES_JSON` | *(empty)* | JSON object merged over the built-in GraphQL variables |
| `FB_IMPERSONATE` | `chrome` | `curl_cffi` TLS profile |
| `REQUEST_TIMEOUT_S` | `30` | per-request timeout |
| `FB_PAGE_SIZE` | `30` | the `first` the frontend sends; Meta returns about 10 collated results regardless |
| `MAX_PAGES` | `3` | GraphQL calls per search |
| `MAX_CONCURRENCY` | `2` | searches in flight |
| `SESSION_POOL_SIZE` | `2` | warm sessions |
| `SESSION_MAX_REQUESTS` | `200` | calls before a session is retired |
| `SESSION_MAX_AGE_S` | `7200` | age before a session is retired |
| `SPACING_MIN_S` / `SPACING_MAX_S` | `2` / `5` | random gap between calls on one session |
| `RATE_LIMIT_PER_MIN` | `4` | GraphQL calls a minute from this process, all sessions |
| `RATE_LIMIT_SLEEP_S` | `60` | sleep before the one retry on `1675004` |
| `EMPTY_STREAK_RETIRE` | `5` | consecutive empty first pages that retire a session |
| `SCRAPE_BUDGET_S` | `240` | wall-clock ceiling per search; must stay under Stage 0's 300 s |
| `CACHE_TTL_S` / `CACHE_EMPTY_TTL_S` | `86400` / `3600` | cache life for results with ads and without |
| `CACHE_MAX_ENTRIES` | `2000` | cache size |
| `HOST` | `0.0.0.0` | bind address |
| `PORT` | `8002` | bind port; `8000` and `8001` are the siblings |
