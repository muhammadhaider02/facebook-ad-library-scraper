# API

Two endpoints answer the body an n8n workflow sends to a vendor, in that vendor's shape: `POST /facebook` stands in for the Apify actor a sourcing workflow calls once per keyword-and-country pair; `POST /adyntel` stands in for the Adyntel API a qualification or research workflow calls once per brand. `POST /jobs` batches either, and `POST /fetch` reads homepages. Interactive docs are at `/docs`.

## The Apify contract

| | Apify | This service |
|---|---|---|
| Endpoint | `POST https://api.apify.com/v2/acts/igolaizola~facebook-ad-library-scraper/run-sync-get-dataset-items` | `POST http://facebook-ad-library-sourcing:8003/facebook` |
| Auth | n8n `apifyApi` credential | n8n Header Auth credential sending `Authorization: Bearer <API_TOKEN>` |
| Query string | `?maxTotalChargeUsd=1` | none; a leftover one is ignored |
| Success | JSON array, one object per ad | same |
| Failure | non-`2xx` from Apify, or items carrying `error` | an HTTP status with one `{"error": {...}}` object |
| Node timeout | 300 s | unchanged; `SCRAPE_BUDGET_S` sits under it |

The field-by-field comparison against the actor is in [architecture.md](architecture.md#measured-against-apify).

### The search call

The actor's body, sent unchanged; only `query` and `country` vary.

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

Honoured: `query`, `country`, `maxItems`, `activeStatus`. Accepted and ignored: `category` and `mediaType` (the service always searches all), `advertisers`, `fetchDetails` (see below).

### Where the service differs from the actor

- **No per-ad detail call.** The actor's `fetchDetails` ran a second query per ad to fill `_details.advertiser.page`. Nothing a sourcing caller needs lives there, so `_details` is always `null` and `page_alias` is always `""`.
- **Up to 30 ads by default, not 80.** One search is one GET of the Ad Library's search page, which carries the first page of results, up to 30 ads. `maxItems` is honoured up to that; `max_pages` pages deeper over GraphQL (see [Request fields](#request-fields) and [architecture.md](architecture.md#how-a-search-is-made)).
- **Ads are de-duplicated** on `ad_archive_id`.
- **A missing page is an error, not an empty list.** About 1 in 4 page loads arrives without its results and is retried; if every attempt inside the budget misses, the answer is `503 ResultsMissing`, never `200 []`, because a sourcing caller retires keywords on empty lists.
- **Repeats are free.** An identical request inside `CACHE_TTL_S` is answered from memory with `X-Cache: hit`, whatever its `maxItems`.

## The Adyntel contract

| | Adyntel | This service |
|---|---|---|
| Endpoint | `POST https://api.adyntel.com/facebook` | `POST http://facebook-ad-library-sourcing:8003/adyntel` |
| Auth | `api_key` and `email` in the body | `Authorization: Bearer <API_TOKEN>`; `api_key` and `email` in the body are ignored |
| Found | `200` envelope with `number_of_ads` | same |
| Not found | `200 {}` | same |
| Failure | `5xx`, or an `error` field | an HTTP status with one `{"error": {...}}` object |
| Timeout | the caller's | `BRAND_BUDGET_S` (25 s) sits under a 30 s caller timeout |

### The lookup call

The bodies a caller sends to Adyntel, unchanged:

```json
{ "api_key": "…", "email": "…", "company_domain": "gymshark.com" }
{ "api_key": "…", "email": "…", "company_domain": "gymshark.com", "active_status": "all" }
{ "api_key": "…", "email": "…", "company_domain": "gymshark.com", "media_type": "video" }
```

and `{"facebook_url": "https://www.facebook.com/<handle>"}` with or without `active_status`. This service also takes `page_id`, which the vendor did not: a caller that stored a brand's page id when it sourced it gets a lookup that is one GET with no resolution step.

### Where the service differs from Adyntel

- **Always complete.** The page view carries the page's total, so `number_of_ads` is the whole count in one answer, `is_result_complete` is always `true` and `continuation_token` is always `null`. A caller's continuation loop never runs.
- **Up to 30 results, 10 by default.** The vendor's page was 10; `max_results` goes to 30, which is what the page carries. `unique_landing_pages` and `platform` are built over the returned results, so an ownership check sees the hosts of the ads it receives.
- **`duration_s` is on every video**, decoded from the CDN URL's `efg` parameter. The URL is unchanged, so a caller that decoded it by hand keeps working.
- **`platform` is always an array**, lower-case. The vendor sent an array on recent answers and a string on old ones.
- **Results are plain objects**, never one-element arrays.
- **`media_type: "video"` forces `active_status: "active"`**, as the vendor did, so a video count stays a live count.
- **Two pages for one brand.** A brand with a second page for another market (Shakti Mat has a DE page) resolves to the page with most ads landing on the domain; `X-Resolved-Page-Id` says which. Sending `page_id` removes the question.
- **Repeats are cheap.** The page view is kept for `ADYNTEL_CACHE_TTL_S` (600 s) under (page id, status, media), and a domain's or URL's resolved page id for `CACHE_TTL_S`, so three calls for one brand cost one resolution and three page views.

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

| Field | Value | Typical use |
|---|---|---|
| `is_active` | boolean | long-running and retired ads |
| `start_date`, `end_date` | unix seconds, integers | days running |
| `page_name` | advertiser page name | page-name match |
| `publisher_platform` | e.g. `["FACEBOOK", "INSTAGRAM"]` | the `platform` union |
| `snapshot.body` | `{"text": "…"}` | primary text; DCO placeholders like `{{product.brand}}` are passed through as the vendor did |
| `snapshot.title`, `snapshot.link_description`, `snapshot.cta_text`, `snapshot.cta_type`, `snapshot.display_format` | | ad analysis |
| `snapshot.link_url` | landing URL | ownership checks, `unique_landing_pages` |
| `snapshot.videos[]` | `video_sd_url`, `video_hd_url`, `video_preview_image_url`, `duration_s` | clip selection and transcription |
| `snapshot.cards[]` | `body`, `title`, `caption`, `link_description`, `link_url`, `cta_text`, `cta_type`, `video_sd_url`, `video_hd_url`, `duration_s`, image URLs | catalogue ads, where the card copy is the only human-written text |
| `snapshot.caption`, `snapshot.page_id`, `snapshot.page_name`, `snapshot.page_profile_uri`, `snapshot.page_profile_picture_url`, `snapshot.page_like_count`, `snapshot.page_categories`, `snapshot.images[]` | | kept because they cost nothing |
| `ad_archive_id`, `page_id`, `collation_count`, `categories`, `url` | | provenance |

Video URLs are Meta CDN links signed for about four days; fetch them in the same run.

### Lookup response headers

| Header | Meaning |
|---|---|
| `X-Resolver` | `page_id`, `facebook_url` or `company_domain`: which field decided the lookup |
| `X-Resolved-Page-Id` | the page id the lookup used, empty when not found |
| `X-Found` | `1` or `0` |
| `X-Scrape-Seconds`, `X-Queue-Seconds` | wall clock for the lookup, and the part of it spent waiting for a lane |
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
| `max_pages` | `maxPages` | `1` | `1` reads the rendered page (up to 30 ads), which is what the Apify-shaped body gets. Above `1` pages the search over GraphQL on the lane's proxy, with no rendered page; capped at 150 pages |
| `max_ads` | `maxAds` | `0` | how many ads are worth paging for. `0` leaves the stop to the page, novelty and empty limits. This is the target `maxItems` must not be confused with |
| `novelty_stop` | `noveltyStop` | `25` | pages with no new advertiser that end the search |
| `empty_tol` | `emptyTol` | `8` | blank pages in a row that end it. Meta serves blanks mid-run; stopping at the first cost 135 ads and 25 advertisers on one keyword |
| `budget_s` | `budgetS` | `PAGE_BUDGET_S` | seconds this one call may take, so it answers inside the caller's node timeout. `0` removes the ceiling, for a probe nothing is waiting on |
| `cursor` | `next_cursor`, `nextCursor` | | resume state from a previous call's `X-Next-Cursor` |
| `collation` | `collation_token`, `collationToken` | | from `X-Collation`. Send it **with** the cursor: Meta collates duplicates against it, and a fresh token re-collates the search mid-run |

A search whose ads Meta withholds recovers on its own over GraphQL on the same lane; none of the paging fields are needed for that. They exist for a caller that wants depth deliberately. See [architecture.md](architecture.md#the-throttle).

## Response items

One object per ad, in the order the Ad Library shows them. Every key is present on every item; `null` where Meta has no value.

| Field | Value | Typical use |
|---|---|---|
| `page_name` | advertiser page name | brand name |
| `page_id` | advertiser page id, as a string | dedup and identity; also under `snapshot` |
| `page_profile_uri`, `page_url` | `https://www.facebook.com/<id or vanity>/`, the same value in both | page link |
| `page_alias` | always `""` | as the actor returned it without `fetchDetails` data |
| `page_category` | `page_categories[0]` | category filters |
| `page_categories` | Facebook's own labels, e.g. `["Health/beauty"]` | same |
| `page_like_count`, `page_likes` | the same value in both | page size |
| `snapshot.caption` | the bare domain, e.g. `shaktimat.com` | the brand's domain, first choice |
| `snapshot.link_url` | the ad's landing URL; the first card's when the ad itself has none | the brand's domain, second choice |
| `snapshot.page_like_count`, `snapshot.page_categories`, `snapshot.page_profile_uri`, `snapshot.page_alias`, `snapshot.page_name`, `snapshot.page_id` | the top-level values repeated | the caller's fallbacks |
| `_details` | always `null` | |
| `ad_archive_id`, `page_is_deleted`, `start_date`, `end_date`, `is_active`, `publisher_platform` | | kept because they cost nothing |
| `snapshot.title`, `snapshot.body`, `snapshot.cta_type`, `snapshot.page_profile_picture_url`, `snapshot.cards[]` | | ad copy |
| `query`, `country` | the request that produced the item | provenance |

## Response headers

| Header | Meaning |
|---|---|
| `X-Scrape-Seconds` | wall clock for the search |
| `X-Attempts` | page GETs that answered with the Ad Library page; a challenge re-GET is not a second attempt |
| `X-Misses` | of those, pages that came without their results and were retried |
| `X-Short-Counts` | of those, pages whose total was below the ads on them and were refetched; Meta now and then serves the count unfilled (`count: 0` above 30 ads). When every attempt is short, `number_of_ads` is the number of ads on the page, a floor, never 0 |
| `X-Session-Swaps` | `1` when the first session was refused and a fresh one finished the search |
| `X-Direct-Skipped` | `1` when the lane skipped its rendered GET because Meta was recently withholding from that exit, so the answer came over GraphQL. The page figures above then read `0`: there was no rendered page |
| `X-Cache` | `hit` or `miss` |
| `X-Status` | `ok`, `no_ads`, `blocked` or `error`. `no_ads` is only ever said when the rendered page carried its results with a total of 0; a total above zero with no ads is `blocked`, never `no_ads` |
| `X-Ads-Found`, `X-Reported-Total` | ads in the answer, and Meta's total for the search when a rendered page gave one (empty on a GraphQL-only answer) |
| `X-Lane`, `X-Exit-IP`, `X-Tries` | which lane answered, the exit address it used, and how many lanes the search tried (a lane that refused it is never asked twice; see [architecture.md, Lanes](architecture.md#lanes)) |
| `X-Decoded-Bytes` | what the tries decoded. The proxy bills the wire, roughly a fifth of this for a rendered page |

`/adyntel` answers carry the same `X-Status` (`ok` or `not_found` on a `200`), `X-Lane`, `X-Exit-IP`, `X-Tries` and `X-Decoded-Bytes` beside its own headers above.

On a paged answer (`max_pages > 1`) these are sent as well:

| Header | Meaning |
|---|---|
| `X-Paged` | `1` |
| `X-Pages`, `X-Ads`, `X-Advertisers`, `X-Empty-Pages` | what the run covered and found |
| `X-Stopped-Because` | which limit ended it, in the run's own words: `Meta dropped the cursor (true end)`, `hit the N-page cap`, `ran out of the Ns budget after N page(s)`, `N empty pages in a row`, `N pages with no new advertiser`, `reached the N-ad target` |
| `X-Truncated` | `1` when Meta still has more, `0` when it dropped the cursor and there is nothing left to ask for |
| `X-Next-Cursor`, `X-Collation` | resume state; send **both** back to continue the same search |
| `X-Decoded-Bytes` | what the run decoded. The proxy bills the wire, which is roughly 20% of this: one measurement, not a rate card |
| `X-Session-Minted` | `1` when this call paid for a fresh GraphQL session (~0.9 MB with `FB_DOC_ID` pinned, ~21 MB unpinned) |

## Errors

```json
{ "error": { "type": "ResultsMissing", "status": 503, "message": "the page came without results 3 time(s) in a row for 'running shoes' US", "description": "...", "kind": "error" } }
```

`kind` is `blocked` or `error`: `blocked` means every lane that could take the request was refused by Meta (a 403, a 429, or a page whose ads were withheld), `error` means the request could not be completed for another reason. The `type` is the last lane's own error.

| Status | Type | When |
|---|---|---|
| `400` | `ValueError` | no `query`, a country that is not two letters or `ALL`, an unknown `activeStatus` or `media_type`; on `/adyntel`, none of `page_id`, `facebook_url`, `company_domain`, or a non-numeric page id |
| `401` | `HTTPException` | missing or wrong bearer token |
| `503` | `ScrapeBlocked` | the challenge would not clear, a `403` without it, a `400` error page after it (TLS fingerprint rejected), two dead sessions in a row, or a login wall on the plugin or profile page twice, on every lane that could take it (`LANE_MAX_TRIES`, different lanes) |
| `503` | `ScrapeBlocked` (withheld) | **Meta reported ads and served none on this exit, and GraphQL on the same lane could not get them either**, on every lane tried. Deliberately not an empty `200`: a caller that verifies a brand from where its ads land would reject the brand on an empty list, for a fault that was never the brand's. The message names the count |
| `503` | `RateLimited` | HTTP `429` or GraphQL `1675004` on every lane tried; a lane that answers one cools down and the request moves to another |
| `503` | `ResultsMissing` | every attempt inside the budget came back without the results blob |
| `503` | `BudgetExceeded` | the next GET could not finish inside the budget, priced with the lane limiter's next free slot |
| `503` | `Busy` | no lane could take the request inside its budget (every lane busy, cooling or blocked), or the job queue is full |
| `503` | `ScrapeFailed` | network failure or `5xx` that survived the retries |
| `500` | | anything unexpected; `type` names the exception |

A `503` is a vendor failure: nothing about the request was wrong, and the same request may succeed a minute later. A `400` is the caller's. `200 []` means the Ad Library shows no ads for that keyword in that country; `200 {}` on `/adyntel` means there is no page for the input, and the caller takes its next fallback.

## Batch jobs: `POST /jobs`, `GET /jobs/{id}`, `DELETE /jobs/{id}`

One submission carries a whole run, the lanes spread it out, and the caller polls for the results paired back by id. Submit and poll rather than one long request, so a 300 s node timeout never cuts a run in half. Bearer-protected like the two vendor endpoints.

```http
POST /jobs
{ "items": [
    { "id": "kw17-US", "kind": "search", "query": "running shoes", "country": "US", "activeStatus": "active", "maxItems": 300 },
    { "id": "b-775991435791863", "kind": "count", "page_id": "775991435791863" }
  ],
  "max_tries": 3 }
```

`202 { "job_id": "j_20260925_ab12cd34", "status": "queued", "items": 2, "poll": "/jobs/j_20260925_ab12cd34?wait_s=50" }`

- `kind: search` takes the `/facebook` request fields (`query`, `country`, `activeStatus`, `maxAds`); `kind: count` takes the `/adyntel` fields (`page_id`, `facebook_url` or `company_domain`, `active_status`, `media_type`, `max_results`). `id` is required and must be unique within the job.
- `400`: no items, more than `JOB_MAX_ITEMS` (200), a duplicate `id`, or an item that `/facebook` or `/adyntel` would refuse. `503 Busy`: `JOB_STORE_MAX` jobs are already queued or running, or the queue holds `JOB_QUEUE_MAX` items.
- A search already in the cache is answered without a lane (`cached: true`); an `ok` or `no_ads` job result feeds the same cache the single call reads.
- `kind: search` also takes `max_pages` (with `novelty_stop`, `empty_tol`, `max_ads`), the deep-paging fields of `POST /facebook`: GraphQL on the lane, no rendered page, up to `max_pages` x 30 ads, `direct_skipped: true` on the result. The rendered page serves at most 30 ads, so a second job with `max_pages` on the pairs whose `reported_total` was above 30 is how a run pages the strong searches deeper. A deep item never reads the cache and never writes it, and `max_pages` above `PAGE_MAX_PAGES` is a `400`.

```http
GET /jobs/{id}?wait_s=45&include_items=1&partial=0
```

Long-polls up to `min(wait_s, JOB_POLL_MAX_WAIT_S)` seconds (50; keep it under the caller's HTTP timeout), then answers:

```json
{ "job_id": "j_…", "status": "running | done | cancelled", "submitted_at": 1790350000.1, "started_at": 1790350000.2, "finished_at": null, "elapsed_s": 41.3,
  "counts": { "total": 41, "done": 40, "ok": 30, "no_ads": 6, "blocked": 2, "error": 1, "not_found": 1 },
  "results": [
    { "id": "kw17-US", "kind": "search", "query": "running shoes", "country": "US", "status": "ok",
      "ads_found": 30, "reported_total": 1039, "direct_skipped": false, "items": [ …the /facebook items… ],
      "lane": "lane-2", "exit_ip": "86.x.x.x", "seconds": 7.1, "decoded_bytes": 1210044, "cached": false, "error": null,
      "tries": [ { "lane": "lane-1", "ip": "92.x.x.x", "status": "blocked", "outcome": "rate_limited", "reason": "RateLimited: http 429 …", "seconds": 3.1, "decoded_bytes": 20011 },
                 { "lane": "lane-2", "ip": "86.x.x.x", "status": "ok", "outcome": "ok", "reason": "", "seconds": 4.0, "decoded_bytes": 1190033 } ] },
    { "id": "b-775991435791863", "kind": "count", "page_id": "775991435791863", "status": "ok",
      "number_of_ads": 1039, "page_name": "Shakti Mat", "envelope": { …the /adyntel envelope… }, "lane": "lane-3", "tries": [ … ], "error": null }
  ],
  "lanes": { "total": 2, "up": 2, "cooling": 0, "blocked": 0 } }
```

- `results` is in submission order and every result echoes its `id` and its `query`/`country` (or `page_id`), so the caller can assert `results.length == items.length` and join by id. It is `null` until the job is `done` unless `partial=1`.
- `include_items` sizes the answer. `1` is the full ad arrays and envelopes; `0` drops them for a cheap poll; `lite` keeps only the fields sourcing reads off an ad (page identity, `snapshot.caption`, `link_url`, `title`, `body.text`, `link_description`); `brands` returns no ads at all: each search result carries `brands`, one line per advertiser page and landing domain (`page_id`, `page_name`, `page_url`, `page_profile_uri`, `page_alias`, `page_category`, `page_like_count`, `domain`, `ad_count`, up to 3 `ad_texts`: the longest distinct ones of the first 10, catalogue `{{product.name}}` placeholders dropped, 400 chars each; a trailing `skipped: true` line counts unusable ads). A 900-ad search becomes a few dozen lines. Poll a large deep job with `lite` or `brands`: n8n stores every node's output.
- Result statuses: `ok`, `no_ads` (the rendered page said 0), `blocked` (refused on every lane that could take it: `tries[].outcome` says `withheld`, `blocked` or `rate_limited` per lane), `error`, `not_found` (counts only). A `blocked` search is never turned into `no_ads`.
- `404`: unknown, expired (`JOB_TTL_S` after it finished), or lost to a restart: jobs live in memory only. `DELETE /jobs/{id}` cancels what is still queued (`error: "Cancelled: cancelled"` on those items); the item a lane is on finishes.

## Homepage fetch: `POST /fetch`

A homepage read from a datacenter address fails often: bot walls answer a bare server-side request with a 403. This reads the page the way a browser on a home connection would: through a lane's residential exit, with the Chrome TLS profile. It is not a lane try (no Facebook cookie jar, limiter or cooldown; a refusing homepage is the site's problem, not the exit's), and it never raises, so a caller pairing pages to brands by position never loses a slot.

```http
POST /fetch
Authorization: Bearer <API_TOKEN>
{ "url": "gymshark.com" }
```

`200` always, with `ok` (a 2xx/3xx answer with a non-empty body), `status`, `final_url`, `bytes`, `text` (at most `max_bytes`, default `FETCH_MAX_BYTES` 300 000), `summary` (below; only when `ok`), `error` (`http 403`, `ConnectionError: ...`, ...), `seconds`, `lane` (the exit used, round robin) and `proxied`. Optional `timeout_s` (default `FETCH_TIMEOUT_S` 15, ceiling 60) and `max_bytes`. `400` for a malformed or non-public `url` (localhost, a dotless name such as a container on the Docker network, a private, loopback, link-local or reserved IP literal); a bare domain gets `https://`. At most 5 redirects. `FETCH_CONCURRENCY` (8) fetches run at once, shared with fetch jobs. Measured on 14 homepages that had failed from a datacenter address: 6 loaded through a lane (4 of the 7 403s); the dead hosts and 404s stayed failed. Headers: `X-Status: ok|failed`, `X-Lane`.

### Fetch jobs and the page summary

A job whose items are all `{"id", "kind": "fetch", "url"}` (a bare domain is fine; `domain` works as an alias) reads every homepage on the fetch pool, never on the Facebook lanes. Prefer it to many synchronous `POST /fetch` batches: one stalled reply to a synchronous call can run into n8n's 300 s body timeout and fail the whole run. Fetch items cannot share a job with searches or counts (`400`). Cap: `FETCH_JOB_MAX_ITEMS` (600).

- `deadline_s` (default `FETCH_JOB_DEADLINE_S` 150): when it passes, every unfinished item answers `timeout` and the job is `done`. Each try is `min(FETCH_TIMEOUT_S, time left)` and none starts with under 3 s left; a try still running when its item timed out finishes in its thread and its answer is dropped. Poll with `partial=1` to see finished items early.
- Per item at most two tries. The second runs on another exit when the first never got an answer (DNS, refused, reset, TLS, proxy tunnel: then on `https://www.` + the host) or got 403, 408, 425, 429 or a 5xx. A 404 or other answer is final.
- An item whose url is invalid or non-public fails on its own (`failed`, `error: "invalid url: ..."`); the job goes on.
- Result: `{id, kind: "fetch", status: ok|failed|timeout|error, url, http_status, final_url, summary, error, lanes, tries: [{url, lane, status, ok, error, seconds}], seconds}`. No HTML. `counts`: `total, done, ok, failed, timeout, error`. `error` is a cancelled item.

`summary` (`fetch.page_summary`, plain parsing of up to 1.5 MB of the page, about 3 KB out): `title`, `description` (meta or og), `site_name`, `lang`, `platform` (shopify, woocommerce, bigcommerce, wix, squarespace, magento or null), `has_cart` (add-to-cart / buy-now text or cart and checkout links), `product_links` (links under `/products/`, `/product/`, `/collections/`, `/shop/`), `products` (up to 5 from the page's JSON-LD: name, price or lowPrice, currency, brand), `jsonld_types`, `brand_names` (JSON-LD Organization / Brand / Store), `marketplace_links` (amazon, walmart, etsy, ...), `where_to_buy` (stockists, find a retailer, wholesale), `saas_markers` (free trial, book a demo, pricing plans, per user, ...), `redirected_to` (the site the page ended on when it is not the one asked for, e.g. `linktr.ee`), `headings` (h1-h2, up to 8), `text` (visible text without scripts, styles, nav, header and footer, repeated lines once, 1,500 chars).

## `GET /health`

Unauthenticated, for uptime checks and the Docker `HEALTHCHECK`. Besides the request counters it carries the lanes: `lanes` (one row per lane: `id`, `port`, `exit_ip`, `state` `up|cooling|blocked`, `cooldown_left_s`, `probe`, `rotations`, `ip_changes`, the session and GraphQL session, `throttle_active`, `requests`, `ok`, `no_ads`, `not_found`, `blocked`, `errors`, `avg_response_ms`, `decoded_bytes`, `tries_1h`, `blocked_tries_1h`, `last_error`), `lanes_summary` (`total`, `up`, `cooling`, `blocked`), `block_rate_1h` (blocked tries over tries in the last hour, across lanes), `ip_stats` (per exit address: requests, blocks, average response time, first and last seen, which lanes; the last 100), `ports` (held and reserve), `queue_depth`, `doc_id_stale` (a GraphQL schema change that needs a human), and `jobs` (`queued`, `running`, `done`, `queue_depth`, `store`). `proxy` is `true` when every lane has an exit (`fallback_proxy` mirrors it for older checks); `max_concurrency` is the lane count; `throttle` and `mint_breaker` say how many lanes have each active.

Abridged, lanes omitted:

```json
{
  "status": "ok", "version": "0.4.3", "auth": true, "proxy": true, "max_concurrency": 2,
  "requests": 3, "ok": 2, "empty": 0, "retried": 1, "cache_hits": 1,
  "bad_request": 1, "blocked": 0, "rate_limited": 0, "results_missing": 0, "failed": 0, "in_flight": 0,
  "adyntel_requests": 4, "adyntel_found": 3, "adyntel_not_found": 1, "adyntel_cache_hits": 1, "adyntel_resolve_hits": 1, "adyntel_short_counts": 0,
  "adyntel_by_page_id": 1, "adyntel_by_url": 1, "adyntel_by_domain": 2, "busy": 0, "budget_exceeded": 0,
  "paged_requests": 0, "paged_pages": 0, "paged_decoded_bytes": 0,
  "throttled_pages": 0, "throttled_recovered": 0, "throttle_skipped_direct": 0, "brand_withheld": 0, "brand_recovered": 0,
  "lane_retries": 0, "jobs_submitted": 0, "job_items": 0, "fetch_requests": 0, "fetch_ok": 0, "fetch_failed": 0,
  "sessions": { "live": 1, "warm": 1, "sessions_minted": 1, "sessions_retired": 0, "retired_by_reason": {},
                "challenges": 1, "calls": 6, "bytes": 4711140, "misses": 1, "rate_limited": 0,
                "session_dead": 0, "blocked": 0, "transient": 0,
                "plain_calls": 1, "plain_blocked": 0, "plain_dead": 0, "busy": 0 },
  "cache": { "entries": 1, "hits": 1, "misses": 1, "evictions": 0, "expired_dropped": 0 },
  "brand_cache": { "entries": 3, "hits": 2, "misses": 4, "evictions": 0, "expired_dropped": 0 },
  "throttle": { "active": false, "lanes_throttled": 0 },
  "mint_breaker": { "open": false, "lanes_open": 0 }
}
```

Counters reset on restart. The top-level counters are per request; `sessions` is per GET and per session event, with the page plugin and profile GETs a lookup makes counted apart as `plain_*`. `retried` counts searches and lookups that needed at least one extra GET; `sessions.misses` counts the GETs themselves. `results_missing` rising is the one to watch: it means the retries were not enough and a caller saw errors instead of ads. `adyntel_not_found` rising across many brands means the resolver, not Meta, is the thing to look at. `retired_by_reason` says which limit is retiring sessions.

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
uv run facebook-ad-library search "running shoes" --lane 1 --summary                              # on the second configured lane
uv run facebook-ad-library diag --lane 0 --lane-ip --repeat 3                                     # learn lane 0's exit IP three times; exit 5 if it moves
uv run facebook-ad-library diag --slug shaktimats --save-dir diag-out                             # the plugin and the profile page
```

`brand` makes one lookup the way `POST /adyntel` does and prints the envelope; `--summary` prints one line per ad with the decoded clip durations instead. Exit codes: 2 no input, 3 a vendor failure. `diag` prints each GET as it happens (the challenge seen and cleared, the cookies, the shape of the page, its ad count and, for a page view, the total and whether the page is known) and can repeat the same page on one session to show the miss rate; `--slug` fetches the two pages a vanity URL is resolved through and reads the page id from each, which is the deploy check for those page classes from a new exit. Exit codes: 2 the session was refused, 3 the GET failed, 5 every page came without results.
