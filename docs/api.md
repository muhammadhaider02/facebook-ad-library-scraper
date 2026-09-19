# API

One endpoint that answers the body Stage 0 sends to Apify once per keyword-and-country pair, in the actor's item shape. Interactive docs are at `/docs`.

## The Apify contract

| | Apify | This service |
|---|---|---|
| Endpoint | `POST https://api.apify.com/v2/acts/igolaizola~facebook-ad-library-scraper/run-sync-get-dataset-items` | `POST http://facebook-ad-library:8002/facebook` |
| Auth | n8n `apifyApi` credential | n8n Header Auth credential sending `Authorization: Bearer <API_TOKEN>` |
| Query string | `?maxTotalChargeUsd=1` | none; a leftover one is ignored |
| Success | JSON array, one object per ad | same |
| Failure | non-`2xx` from Apify, or items carrying `error` | an HTTP status with one `{"error": {...}}` object |
| Node timeout | 300 s | unchanged; `SCRAPE_BUDGET_S` sits under it |

Stage 0's node has not been repointed and this contract has not yet been exercised from n8n; it has been exercised with the verbatim body over HTTP, locally and in CI.

### The search call

Sent verbatim by `Apify: Facebook Ad Library`; only `query` and `country` vary.

```json
{
  "maxItems": 80,
  "query": "acupressure mat for back pain",
  "country": "NZ",
  "category": "all",
  "mediaType": "all",
  "activeStatus": "active",
  "advertisers": [],
  "fetchDetails": true
}
```

Honoured: `query`, `country`, `maxItems`, `activeStatus`. Accepted and ignored: `category` and `mediaType` (the service always searches all), `advertisers` (always empty in the workflow), `fetchDetails` (see below).

### Where the service differs from the actor

- **No per-ad detail call.** The actor's `fetchDetails` ran a second query per ad to fill `_details.advertiser.page`. Nothing Stage 0 reads from there is needed, so `_details` is always `null` and `page_alias` is always `""`.
- **About 30 ads, not 80, by default.** One GraphQL call returns about 10 collated results and `MAX_PAGES` is 3. `maxItems` is honoured up to that cap; see [architecture.md](architecture.md#how-a-search-is-made) for the trade-off.
- **Ads are de-duplicated** on `ad_archive_id` across pages before they are returned.
- **A partial result is a `200`.** If the time budget stops the search before `maxItems`, the ads already collected are returned with `X-Truncated: true`; if a session fails after a good page, they are returned with `X-Pages-Failed: 1`. The body looks identical to a complete run.
- **Repeats are free.** An identical request inside `CACHE_TTL_S` is answered from memory with `X-Cache: hit`.

## Request fields

`POST /facebook` takes a JSON body. Unknown fields are ignored.

| Field | Aliases | Default | Notes |
|---|---|---|---|
| `query` | `q`, `keyword` | | required; the keyword, sent as the site's own `keyword_unordered` search |
| `country` | `countries` | `US` | two-letter code or `ALL`; a list is accepted and its first entry used; case-insensitive |
| `maxItems` | `max_items`, `max` | `80` | clamped to 1–300, then capped by `MAX_PAGES` × ~10 |
| `activeStatus` | `active_status` | `active` | `active`, `inactive` or `all` |

## Response items

One object per ad, in the order the Ad Library returns them. Every key is present on every item; `null` where Meta has no value.

| Field | Value | Read downstream by |
|---|---|---|
| `page_name` | advertiser page name | brand name |
| `page_id` | advertiser page id, as a string | dedup and identity; also under `snapshot` |
| `page_profile_uri`, `page_url` | `https://www.facebook.com/<id or vanity>/`, the same value in both | page link |
| `page_alias` | always `""` | the node's numbered-name farm test only runs when this is empty, which is what it saw from the actor too |
| `page_category` | `page_categories[0]` | the ad-farm filter |
| `page_categories` | Facebook's own labels, e.g. `["Health/beauty"]` | same |
| `page_like_count`, `page_likes` | the same value in both | page size |
| `snapshot.caption` | the bare domain, e.g. `shaktimat.com` | the brand's domain, first choice |
| `snapshot.link_url` | the ad's landing URL; the first card's when the ad itself has none | the brand's domain, second choice |
| `snapshot.page_like_count`, `snapshot.page_categories`, `snapshot.page_profile_uri`, `snapshot.page_alias`, `snapshot.page_name`, `snapshot.page_id` | the top-level values repeated | the node's fallbacks |
| `_details` | always `null` | the node's third-choice domain fallback, never used in production |
| `ad_archive_id`, `page_is_deleted`, `start_date`, `end_date`, `is_active`, `publisher_platform` | | not read; kept because they cost nothing |
| `snapshot.title`, `snapshot.body`, `snapshot.cta_type`, `snapshot.page_profile_picture_url`, `snapshot.cards[]` | | not read |
| `query`, `country` | the request that produced the item | provenance |

## Response headers

| Header | Meaning |
|---|---|
| `X-Scrape-Seconds` | wall clock for the search |
| `X-Pages-Fetched` | GraphQL calls that returned ads; retries not counted |
| `X-Pages-Failed` | `1` when a session failed after at least one good page and the ads in hand were returned |
| `X-Truncated` | `true` when `SCRAPE_BUDGET_S` stopped the search with pages left |
| `X-Session-Swaps` | `1` when the first session failed before producing anything and a fresh one restarted the search |
| `X-Cache` | `hit` or `miss` |

## Errors

```json
{ "error": { "type": "RateLimited", "status": 503, "message": "rate limited after 2 call(s) on fb-d3faef: 1675004: Rate limit exceeded", "description": "..." } }
```

| Status | Type | When |
|---|---|---|
| `400` | `ValueError` | no `query`, a country that is not two letters or `ALL`, or an unknown `activeStatus` |
| `401` | `HTTPException` | missing or wrong bearer token |
| `503` | `ScrapeBlocked` | the challenge would not clear, a `403` without it, a `400` error page after it (TLS fingerprint rejected), a page with no `lsd`, or two dead sessions in a row |
| `503` | `RateLimited` | GraphQL error `1675004` again after the one sleep, on a fresh session too |
| `503` | `DocIdStale` | no `doc_id` found in the page's bundles, or `data` null on two sessions |
| `503` | `ScrapeFailed` | network failure or `5xx` that survived the retries |
| `500` | | anything unexpected |

A `503` is a vendor failure: nothing about the request was wrong, and the same request may succeed an hour later. A `400` is the caller's. `200 []` means the Ad Library returned no ads for that keyword in that country, which Stage 0 already handles as its own outcome.

## `GET /health`

Unauthenticated, for uptime checks and the Docker `HEALTHCHECK`.

```json
{
  "status": "ok", "version": "0.1.0", "auth": true, "proxy": false, "max_concurrency": 2,
  "doc_id": "24922295957467452", "doc_id_source": "discovered",
  "requests": 3, "ok": 2, "empty": 0, "partial": 0, "truncated": 0, "cache_hits": 1,
  "bad_request": 1, "blocked": 0, "rate_limited": 0, "docid_stale": 0, "failed": 0, "in_flight": 0,
  "sessions": { "live": 1, "warm": 1, "doc_id": "24922295957467452", "doc_id_source": "discovered",
                "sessions_minted": 1, "sessions_retired": 0, "retired_by_reason": {}, "challenges": 1,
                "calls": 3, "bytes": 204304, "rate_limited": 0, "session_dead": 0, "docid_stale": 0,
                "blocked": 0, "transient": 0 },
  "cache": { "entries": 1, "hits": 1, "misses": 1, "evictions": 0 }
}
```

Counters reset on restart. The top-level counters are per request; `sessions` is per GraphQL call and per session event. `doc_id_source` is `env`, `discovered`, or `none` before the first session is minted. `truncated` or `partial` rising means the budget or the sessions are biting, which a `200` would otherwise hide; `retired_by_reason` says which limit is retiring sessions.

## Command line

Same code, no server:

```bash
uv run facebook-ad-library search "acupressure mat for back pain" --country NZ --max 80 --pretty
uv run facebook-ad-library search "running shoes" "pickleball paddle" --country AU --summary   # counts only
uv run facebook-ad-library diag --search --pages 2 --save-dir diag-out                          # step-by-step bootstrap
```

`diag` prints the bootstrap as it happens (challenge seen and cleared, cookies, each token found or missing, the discovered `doc_id`) and then the search pages; `--print-form` dumps the exact form body and variables for diffing against a DevTools capture. Exit codes: 2 the bootstrap was refused, 3 the search failed, 4 no `doc_id` could be found.
