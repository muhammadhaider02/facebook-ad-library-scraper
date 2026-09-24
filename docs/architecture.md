# Architecture

## What it replaces

Two vendors, both of them fronts for the same public page.

**Adyntel**, called by `01 · Find The Founder` (`KPIhCvLKtyMYZR40`) and `02 · Learn About The Brand` (`LlXYr9cMoypAFYA4`) once per brand: a domain or a page URL in, the brand's ads and their total count out, about $0.0088 a call and 1 to 3 calls a brand. 01 needs the live active count for its 50+ gate, a number that reaches founders' inboxes as "LIVE AD COUNT"; 02 needs the top ads by lifetime impressions with their copy, cards, CTAs, dates and video URLs for Deepgram. What the seven call sites read is recorded in `adyntel.md` (22 Sep 2026) from the live node code; [How a brand lookup is made](#how-a-brand-lookup-is-made) is the answer.

**Apify**: Stage 0 of the SmartLead pipeline (`00 · Find Brands`, `CtyWWHDoi0316VU7`, on the VPS n8n) calls the Apify actor `igolaizola~facebook-ad-library-scraper` once per keyword-and-country pair, from the node `Apify: Facebook Ad Library`:

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

The Ad Library is a logged-out React site. When its search page is requested, Meta runs the search on the server and embeds the first page of results in the HTML, as a prefetched Relay stream that is byte-for-byte what the frontend's own first GraphQL call would return. This service reads that and nothing else: **one GET per keyword-and-country pair**. Measured against the live site on 19 Sep 2026 from a laptop and from the VPS (the runs are under [Measured against Apify](#measured-against-apify)):

1. **The GET.** `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=NZ&q=<keyword>&search_type=keyword_unordered&media_type=all`, the URL the site's own address bar shows. On a fresh cookie jar it answers `403 Client challenge` with a 481-byte page whose script does `fetch('/__rd_verify_<token>?challenge=3', {method: 'POST'})` and reloads. The path is relative. One POST to it returns `200` and a `rd_challenge` cookie (`Max-Age=86400`); the re-GET returns the page and sets `datr`. Later GETs on that jar are not challenged: 41 in a row were measured without one.
2. **The page.** 0.6 to 1.7 MB of HTML with about 40 `<script type="application/json">` blobs. One of them, when present, holds `RelayPrefetchedStreamCache` → `result.data.ad_library_main.search_results_connection` with `edges[].node.collated_results[]` and a `page_info`. The service parses only the blob that mentions `search_results_connection` and flattens the collated results, unique by `ad_archive_id`, in page order. Up to 30 ads per page; 15 to 20 distinct advertisers among them on the productive keywords measured.
3. **Three shapes.** The page comes in three shapes, from every address, decided per request on Meta's side:

   | Shape | Size | Marker | Meaning | What the service does |
   |---|---|---|---|---|
   | ads | 0.6 to 1.7 MB | results blob with edges | the result | `200` with the items |
   | empty | ~582 KB | results blob with no edges | the keyword has no active ads in that country | `200 []` |
   | miss | ~573 KB | no results blob | Meta skipped the server-side prefetch; about 1 in 4 requests | retry, up to `SSR_RETRIES` (2) more GETs; if every attempt misses, `503 ResultsMissing` |

   A miss is never turned into an empty list, because Stage 0 reads an empty list as "no inventory" and retires a keyword after two of them. The real empty result is distinguishable, so the retry never loops on a keyword that is simply dry.

Why not the GraphQL endpoint the frontend uses to scroll past the first page: Meta answers `POST /api/graphql/` from datacenter addresses (Hostinger, GitHub's runners) with error `1675004` on the very first call of a fresh session, keyed on the source address, while it serves the page to the same address without a throttle. From a residential address it gave about 10 ads a call regardless of the `first` asked for, so three calls, which is what the first version of this service made, gave the same 30 the page gives in one. Nothing is lost. The measurements behind that are below.

**That held until 24 Sep 2026, when Meta began withholding the ad payload from the VPS address entirely.** The page still renders, the total is still correct, and `edges` is empty. GraphQL is now the recovery path, reached through a residential proxy and only for the requests that were actually refused - see [The throttle](#the-throttle) below.

## How a brand lookup is made

The same page, opened for one advertiser instead of a keyword: `https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&view_all_page_id=<page id>&search_type=page&media_type=all`, the URL the site's own "See all ads" link opens. Measured 22 Sep 2026 from the laptop and from the VPS, with identical numbers from both (the pages are under `diag-out/adyntel-probe/` on the laptop and `/opt/fb-diag/adyntel-probe-vps/` on the VPS):

| Page | What the embedded blob carries | Shakti Mat, page 775991435791863 |
|---|---|---|
| page view, `active_status=active` | `search_results_connection.count` = the page's live ad count, the first 30 ads, `page_info` | count 1027, 30 ads |
| `active_status=all` | the lifetime count | 2005 = 1027 active + 978 inactive |
| `active_status=inactive` | | 978 |
| `media_type=video` | live video ads only | 489, all `display_format: VIDEO` |
| a page with 10 ads | | count 10, 10 ads: the count is exact, not a rounded "~N" |
| a page id Meta does not know | `count: 0`, no edges, `ad_library_page_info: {"page_info": null}` | not found |
| a keyword search for the domain | the ads whose caption or landing page mentions it, from every advertiser | count 1003, 30 ads, all Shakti Mat; `gymshark.com` gave 5 advertisers in 30 ads |

Every page embeds `sortData: {"mode": "total_impressions", "direction": "desc"}`, page view and keyword search alike, which is the vendor's order too. The page's own record lives in a smaller blob earlier in the document (`page_name`, `page_is_deleted`) and, without the name, in the results blob (`hidden_ads`, `related_pages`); `find_page_record` merges every copy, and a page is known when any copy is a record rather than `null`. That is how "unknown page" is told apart from "known page with no ads", which the workflows route on.

The resolver ladder, in `brand.py`:

| Input | GETs | How |
|---|---|---|
| `page_id` | 1 | the page view |
| `facebook_url` carrying the id (`/p/<Name>-<id>/`, `/people/…/<id>/`, `/pages/…/<id>/`, `profile.php?id=`, a numeric path) | 1 | the page view; a numeric path that Meta does not know as a page is a user id (stored page URLs are often that shape) and is then resolved as a handle |
| `facebook_url` with a vanity handle | 2 | the public page plugin `https://www.facebook.com/plugins/page.php?href=https://www.facebook.com/<handle>`, ~45 KB, not challenged, with the id in one link (`facebook.com/<id>?ref=embed_page`); then the page view. When the plugin renders but knows no such page, and `BRAND_PROFILE_FALLBACK` is on, the profile page `facebook.com/<handle>` (~1.3 MB) is read for `"delegate_page":{"id":…}`. Never `userID` or `al:android:url`: on a New Page Experience page those are the user id, which the Ad Library answers with an empty page |
| `company_domain` | 2 | a keyword search on the bare domain across every country and every status (a brand whose ads are all retired is still found, as the vendor did); the page with the most ads whose caption, link URL or card link URL is on the brand's registrable domain is the brand, ties going to a page whose name carries the domain's stem, then to the larger page; then the page view. No ad landing on the domain is not found |

`media_type: video` forces `active`, as the vendor did. The plugin and profile GETs share the session's cookie jar and pacing and count against the same limiter, but they are classified apart (`plain_*` counters): a wall or a refusal there is a dead session, never mistaken for "no such page".

Why the domain search works as well as it does: the Ad Library shows every ad's landing domain as its `caption` (the bare domain, `probablyillegal.com`), so a keyword search on the domain is close to an exact "ads landing here" query, and `pick_page` only has to choose among the advertisers whose ads land on it. A brand-**name** keyword search is not a substitute and is not offered: Meta's `keyword_unordered` is fuzzy on words and does not index page names. Measured 23 Sep 2026 with `search --country ALL`: "Home and Sprout" returned 30 ads from 11 unrelated pages and not the brand's own page (which the domain search finds, 1 live ad); "Solar Titans" returned the brand's page last of 29 and took 44 s on a single GET; "Probably Illegal Game" 5 owned ads among 30 from 16 pages.

Measured from the VPS after the 0.3.0 deploy, 22 Sep 2026: every resolver path answered from the VPS address exactly as from the laptop (page id 2.3 s, vanity URL through the plugin 3.9 s, domain 6.6 s, video 6.1 s with one miss retried), the profile page was served too, and a burst of 20 page views on one session at 12 a minute took 96 s with 20 `200`s, one challenge on the first GET, no misses and no throttle.

## The throttle

On 24 Sep 2026 Meta stopped giving this address the ads. Not with a `403`, not with a `429`, and not by failing: the page renders, the total is right, and `edges` is empty.

```
search  'running shoes' US        count=50001   ads=0   590 KB of page   no error
brand   775991435791863           count=1039    ads=0   page name "Shakti Mat"   no error
```

Production served **722 empty results out of 1,320 requests** this way before it was found, and `ok` had not moved in over a hundred searches. Nothing in the logs said so, because from the service's point of view every one of those was a successful GET of a keyword with no inventory.

It is the address, not the code or the session. Proven by running two containers from the same image in the same minute: the VPS address returned 0 ads, a residential exit returned 30. Thirteen sessions had rotated normally; `rate_limited` and `blocked` were both 0.

### Why it is worse than an outage

An empty list is a *valid answer*. Stage 0 reads it as "this keyword has no inventory" and retires the keyword. Workflow 01 reads it as "this brand's ads cannot be seen", fails ownership verification, rejects the brand and **spends one of its three retries** - on a fault that was never the brand's. Three of those and the row is Abandoned permanently. When this was found, 11 of 11 brands in one execution sat at `Retry Count: 2`, and 749 rows were already exhausted.

So the service must never answer a withheld payload as an empty success.

### How a request recovers

1. **The direct GET happens first, free**, on the VPS address as always.
2. **The two empties are told apart by the total.** `count: 0` is an honest answer and returns `200 []` without touching the proxy. `count > 0` with no ads is the throttle's signature.
3. **Only a withheld request pays for the residential exit.** Don't proxy what isn't blocked - the rendered page is ~1 MB, and routing all of it through a proxy costs a megabyte per call for pages already known to be empty.
4. **The recovery differs by endpoint.** A search re-asks over GraphQL, which answers the same query at ~7 KB an ad against the page's 24.7 KB, capped at `FALLBACK_MAX_PAGES`. A brand lookup re-fetches the same rendered page through the proxy, because a page view has no paging to do.
5. **If the ads are still missing, the answer is `503`.** A vendor failure costs the caller no retry; an empty success costs it one.

### Skipping the GET that is known to fail

While the throttle is on it is total, not sampled: 24 of the 24 searches with ads to give came back empty in one sourcing cycle. Re-making the direct GET each time costs ~3 s and ~1 MB to be told what the previous search established - about two minutes and 40 MB across a 40-keyword cycle.

One withheld page therefore suppresses the direct GET for `THROTTLE_MEMORY_S`. It **expires rather than latching**, because while it is set every search pays the proxy: one direct GET per window is the price of noticing Meta has stopped, and any direct page that does carry ads clears it at once.

### Measured: depth does not pay

The obvious response to a 30-ad ceiling is to page deeper. It was tried, and the tail is worth very little. Advertisers per page, across 10 keywords paged to Meta's true end on 24 Sep:

| Keyword | Pages | Advertisers | Per page |
|---|---|---|---|
| toddler balance bike | 6 | 26 | **4.33** |
| reusable makeup remover pads | 1 | 3 | 3.00 |
| portable neck fan | 13 | 32 | 2.46 |
| red light therapy mask | 108 | 194 | 1.80 |
| magnesium glycinate gummies | 37 | 64 | 1.73 |
| collagen coffee creamer | 41 | 52 | 1.27 |
| grounding sheets | 60 | 62 | 1.03 |
| silk sleep bonnet | 59 | 30 | 0.51 |
| beard growth kit | 89 | 42 | 0.47 |
| posture corrector device | 41 | 16 | **0.39** |

The first pages carry distinct advertisers and the tail re-samples the same handful - `beard growth kit` spent 89 pages to find 42, at 18.7 ads each.

Then it was tried in production. `FALLBACK_MAX_PAGES` was raised from 8 to 20, cycle 3951 against cycle 3931:

| | 8 pages | 20 pages |
|---|---|---|
| Pages | 64 | 169 |
| Decoded | 6.62 MB | 17.32 MB |
| Run time | 6m30s | 13m47s |
| `brands_new` | 25 | **25** |
| `brands_verified` | 20 | **16** |

2.6x the proxy spend and twice the wall clock for no additional new brands. The keywords differ between cycles so some of that is luck, but it agrees with the table above and with 00's own `Select Keyword And Country` node, which concluded the same from exec 1014: *"the lever is MORE KEYWORDS PER RUN, not more ads per keyword."*

Three independent measurements. The cap is 8.

### What `max_items` is not

`max_items` sizes the response. It is **not** a paging stop, and conflating the two has caused the same bug twice: passed as the paging ad target, a search ends as soon as it has that many ads, so a 150-page cap silently became 9 pages. `max_ads` is the target and defaults to 0, meaning the page cap decides.

## Why no browser, and why Chrome TLS

The Ad Library's only gate is the challenge above plus a TLS-fingerprint check. Measured 19 Sep 2026: plain `curl` clears the challenge and receives the cookie, and is then answered with a `400 Sorry, something went wrong` error page on every request after it, including the homepage. `curl_cffi` with `impersonate="chrome"` is answered normally. So there is no Chromium in this service: one Python process, one curl handle per session.

`FB_IMPERSONATE` is `chrome`, an alias for the newest Chrome profile the installed `curl_cffi` knows (`chrome150` in 0.16.3). A `400` after a clean challenge is the fingerprint being rejected; the service names that symptom in its `ScrapeBlocked` message.

## Sessions

The unit of identity is a session, not a request: one cookie jar (`datr`, `rd_challenge`), created empty and filled by its first GET. `FbSession` in `session.py` holds one; `_SessionPool` keeps `SESSION_POOL_SIZE` (3) of them warm, hands them out round-robin under a `MAX_CONCURRENCY` (3) semaphore, creates lazily on the first lease, and drops a session on its way back if it is retired or expired. A session expires after `SESSION_MAX_REQUESTS` (200) GETs or `SESSION_MAX_AGE_S` (7,200 s), both starting points rather than measured limits; `/health` reports `retired_by_reason` so the limit that actually bites can be seen. A session that gets `MISS_STREAK_RETIRE` (5) pages without results in a row is retired as suspect; a real empty result resets the streak. A lookup that cannot get a slot inside its budget is answered `Busy` rather than queued past the caller's own timeout.

Pacing has two layers: a random gap of `SPACING_MIN_S` to `SPACING_MAX_S` (2 to 5 s) between GETs on one session, and a process-wide ceiling of `RATE_LIMIT_PER_MIN` (20) GETs a minute across all sessions and both endpoints. Stage 0 needs one GET per pair, 40 pairs an hour; a brand lookup is 1 to 3 GETs and 01 and 02 make up to 3 and 4 lookups per brand, one brand at a time each. Measured 22 Sep 2026 from the VPS: 20 page views in 96 s at 12 a minute, then the harness's ~110 lookups in 10 minutes at 20 a minute, all served, no challenge after the first, no throttle, with Stage 0's hourly run completing normally in between; at 12 the harness's back-to-back calls hit the lookup budget once.

## Retries and the error ladder

`fetch` classifies every answer and raises a typed error for anything that is not an Ad Library page. `search` then applies the recovery rules:

| Answer | Raised | What `search` does |
|---|---|---|
| `5xx` or a network error | `ScrapeFailed` after in-call retries at 1 s and 3 s | retire the session, swap once |
| `429` | `RateLimited` | sleep `RATE_LIMIT_SLEEP_S` (60) once and retry on the same session; a second one retires it and swaps once |
| `400` (the TLS symptom), `403` without the challenge marker, a challenge page with no URL | `ScrapeBlocked` | raise; a new jar would not help |
| any other non-`200`, or a `200` that is not the Ad Library page (no `LSD` token blob) | `SessionDead` | retire the session, swap once |
| a page without the results blob | (not an error yet) | retry, up to `SSR_RETRIES` more GETs on the same session |
| a page view whose total is below the ads on it | (not an error yet) | retry, up to `BRAND_SSR_RETRIES` more GETs; when every attempt is short, the ads on the page are the total. Meta now and then serves the count unfilled: first seen in production on 23 Sep 2026 (02's execution 3235, The Sleep Co, `count: 0` above 30 video ads, 55 on the next fetch), never in the 100-plus harness lookups the day before. Passed through, a 0 above a page full of ads would fail 01's 50-ads gate for a brand that clears it |

One session swap is allowed per search. A second failure escapes as the API's `503`; two `SessionDead`s in a row become `ScrapeBlocked`. Every retry, swap and sleep is priced against the deadline before it starts; the first GET is never skipped.

A brand lookup (`brand.py`) uses the same ladder with two differences forced by its 25 s budget: a `429` swaps sessions at once instead of sleeping 60 s, and a page plugin or profile page that comes back as a login wall (a `200` without the site's token blob) is `SessionDead`, swapped once and fetched again on the fresh session.

| Status | Type | Cause |
|---|---|---|
| `400` | `ValueError` | no `query`, a country that is not two letters or `ALL`, an unknown `activeStatus` or `media_type`; on `/adyntel`, no `page_id`, `facebook_url` or `company_domain` |
| `503` | `ScrapeBlocked` | challenge would not clear, `403` without the challenge marker, `400` after the challenge, two dead sessions in a row, or a wall twice |
| `503` | `RateLimited` | `429` again after the one sleep (searches) or on the fresh session (lookups) |
| `503` | `ResultsMissing` | every attempt inside the budget came back without the results blob |
| `503` | `BudgetExceeded` | a lookup's next GET, priced with the limiter's next free slot, would not finish inside `BRAND_BUDGET_S` |
| `503` | `Busy` | every concurrency slot stayed taken for a lookup's whole budget |
| `503` | `ScrapeFailed` | network failure or `5xx` that survived the retries |

## The time budget

The numbers only make sense together, and `.env.example` carries the arithmetic:

| | |
|---|---|
| one GET, worst case | `SPACING_MAX_S` + 60 / `RATE_LIMIT_PER_MIN` + `REQUEST_TIMEOUT_S` = 5 + 3 + 30 = 38 s |
| one search, worst case | (1 + `SSR_RETRIES`) × 38 s + `RATE_LIMIT_SLEEP_S` 60 s = 174 s |
| Stage 0 node timeout | 300 s |
| measured, laptop, no contention | 3.6 to 6.4 s for one GET including the challenge; 45 s when a fifth GET inside a minute waited for the limiter at its old setting of 4 |

`SCRAPE_BUDGET_S` (240) is what protects the workflow. Nothing cancels a request once it starts: Starlette does not cancel a handler when the client disconnects and the search runs in a thread, so a caller that gives up does not free the session. Before each GET after the first the search checks that a whole GET's worst case still fits before the deadline; if not, a search that has only misses so far is a `503`. `stop_grace_period` in `docker-compose.yml` is 250 s so a redeploy cannot kill a search mid-budget.

A brand lookup has a budget of its own, `BRAND_BUDGET_S` (25), because 01 and 02 call it from Code nodes whose own request timeout is 30 s (45 s for the video call): the service must answer, with a `503` if need be, before n8n's timeout hides the reason. The clock starts before the wait for a concurrency slot. Before every GET after the first, the limiter's next free slot plus a page's worst case (`SPACING_MAX_S` + 5 s) is priced against what is left, the GET itself is given the remaining time as its timeout, and a transient retry that would not fit is not made. Measured 22 Sep 2026: 1.2 to 1.8 s per page GET from both addresses, 0.9 to 7.5 s for a whole lookup by id, vanity URL or domain from the laptop.

## Cache

`api.py` keeps the whole page's items for `CACHE_TTL_S` (86,400 s), keyed on the normalised query, the upper-cased country and `activeStatus`; `maxItems` is applied on the way out, so a request for 30 and one for 80 share one entry. Stage 0 re-searches an exhausted keyword's remaining country slots and retries a pair on error, so the same request arrives more than once a day; a hit costs nothing and is answered in a few milliseconds with `X-Cache: hit`. Empty results live `CACHE_EMPTY_TTL_S` (3,600 s). The cache is bounded at `CACHE_MAX_ENTRIES` (2,000) and lives in memory, so a restart clears it.

Brand lookups keep a second, shorter-lived cache: the page view under (page id, status, media) for `ADYNTEL_CACHE_TTL_S` (600 s), and a domain's or URL's resolved page id for `CACHE_TTL_S`. 02's three calls per brand then cost one resolution and three page views, and a page id Meta does not know is pinned for `CACHE_EMPTY_TTL_S`; a vanity or a domain that was not found is not pinned, in case the plugin or the keyword search had an off moment. Ten minutes is short on purpose: a second harness run measures the site, not the cache.

## Memory and concurrency

One Python process with no browser. Each in-flight search or lookup holds one page of up to 1.7 MB while it is parsed; the two caches are the only things that grow, and both are bounded. The compose limits (512m) are a blast-radius guard for the host, not a working budget.

`MAX_CONCURRENCY` (3) bounds searches and lookups in flight: three workflows call the service, each one request at a time, and three slots let them overlap without queueing. The global limiter, not the semaphore, is what shapes the traffic Meta sees.

## Measured against Apify

The address question first, 19 Sep 2026, same commit and same query from both machines, no proxy anywhere. Every request body and page from these runs is kept under `diag-out/address-classification-2026-09-19/` (gitignored) with the probe that produced them.

| Test | Source address | Result |
|---|---|---|
| GraphQL, control | laptop, residential | 10 of 10 searches, about 10 ads each |
| GraphQL, laptop-minted session replayed | VPS, Hostinger v4 | refused at call 1 in 46 ms with `1675004`; the same session kept working from the laptop |
| GraphQL, fresh session; again after 10 min idle | VPS v4 | refused at call 1 both times |
| GraphQL over IPv6 | VPS v6 | reaches Facebook, refused at call 1; other addresses in the /64 are not routed by Hostinger |
| Legacy `/ads/library/async/search_ads/` | both | `404`, the endpoint is gone |
| GraphQL `first` 10 / 30 / 60 / 100 | laptop | identical body, about 10 edges: Meta ignores `first` |
| **Page GET, 20 keywords** | **VPS v4** | **20 of 20 `200`, no challenge after the first, 345 ads; 5 pages came without the results blob** |
| Page GET, the 3 no-results keywords retried ×3 | VPS v4 | 6 of 9 carried ads: the miss is per request, not per keyword |
| Page GET, the 2 exhausted keywords ×3 | VPS v4 | 0 ads with an explicit empty results blob on 5 of 6; GraphQL from the laptop also gives 0 |
| Page GET, laptop control | laptop | ad counts per keyword identical to the VPS run |

Then the pipeline's own harness, the same day: the `scraper-testing` workflow (`0q7jtSF7FG0cbyBe`) with Stage 0's node cloned verbatim, pointed at this service, and Stage 0's `Extract Dedupe And Filter` logic verbatim in `Measure FB Response`. Twelve keyword-and-country pairs from the live bank, ten productive and two exhausted.

| Metric | This service | Apify, Stage 0's own telemetry over 12 runs |
|---|---|---|
| Request failures | 0 of 12 | 0 to 1 per 40-call run |
| Ads per call | 14.2 (30 on the four most productive, 0 on both exhausted) | mean 10.6, range 3.75 to 29.5 |
| Unique domains and pages | 56 and 87 from 170 ads | about 12 ads per advertiser at 120 ads a keyword |
| `page_id`, `page_profile_uri`, `page_category`, `page_like_count` | 170 of 170 | same fields, same coverage |
| `page_alias` | `""` on every ad | `""` from the actor too |
| Domain source | caption 157, link_url 9, about_text 0, none 4 | caption on 119 of 120 in the run fb.md measured |
| Ad-farm category filter | fired on 46 ads | |
| Wall clock per call | 1.4 to 5.8 s; 45 s twice while the 4-a-minute limiter held the harness's burst | 300 s allowed |

The two 45 s calls are the harness sending its 12 calls back to back; Stage 0 sends under one a minute and will not see that wait. What the run does not show is volume over days: about 60 page GETs in one day is the whole evidence that the VPS address is not throttled on the page, and `results_missing` and `blocked` on `/health` are what would move first if that changed.

## Measured against Adyntel

The pipeline's own harness, 22 Sep 2026: two lanes in the `scraper-testing` workflow (`0q7jtSF7FG0cbyBe`), each a clone of the production nodes pointed at `/adyntel`. Adyntel was not called; the baselines are the answers it had already written. `RATE_LIMIT_PER_MIN` was 12 for the first two runs and 20 for the rest; the service ran on the VPS beside Stage 0, whose 12:45 run completed normally in the middle of the lanes.

**Lane 01, the qualification gate.** Fifty-three brands from 01's executions of the previous 14 hours (15 with 100+ live ads, 15 near the gate, 10 under 20 or not found, 5 that 01 had resolved through a page fallback, 8 with a page id stored by Stage 0), each looked up twice: by domain through a clone of `Adyntel: Ad Count`, and by the page id Adyntel had used, through a Code node with the bearer inline.

| Metric | Canary, 12 brands (run 2705) | Full list, 53 brands (run 2713) | Full list again, 12 min later (run 2723) |
|---|---|---|---|
| Brands Adyntel had found, found in-house | 12 of 12 | 46 of 46 | 46 of 46 |
| Gate agreement (50+ live ads) | 12 of 12, no flips | 46 of 46, no flips | 46 of 46, no flips |
| Exact count match | 10 of 12; median difference 0 | median difference 0, mean 4.7 ads; 45 baselines under 24 h old, median relative difference 0% | median 0, mean 4.2 |
| Domain path resolved to the page Adyntel used | 11 of 12 | 49 of 50 | 49 of 50 |
| Both paths found the same page: counts equal | 11 of 11 | 51 of 51 | 51 of 51 |
| Errors | 0 | 0 | 0 |
| Page-path wall clock, uncached | 7.6 s (one) | p95 4.5 s, max 11.5 s | max 6.9 s |
| Counts against the previous run | | | 51 of 53 identical; Snapmaker 677 → 704 and Cryptozoic 169 → 171, live counts moving |

What did not match, and why: Snapmaker 832 → 677 and Kids Dreams 82 → 63 against baselines 3 to 4 hours old, both large advertisers whose live count moves by the hour (the same page id on both sides, so it is the count that moved, not the page); Traitors Aboard, where Adyntel had answered 0 ads on page 101461635716856 while the domain resolver chose page 258509494006770, the one Stage 0 had stored for the brand, with 92 live ads landing on savana-games.com; Board.Fun, whose ads land on another domain, not found by domain (Adyntel found it) but found by its stored page id with 187 ads against 186. Two brands 01 had never resolved (Kids Australia, WobbleWink, both stored as numeric user-id URLs) were found by domain and by URL with 614 and 87 live ads.

**Lane 02, the research call.** Twelve brands with a stored research bundle, through clones of `Adyntel: Ad Creative` (`active_status: all`), `Analyse Ad Strategy` and `Collect Video Ads` (`media_type: video`), with one HEAD per selected clip, run twice ten minutes apart.

| Metric | Run 2708 (12 a minute) | Run 2719 (20 a minute) |
|---|---|---|
| Creative call found | 11 of 12 | 12 of 12 |
| Video call found | 12 of 12 | 12 of 12 |
| Clips selected by 02's own 8-clip / 600 s rule | 95 | 95 |
| Clips fetchable anonymously (`200`, `video/mp4`), with a decoded duration | 95 of 95 | 95 of 95 |
| Live themes non-empty | 11 of 12 | 12 of 12 |
| Formats seen | IMAGE, VIDEO, DCO, DPA, CAROUSEL | same |
| Counts between the two runs | | identical on 11 brands, Copper Pearl 535 → 537 |

The one miss in run 2708 was the service refusing a lookup with `BudgetExceeded` (12 s left, 15 s needed after one GET) while the harness's back-to-back calls sat on the 12-a-minute limiter; at 20 a minute the same brand answered in the next run, and the refusal is the designed outcome, a `503` before the Code node's 30 s. Two brands' lifetime counts (Mamma Mia Covers 238, HolStrength 270) sit under the live counts their bundles carry (290, 343), which are older sheet values; the page view shows 0 inactive ads for both, so lifetime equals live for them today.

## Measured against Brave

`01 · Find The Founder` used Brave Search, plus a Haiku call to choose among its results, as the last step of its Facebook-page fallback, after the domain lookup, the website footer and the page Stage 0 had stored. Read on 23 Sep 2026 before the switch: in the 173 runs the telemetry table holds (16 to 23 Sep), Brave ran 7 times, for 0 qualified brands, against 467 brands served by the stored page. Its two retained runs (executions 2607 and 2715, 22 Sep, still on Adyntel): for notjustsundays.co it chose `facebook.com/NotJustSundays`, a podcast page with 0 ads; for blonderesin.com Haiku answered NONE.

The eight brands of those two runs that had fallen through Adyntel's domain lookup, replayed through `brand --domain` from the laptop:

| Domain | Old chain | In-house domain lookup |
|---|---|---|
| notjustsundays.co | Brave → podcast page, 0 ads | page 865741726622144 "Not Just Sundays", 954 live ads |
| blonderesin.com | Brave → NONE | page 118393287932862, 2 |
| publishorperish.games | stored page rejected (a reseller) → Needs Review | page 330052933516491, 80 |
| probablyillegal.com | stored page → 53 | 53 |
| solsoragames.com | stored page → 8 | 8 |
| nookfan.com | stored page → 69 | 69 |
| octagonstudio.com | stored page → 18 | 18, one miss retried |
| homeandsprout.store | stored page → 1 | 1 |

8 of 8 found, each on a page whose ads land on the brand's domain (`pick_page` accepts nothing else), 4 to 12 s each. Twenty-two older Needs Review rows whose reason came from the page lookup (11 to 22 Sep), replayed from the VPS: 20 of 22 found a page, 4 with 50 or more live ads (berissablinds.com 60, sweetmyo.com 69, wiseharvestfreezedriedfoods.com 63, yerbamagic.com 72), 9 with 1 to 49, 7 with 0; the two not found (engineerednutrition.us, healthyfamwell.com) have no ad landing on the domain. 01 does not retry a Needs Review row on its own; the status stays until someone resets it.

So the Brave slot was replaced with no search at all (deployment.md, "Cutting the production workflow over"): a brand reaching it has already failed the in-house domain search, and it goes to Needs Review as a Brave NONE did. The one thing Brave could do that no Ad Library search can is surface a page whose ads carry no landing link at all, which `Resolve Ads Via Searched Page` trusts on a page-name match; no such success appears in the retained runs, and such a brand still lands in Needs Review rather than being dropped.

## Layout

| File | Role |
|---|---|
| `src/facebook_ad_library/scraper.py` | the search and page-view URLs, the challenge URL, the page markers and the results and page-record parsers, the vanity and domain resolvers, and the search with its budget and recovery rules |
| `src/facebook_ad_library/brand.py` | one brand lookup: the resolver ladder, the page view, its own budget and recovery rules |
| `src/facebook_ad_library/session.py` | one session (the GET, the challenge, pacing, classification of the answer, plain GETs for the plugin and profile pages), the global limiter and the pool; the transport seam the tests replace |
| `src/facebook_ad_library/mapping.py` | turns a collated result into the item shape Stage 0 reads |
| `src/facebook_ad_library/adyntel_mapping.py` | turns a lookup into the envelope the 01 and 02 call sites read, with `duration_s` decoded from the video URLs |
| `src/facebook_ad_library/cache.py` | the TTL cache, used twice |
| `src/facebook_ad_library/api.py` | FastAPI surface: both endpoints' request aliases, bearer check, caches, error bodies, headers, counters |
| `src/facebook_ad_library/proxy.py` | `SCRAPER_PROXY` parsing and the rotating-gateway guard |
| `src/facebook_ad_library/config.py` | environment variables, read once |
| `src/facebook_ad_library/__init__.py` | the `facebook-ad-library` CLI (`serve`, `search`, `brand`, `diag`) |
| `tests/` | 190 tests against the saved fixtures; no network. `tests/fixtures/README.md` says how each fixture was cut from a live page and how to refresh it |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `API_TOKEN` | *(empty)* | bearer token callers must send; empty disables auth, for local testing only |
| `SCRAPER_PROXY` | *(empty)* | proxy for **every** request, as a URL or `host:port:user:pass`. Leave empty: the direct path is free and the rendered page is ~1 MB. Rotating gateway ports (Decodo 7000, DataImpulse 823) are refused at startup |
| `FALLBACK_PROXY` | *(empty)* | proxy for the **recovery** path only - the searches and lookups Meta answers with a total and no ads. Set this, leave `SCRAPER_PROXY` empty, and only refused calls are billed. Falls back to `SCRAPER_PROXY` |
| `FALLBACK_MAX_PAGES` | `8` | pages a recovered search may take. Raising it buys the worst pages there are; see [The throttle](#the-throttle) |
| `THROTTLE_MEMORY_S` | `600` | how long one withheld page suppresses the direct GET. Expires rather than latching: while it is set every search pays the proxy |
| `PAGE_BUDGET_S` | `240` | wall-clock ceiling on one paged call, so it answers inside the caller's node timeout. A run that stops here returns `X-Truncated: 1` with a cursor |
| `PAGE_MAX_PAGES` | `150` | ceiling on an explicit `max_pages` request; also the hard ceiling in code |
| `PAGE_NOVELTY_STOP` | `25` | pages with no new advertiser that end a paged search |
| `PAGE_EMPTY_TOL` | `8` | blank pages in a row that end a paged search. Stopping at the first one cost 135 ads and 25 advertisers on one keyword |
| `FB_DOC_ID` | *(empty)* | persisted-query id for `AdLibrarySearchPaginationQuery`. Discovered from the page bundles when empty, which costs ~21 MB a mint; pinning it costs ~0.9 MB |
| `FB_VARIABLES_JSON` | *(empty)* | JSON object merged over the GraphQL variables, for a schema change that needs a field the service does not send |
| `FB_IMPERSONATE` | `chrome` | `curl_cffi` TLS profile |
| `REQUEST_TIMEOUT_S` | `30` | per-request timeout |
| `SSR_RETRIES` | `2` | extra GETs when the search page comes without its results blob |
| `MAX_CONCURRENCY` | `3` | searches and lookups in flight |
| `SESSION_POOL_SIZE` | `3` | warm sessions |
| `SESSION_MAX_REQUESTS` | `200` | GETs before a session is retired |
| `SESSION_MAX_AGE_S` | `7200` | age before a session is retired |
| `SPACING_MIN_S` / `SPACING_MAX_S` | `2` / `5` | random gap between GETs on one session |
| `RATE_LIMIT_PER_MIN` | `20` | GETs a minute from this process, all sessions, both endpoints |
| `RATE_LIMIT_SLEEP_S` | `60` | sleep before the one retry on a `429` (searches only) |
| `MISS_STREAK_RETIRE` | `5` | consecutive pages without results that retire a session |
| `SCRAPE_BUDGET_S` | `240` | wall-clock ceiling per search; must stay under Stage 0's 300 s |
| `BRAND_BUDGET_S` | `25` | wall-clock ceiling per brand lookup, queue time included; must stay under the Code nodes' 30 s |
| `BRAND_SSR_RETRIES` | `2` | extra GETs when the page view comes without its results blob |
| `BRAND_PROFILE_FALLBACK` | `false` | also read the profile page when the plugin does not know a vanity handle |
| `CACHE_TTL_S` / `CACHE_EMPTY_TTL_S` | `86400` / `3600` | cache life for results with ads and without, and for resolved page ids |
| `ADYNTEL_CACHE_TTL_S` | `600` | cache life for a brand's page view |
| `CACHE_MAX_ENTRIES` | `2000` | size of each cache |
| `HOST` | `0.0.0.0` | bind address |
| `PORT` | `8002` | bind port; `8000` and `8001` are the siblings |
