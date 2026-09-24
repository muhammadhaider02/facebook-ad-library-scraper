# API

Two endpoints, each answering the body an n8n workflow sends to a vendor today, in that vendor's shape: `POST /facebook` stands in for the Apify actor Stage 0 calls once per keyword-and-country pair; `POST /adyntel` stands in for the Adyntel API workflows 01 and 02 call once per brand. Interactive docs are at `/docs`.

## The Apify contract

| | Apify | This service |
|---|---|---|
| Endpoint | `POST https://api.apify.com/v2/acts/igolaizola~facebook-ad-library-scraper/run-sync-get-dataset-items` | `POST http://facebook-ad-library:8002/facebook` |
| Auth | n8n `apifyApi` credential | n8n Header Auth credential sending `Authorization: Bearer <API_TOKEN>` |
| Query string | `?maxTotalChargeUsd=1` | none; a leftover one is ignored |
| Success | JSON array, one object per ad | same |
| Failure | non-`2xx` from Apify, or items carrying `error` | an HTTP status with one `{"error": {...}}` object |
| Node timeout | 300 s | unchanged; `SCRAPE_BUDGET_S` sits under it |

Stage 0's node has not been repointed. The contract has been exercised from n8n in `scraper-testing` with Stage 0's node cloned verbatim and Stage 0's extraction verbatim: 12 calls, 0 failures, every field read downstream present on every ad (architecture.md, [Measured against Apify](architecture.md#measured-against-apify)).

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
- **Up to 30 ads, not 80.** One search is one GET of the Ad Library's search page, which carries the first page of results, up to 30 ads. `maxItems` is honoured up to that. Stage 0's own telemetry averaged 10.6 ads a call from the actor with a best run of 29.5, so the cap is above what the pipeline saw; see [architecture.md](architecture.md#how-a-search-is-made).
- **Ads are de-duplicated** on `ad_archive_id`.
- **A missing page is an error, not an empty list.** About 1 in 4 page loads arrives without its results and is retried; if every attempt inside the budget misses, the answer is `503 ResultsMissing`, never `200 []`, because Stage 0 retires keywords on empty lists.
- **Repeats are free.** An identical request inside `CACHE_TTL_S` is answered from memory with `X-Cache: hit`, whatever its `maxItems`.

## The Adyntel contract

| | Adyntel | This service |
|---|---|---|
| Endpoint | `POST https://api.adyntel.com/facebook` | `POST http://facebook-ad-library:8002/adyntel` |
| Auth | `api_key` and `email` in the body | `Authorization: Bearer <API_TOKEN>`; `api_key` and `email` in the body are ignored |
| Found | `200` envelope with `number_of_ads` | same |
| Not found | `200 {}` | same |
| Failure | `5xx`, or an `error` field | an HTTP status with one `{"error": {...}}` object |
| Timeouts at the call sites | 60 s and 120 s on the HTTP nodes, 30 s and 45 s inside Code nodes | `BRAND_BUDGET_S` (25) sits under the smallest |

Workflows 01 and 02 have not been repointed. The seven call sites and what each reads are recorded in `adyntel.md` (22 Sep 2026) from the live node code; the contract below reproduces every read.

### The lookup call

The three bodies the workflows send, verbatim apart from the key:

```json
{ "api_key": "…", "email": "…", "company_domain": "gymshark.com" }
{ "api_key": "…", "email": "…", "company_domain": "gymshark.com", "active_status": "all" }
{ "api_key": "…", "email": "…", "company_domain": "gymshark.com", "media_type": "video" }
```

and, from the fallback paths, `{"facebook_url": "https://www.facebook.com/<handle>"}` with or without `active_status`. This service also takes `page_id`, which the vendor did not: `00 · Find Brands` stores the page id of every brand it sources, and a lookup by id is one GET with no resolution step.

### Where the service differs from Adyntel

- **Always complete.** The page view carries the page's total, so `number_of_ads` is the whole count in one answer, `is_result_complete` is always `true` and `continuation_token` is always `null`. 01's paging loop never runs; nothing was lost, it only ran when the vendor's first page was partial.
- **Up to 30 results, 10 by default.** The vendor's page was 10; `max_results` goes to 30, which is what the page carries. `unique_landing_pages` and `platform` are built over the returned results, so the ownership checks in 01 and 02 see the hosts of the ads they receive.
- **`duration_s` is on every video.** Decoded from the CDN URL's `efg` parameter, which `Collect Video Ads` did by hand; the URL is unchanged, so that code keeps working too.
- **`platform` is always an array**, lower-case. The vendor sent an array on every recent run and a string on old ones; the nodes accept both.
- **Results are plain objects**, never one-element arrays. The nodes unwrap either.
- **`media_type: "video"` forces `active_status: "active"`**, as the vendor did: `video_ad_count` in 02 stays a live count.
- **Two pages for one brand.** A brand with a second page for another market (Shakti Mat has a DE page) resolves to the page with most ads landing on the domain; `X-Resolved-Page-Id` says which. Sending `page_id` removes the question.
- **Repeats are cheap.** The page view is kept for `ADYNTEL_CACHE_TTL_S` (600 s) under (page id, status, media), and a domain's or URL's resolved page id for `CACHE_TTL_S`, so 02's three calls per brand cost one resolution and three page views.

### Lookup request fields

`POST /adyntel` takes a JSON body. Unknown fields (`api_key`, `email`, `webhook_url`, `all_ads`, `country_code`) are ignored. Exactly one of the first three is used, in this order of preference.

| Field | Aliases | Default | Notes |
|---|---|---|---|
| `page_id` | `pageId` | | the advertiser's numeric page id; digits, as a string or a number |
| `facebook_url` | `facebookUrl`, `page_url` | | a page URL: a vanity handle (`facebook.com/shaktimats`), or one carrying the id (`/p/<Name>-<id>/`, `/people/<Name>/<id>/`, `/pages/<Name>/<id>/`, `profile.php?id=`, a bare numeric path) |
| `company_domain` | `companyDomain`, `domain` | | the brand's website; subdomains, `www.`, a scheme or a path are stripped to the registrable domain |
| `active_status` | `activeStatus` | `active` | `active`, `inactive` or `all` |
| `media_type` | `mediaType` | `all` | `all` or `video`; `video` implies `active` |
| `continuation_token` | `continuationToken` | | accepted and ignored: every answer is complete |
| `max_results` | `maxResults`, `max` | `10` | 1 to 30 |

### Lookup response

Found:

```json
{
  "number_of_ads": 1013, "is_result_complete": true, "continuation_token": null,
  "page_id": "775991435791863", "page_name": "Shakti Mat", "active_status": "active", "media_types": ["all"],
  "platform": ["audience_network", "facebook", "instagram", "messenger"],
  "unique_landing_pages": ["https://shaktimat.com/"], "count_landing_pages": 1,
  "results": [ { "...": "one object per ad, see below" } ],
  "source": "facebook-ad-library"
}
```

Not found: `{}`. A page that exists and runs no ads is found, with `number_of_ads: 0` and `results: []`.

One result, every key present on every item, `null` where Meta has no value:

| Field | Value | Read downstream by |
|---|---|---|
| `is_active` | boolean | 02's long runners and retired lists |
| `start_date`, `end_date` | unix seconds, integers | `days_running` |
| `page_name` | advertiser page name | 01's page-name match |
| `publisher_platform` | e.g. `["FACEBOOK", "INSTAGRAM"]` | the `platform` union |
| `snapshot.body` | `{"text": "…"}` | primary text; DCO placeholders like `{{product.brand}}` are passed through as the vendor did |
| `snapshot.title`, `snapshot.link_description`, `snapshot.cta_text`, `snapshot.cta_type`, `snapshot.display_format` | | 02's ad analysis |
| `snapshot.link_url` | landing URL | ownership checks, `unique_landing_pages` |
| `snapshot.videos[]` | `video_sd_url`, `video_hd_url`, `video_preview_image_url`, `duration_s` | 02's clip selection and Deepgram |
| `snapshot.cards[]` | `body`, `title`, `caption`, `link_description`, `link_url`, `cta_text`, `cta_type`, `video_sd_url`, `video_hd_url`, `duration_s`, image URLs | catalogue ads, where the card copy is the only human-written text |
| `snapshot.caption`, `snapshot.page_id`, `snapshot.page_name`, `snapshot.page_profile_uri`, `snapshot.page_profile_picture_url`, `snapshot.page_like_count`, `snapshot.page_categories`, `snapshot.images[]` | | not read; kept because they cost nothing |
| `ad_archive_id`, `page_id`, `collation_count`, `categories`, `url` | | provenance |

Video URLs are Meta CDN links signed for about four days; 02 fetches them in the same run.

### Lookup response headers

| Header | Meaning |
|---|---|
| `X-Resolver` | `page_id`, `facebook_url` or `company_domain`: which field decided the lookup |
| `X-Resolved-Page-Id` | the page id the lookup used, empty when not found |
| `X-Found` | `1` or `0` |
| `X-Scrape-Seconds`, `X-Queue-Seconds` | wall clock for the lookup, and the part of it spent waiting for a concurrency slot |
| `X-Attempts`, `X-Misses` | Ad Library page GETs, and those that came without the results blob and were retried |
| `X-Plain-Gets` | page plugin and profile GETs made to resolve a vanity URL |
| `X-Session-Swaps` | `1` when the first session was refused and a fresh one finished the lookup |
| `X-Cache` | `hit` or `miss` |

## Request fields

`POST /facebook` takes a JSON body. Unknown fields are ignored.

| Field | Aliases | Default | Notes |
|---|---|---|---|
| `query` | `q`, `keyword` | | required; the keyword, sent as the site's own `keyword_unordered` search |
| `country` | `countries` | `US` | two-letter code or `ALL`; a list is accepted and its first entry used; case-insensitive |
| `maxItems` | `max_items`, `max` | `80` | how many items come back, clamped to 1–5000. **Not a paging stop**: it sizes the response and nothing else |
| `activeStatus` | `active_status` | `active` | `active`, `inactive` or `all` |
| `max_pages` | `maxPages` | `1` | `1` keeps the rendered-page path, unproxied, and is what Stage 0 sends. Above `1` pages the search over GraphQL, which **requires** `FALLBACK_PROXY`; capped at `PAGE_MAX_PAGES` (150) |
| `max_ads` | `maxAds` | `0` | how many ads are worth paging for. `0` leaves the stop to the page, novelty and empty limits. This is the target `maxItems` must not be confused with |
| `novelty_stop` | `noveltyStop` | `25` | pages with no new advertiser that end the search |
| `empty_tol` | `emptyTol` | `8` | blank pages in a row that end it. Meta serves blanks mid-run; stopping at the first cost 135 ads and 25 advertisers on one keyword |
| `budget_s` | `budgetS` | `PAGE_BUDGET_S` | seconds this one call may take, so it answers inside the caller's node timeout. `0` removes the ceiling, for a probe nothing is waiting on |
| `cursor` | `next_cursor`, `nextCursor` | | resume state from a previous call's `X-Next-Cursor` |
| `collation` | `collation_token`, `collationToken` | | from `X-Collation`. Send it **with** the cursor: Meta collates duplicates against it, and a fresh token re-collates the search mid-run |

A search that is refused by the throttle recovers on its own; none of the paging fields are needed for that. They exist for a caller that wants depth deliberately. See [architecture.md](architecture.md#the-throttle).

## Response items

One object per ad, in the order the Ad Library shows them. Every key is present on every item; `null` where Meta has no value.

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
| `X-Attempts` | page GETs that answered with the Ad Library page; a challenge re-GET is not a second attempt |
| `X-Misses` | of those, pages that came without their results and were retried |
| `X-Short-Counts` | of those, pages whose total was below the ads on them and were refetched; Meta now and then serves the count unfilled (`count: 0` above 30 ads, seen 23 Sep 2026). When every attempt is short, `number_of_ads` is the number of ads on the page, a floor, never 0 |
| `X-Session-Swaps` | `1` when the first session was refused and a fresh one finished the search |
| `X-Direct-Skipped` | `1` when the direct GET was skipped because Meta was recently withholding, so the answer came from the proxied path. The page figures above then read `0`: there was no rendered page |
| `X-Cache` | `hit` or `miss` |

On a paged answer (`max_pages > 1`) these are sent as well:

| Header | Meaning |
|---|---|
| `X-Paged` | `1` |
| `X-Pages`, `X-Ads`, `X-Advertisers`, `X-Empty-Pages` | what the run covered and found |
| `X-Stopped-Because` | which limit ended it, in the run's own words: `Meta dropped the cursor (true end)`, `hit the N-page cap`, `ran out of the Ns budget after N page(s)`, `N empty pages in a row`, `N pages with no new advertiser`, `reached the N-ad target` |
| `X-Truncated` | `1` when Meta still has more, `0` when it dropped the cursor and there is nothing left to ask for |
| `X-Next-Cursor`, `X-Collation` | resume state; send **both** back to continue the same search |
| `X-Decoded-Bytes` | what the run decoded. The proxy bills the wire, which is roughly 20% of this - one measurement, not a rate card |
| `X-Session-Minted` | `1` when this call paid for a fresh GraphQL session (~0.9 MB pinned, ~21 MB unpinned) |

## Errors

```json
{ "error": { "type": "ResultsMissing", "status": 503, "message": "the page came without results 3 time(s) in a row for 'running shoes' US", "description": "..." } }
```

| Status | Type | When |
|---|---|---|
| `400` | `ValueError` | no `query`, a country that is not two letters or `ALL`, an unknown `activeStatus` or `media_type`; on `/adyntel`, none of `page_id`, `facebook_url`, `company_domain`, or a non-numeric page id |
| `401` | `HTTPException` | missing or wrong bearer token |
| `503` | `ScrapeBlocked` | the challenge would not clear, a `403` without it, a `400` error page after it (TLS fingerprint rejected), two dead sessions in a row, or a login wall on the plugin or profile page twice |
| `503` | `ScrapeBlocked` (withheld) | **Meta reported ads and served none, and the recovery could not get them either.** Deliberately not an empty `200`: the caller verifies a brand from where its ads land, so an empty list makes it reject the brand and spend one of its three retries on a fault that was never the brand's. The message names the count and says whether the fallback proxy was tried |
| `503` | `RateLimited` | HTTP `429` again after the one sleep, on a fresh session too (a lookup does not sleep: it swaps) |
| `503` | `ResultsMissing` | every attempt inside the budget came back without the results blob |
| `503` | `BudgetExceeded` | `/adyntel` only: the next GET could not finish inside `BRAND_BUDGET_S`, priced with the limiter's next free slot |
| `503` | `Busy` | `/adyntel` only: every concurrency slot stayed taken for the whole budget |
| `503` | `ScrapeFailed` | network failure or `5xx` that survived the retries |
| `500` | | anything unexpected |

A `503` is a vendor failure: nothing about the request was wrong, and the same request may succeed a minute later. A `400` is the caller's. `200 []` means the Ad Library shows no ads for that keyword in that country, which Stage 0 already handles as its own outcome; `200 {}` on `/adyntel` means there is no page for the input, which 01 and 02 handle by taking their next fallback.

## `GET /health`

Unauthenticated, for uptime checks and the Docker `HEALTHCHECK`.

```json
{
  "status": "ok", "version": "0.3.1", "auth": true, "proxy": false, "max_concurrency": 3,
  "requests": 3, "ok": 2, "empty": 0, "retried": 1, "cache_hits": 1,
  "bad_request": 1, "blocked": 0, "rate_limited": 0, "results_missing": 0, "failed": 0, "in_flight": 0,
  "adyntel_requests": 4, "adyntel_found": 3, "adyntel_not_found": 1, "adyntel_cache_hits": 1, "adyntel_resolve_hits": 1, "adyntel_short_counts": 0,
  "adyntel_by_page_id": 1, "adyntel_by_url": 1, "adyntel_by_domain": 2, "busy": 0, "budget_exceeded": 0,
  "sessions": { "live": 1, "warm": 1, "sessions_minted": 1, "sessions_retired": 0, "retired_by_reason": {},
                "challenges": 1, "calls": 6, "bytes": 4711140, "misses": 1, "rate_limited": 0,
                "session_dead": 0, "blocked": 0, "transient": 0,
                "plain_calls": 1, "plain_blocked": 0, "plain_dead": 0, "busy": 0 },
  "cache": { "entries": 1, "hits": 1, "misses": 1, "evictions": 0 },
  "brand_cache": { "entries": 3, "hits": 2, "misses": 4, "evictions": 0 }
}
```

Counters reset on restart. The top-level counters are per request; `sessions` is per GET and per session event, with the page plugin and profile GETs a lookup makes counted apart as `plain_*`. `retried` counts searches and lookups that needed at least one extra GET; `sessions.misses` counts the GETs themselves. `results_missing` rising is the one to watch: it means the retries were not enough and a workflow saw errors instead of ads. `adyntel_not_found` rising across many brands means the resolver, not Meta, is the thing to look at. `retired_by_reason` says which limit is retiring sessions.

## Command line

Same code, no server:

```bash
uv run facebook-ad-library search "acupressure mat for back pain" --country NZ --max 80 --pretty
uv run facebook-ad-library search "running shoes" "pickleball paddle" --country AU --summary   # counts only
uv run facebook-ad-library brand --page-id 775991435791863 --summary                              # one lookup, the vendor envelope
uv run facebook-ad-library brand --url https://www.facebook.com/shaktimats --status all --pretty
uv run facebook-ad-library brand --domain gymshark.com --media video --summary
uv run facebook-ad-library diag --query "running shoes" --repeat 3 --save-dir diag-out           # one session, step by step
uv run facebook-ad-library diag --page-id 775991435791863 --status all                            # the page view, with its count
uv run facebook-ad-library diag --slug shaktimats --save-dir diag-out                             # the plugin and the profile page
```

`brand` makes one lookup the way `POST /adyntel` does and prints the envelope; `--summary` prints one line per ad with the decoded clip durations instead. Exit codes: 2 no input, 3 a vendor failure. `diag` prints each GET as it happens (the challenge seen and cleared, the cookies, the shape of the page, its ad count and, for a page view, the total and whether the page is known) and can repeat the same page on one session to show the miss rate; `--slug` fetches the two pages a vanity URL is resolved through and reads the page id from each, which is the deploy check for those page classes from a new address. Exit codes: 2 the session was refused, 3 the GET failed, 5 every page came without results.
