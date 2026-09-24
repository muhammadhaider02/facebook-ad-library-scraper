# Deployment

## Where it runs

The same topology the two siblings run in.

| | |
|---|---|
| Host | the Hostinger VPS that runs the self-hosted n8n at `n8n.srv1980669.hstgr.cloud` |
| Checkout | `/opt/facebook-ad-library-scraper`, a clone of `main` over a read-only deploy key (`github-facebook` in the root SSH config), following the siblings' convention |
| Container | `facebook-ad-library`, built from the `Dockerfile` by `docker-compose.yml` |
| Network | `n8n_default`, the network n8n's own Compose project created; declared external so this file never owns it |
| Address from n8n | `http://facebook-ad-library:8002/facebook` |
| Beside it | `trustpilot-reviews` on `8000` and `reddit-reviews` on `8001`, deployed the same way |
| Proxy | none; `/health` reports `proxy: false`. The page GET this service makes is served to the VPS's own address without a throttle (architecture.md, [Measured against Apify](architecture.md#measured-against-apify)) |

Nothing is published to the host. Docker publishes straight past UFW, so even `8002:8002` behind the firewall would be public; the service is reachable only from containers on the network, and `API_TOKEN` still applies so a compromised container cannot drive it freely. The VPS already runs Traefik on 80/443 for n8n. There is no domain, no certificate and no reverse proxy for this service, and adding one would fail to bind.

## What the compose file sets, and why

| Setting | Value | Reason |
|---|---|---|
| `mem_limit` / `memswap_limit` | 512m / 512m | a Python process with no browser: 41 MiB resident idle, one page of up to 1.7 MB in hand while a search is parsed. The limit is a blast-radius guard for the host; hitting it means something leaked |
| `mem_reservation` | 128m | soft limit, enforced only when the host is short of memory |
| `init`, `pids_limit` | true / 128 | the box's convention; 8 PIDs measured idle, so 128 is a fork guard, not a budget. No `shm_size`: that exists in the siblings for Chromium and this image has no browser |
| `stop_grace_period` | 250s | `SCRAPE_BUDGET_S` plus slack, so a redeploy never kills an in-flight search |
| logging | json-file, 10m × 3 | one line per search plus one per session event; bounded anyway |

`RATE_LIMIT_PER_MIN` is what shapes the traffic Meta sees from the address; `MAX_CONCURRENCY` only bounds searches in flight.

## First deploy

```bash
git clone git@github-facebook:haider-ecombench/facebook-ad-library-scraper.git /opt/facebook-ad-library-scraper
cd /opt/facebook-ad-library-scraper
cp .env.example .env          # set API_TOKEN
docker compose up -d --build  # a few seconds: no browser to download
```

If the network name is wrong the container refuses to start with `network n8n_default declared as external, but could not be found`; `docker network ls` gives the real one.

## Update, rollback, logs

```bash
cd /opt/facebook-ad-library-scraper
git pull origin main && docker compose up -d --build        # deploy main
git checkout <sha> && docker compose up -d --build           # roll back to a known commit
docker compose logs -f --tail 100                            # follow the service log
```

`.env` is read at container start (`env_file`), so a changed value needs `docker compose up -d`, not a restart of the process inside. Keep a dated copy before editing it. Check `/health` is idle (`in_flight: 0`) before recreating.

Moving to 0.3.0 (the `/adyntel` endpoint) changes three defaults and adds four variables; `.env.example` has the reasons. Copy them into `.env` before the `up`: `MAX_CONCURRENCY=3`, `SESSION_POOL_SIZE=3`, `RATE_LIMIT_PER_MIN=20`, `BRAND_BUDGET_S=25`, `BRAND_SSR_RETRIES=2`, `BRAND_PROFILE_FALLBACK=false`, `ADYNTEL_CACHE_TTL_S=600`. A value left out takes the new default.

The container has no `curl`. Read health from inside it with:

```bash
docker exec facebook-ad-library python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8002/health').read().decode())"
```

## Verifying a deploy

1. The deploy check, from the VPS, before anything is pointed at the service:
   ```bash
   docker compose exec scraper uv run --no-sync facebook-ad-library diag --query "running shoes" --repeat 3
   ```

   | Outcome | Meaning | Next |
   |---|---|---|
   | `GET 1: ads, 30 ads, …` on most of the three | the address is fine | step 2 |
   | every GET `miss` | Meta is skipping the server-side prefetch for this address right now; the fix, if it persists, is a higher `SSR_RETRIES`, not a proxy | re-run in a few minutes; watch `results_missing` on `/health` |
   | `GET 1: ScrapeBlocked …400` | the TLS fingerprint was rejected | check `FB_IMPERSONATE` is a current Chrome profile for the installed `curl_cffi` |
   | `GET 1: ScrapeBlocked …403 without a challenge` | the address itself is refused the page, which no address has been so far | that would be new; save the page with `--save-dir` and read it before changing anything |

   Measured on the first deploy, 19 Sep 2026, commit `024d4c2`: 30 ads and 16 advertisers on each of the three GETs, one challenge, no misses, 1.1 MB pages.
2. A real search from the server:
   ```bash
   docker compose exec scraper uv run --no-sync facebook-ad-library search "running shoes" --country US --max 30 --summary
   ```
   Expect up to 30 ads from 10 or more advertisers in a few seconds; a second GET with `misses: 1` now and then is normal.
3. `/health` should show `auth: true`, `max_concurrency: 3`, and after the first search `sessions.challenges: 1` and `sessions.calls` equal to the GETs made.
4. Each successful call logs one line: `ok 'running shoes' US ads=30 attempts=1 misses=0 swaps=0 4.1s`. A retried one logs `page without results (1 of 3), retrying` before it.
5. The brand lookup, the three ways it resolves, from the VPS address. The page view is the same page class as the search and needs no separate answer; the plugin and the profile page are two page classes this address had never fetched before 0.3.0, so this is where a refusal of either would show:
   ```bash
   docker compose exec scraper uv run --no-sync facebook-ad-library brand --page-id 775991435791863 --summary
   docker compose exec scraper uv run --no-sync facebook-ad-library brand --url https://www.facebook.com/shaktimats --summary
   docker compose exec scraper uv run --no-sync facebook-ad-library brand --domain gymshark.com --summary
   docker compose exec scraper uv run --no-sync facebook-ad-library diag --slug shaktimats
   ```

   | Outcome | Meaning | Next |
   |---|---|---|
   | `found page 775991435791863 (Shakti Mat); count=~1000` on the first two, `found page 129669023798560 (Gymshark)` on the third, `diag --slug` reads the id from both pages | every page class is served to this address | done; `BRAND_PROFILE_FALLBACK=true` may be set |
   | `--url` is `not found (no page for facebook.com/shaktimats)` while `--page-id` works | the plugin rendered but without the page: read `diag --slug`'s saved page before changing anything | the id-carrying URL forms and stored page ids still work; keep the fallback off |
   | `diag --slug` reports `SessionDead` or `ScrapeBlocked` on the profile page only | that page class is refused to this address | leave `BRAND_PROFILE_FALLBACK=false`; nothing else is affected |
   | `ScrapeBlocked …400` on any of them | the TLS fingerprint was rejected | as for the search |
6. Once the lookups pass, the burst timing that backs `RATE_LIMIT_PER_MIN`: `docker compose exec scraper uv run --no-sync facebook-ad-library diag --page-id 775991435791863 --repeat 20` makes 20 page views on one session at the configured pace; every GET a `200` with `page=known` and no challenge after the first is the pass. Record the numbers in architecture.md, "How a brand lookup is made".

Measured on the VPS after the `scraper-testing` run of 19 Sep 2026 (12 calls from n8n over the Docker network, 0 failures, 170 ads; the full comparison is in architecture.md, [Measured against Apify](architecture.md#measured-against-apify)): 1.4 to 5.8 s per call, one page miss in 14 GETs retried once, `results_missing` 0, 63 MiB resident, 10 PIDs, 11 MB transferred.

## Rotating credentials

`API_TOKEN`: change it in `.env`, recreate the container, then update the n8n Header Auth credential `facebook-scraper` (`5K2ikYegpPkEPGfn`, sending `Authorization: Bearer <token>`, the same shape as `trustpilot-scraper` and `reddit-scraper`) to match. Edit that credential in place; do not rename another one into it. Retrieve tokens in a terminal, not in anything that keeps a transcript: `grep ^API_TOKEN /opt/facebook-ad-library-scraper/.env` on the VPS.

The same token also sits inline in every Code node that calls `/adyntel`, because n8n Code nodes cannot read stored credentials (the Adyntel key sat there the same way). Each of those nodes starts with one line, `const IN_HOUSE_TOKEN = '…'; // mirrors the facebook-scraper credential`, so a rotation is that line in each of them, and nothing else in the node changes. Where they are:

| Workflow | Node | State |
|---|---|---|
| `scraper-testing` | `In-house: Ad Count (page)`, `Collect Video Ads (in-house)` | the harness lanes |
| `01 · Find The Founder` | `Resolve Ads Via Facebook Page`, `Resolve Ads Via Searched Page`, `Resolve Ad Count` | production since 23 Sep 2026 |
| `02 · Learn About The Brand` | `Resolve Ads Via Facebook Page`, `Collect Video Ads` | production since 23 Sep 2026 |

The two HTTP Request nodes (`Adyntel: Ad Count` in 01, `Adyntel: Ad Creative` in 02) use the credential, like the harness's HTTP clones.

## Testing against the pipeline

The production workflows are not edited. The service is exercised in the n8n workflow **`scraper-testing`** (`0q7jtSF7FG0cbyBe`) on the same instance, the way the Trustpilot and Reddit lanes there already do. `fb.md` describes the harness that exists there: a `Start FB Test` trigger, `FB Keyword List` with 12 keyword-country pairs, a disabled clone of Stage 0's Apify node, `Measure FB Response` with Stage 0's extraction verbatim, and `Collect FB Results`. The clone was pointed at this service on 19 Sep 2026 (URL, the `facebook-scraper` credential, no query parameters, enabled; body unchanged); the run's numbers are in architecture.md under [Measured against Apify](architecture.md#measured-against-apify).

Two more lanes, added 22 Sep 2026 for the Adyntel replacement, each with a sticky note carrying its run protocol: **`Start ADY 01 Test`** (`ADY 01 Brand List` → `Loop ADY 01` → `In-house: Ad Count (domain)`, a clone of 01's HTTP node, → `In-house: Ad Count (page)`, the same brand by stored page id or URL → `Measure ADY 01` → `Collect ADY 01`) and **`Start ADY 02 Test`** (`ADY 02 Brand List` → `Loop ADY 02` → `In-house: Ad Creative` → `Analyse ADY 02` and `Collect Video Ads (in-house)`, 02's nodes verbatim but for the loop name and the URL → `Check Clip URL`, a HEAD per clip → `Measure ADY 02` → `Collect ADY 02`). The brand lists are literals built from 01's executions and the two data tables; `ADY 01 Brand List` has a `LIMIT` constant (12 for a canary, 0 for the whole list). Runs 2705, 2713 (lane 01) and 2708, 2719 (lane 02) are the ones in architecture.md, [Measured against Adyntel](architecture.md#measured-against-adyntel). Read a run's single `Collect ADY 0X` item; its `per_brand` array holds the rows.

## The throttle, and the proxy that answers it

**What happened, 24 Sep 2026.** Meta stopped serving the ad payload to the VPS address. The page rendered, the total was correct, `edges` was empty, and there was no error of any kind. Production returned 722 empty results out of 1,320 requests before it was noticed, because every one of them looked like a keyword with no inventory. The mechanics are in [architecture.md](architecture.md#the-throttle); this is what to do about it.

**The setting that fixes it.** One credential, in the recovery slot, not the global one:

```dotenv
SCRAPER_PROXY=                       # leave EMPTY - the direct path stays free
FALLBACK_PROXY=host:port:user:pass   # only refused calls are billed
FB_DOC_ID=24922295957467452          # pin it; discovery costs ~21 MB a mint
```

Setting `SCRAPER_PROXY` instead also works and was the first fix applied, but it proxies **every** request including the ~1 MB rendered page: measured at ~190 MB/day against ~31 MB/day for the split. Use the split.

`.env` is read at container start, so this needs `docker compose up -d`, not a restart of the process inside. Check `/health` is idle (`in_flight: 0`) first, and keep a dated copy of `.env` before editing.

**Verifying it took.** From the VPS, two commands, about ten seconds:

```bash
docker exec facebook-ad-library python -c "import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8002/health')); print(d['proxy'], d['fallback_proxy'])"
# expect: False True   - direct path free, recovery armed

docker compose exec scraper uv run --no-sync facebook-ad-library brand --page-id 775991435791863 --summary
# expect: count=1039 ads=30.   count=1039 ads=0 means the recovery is not working.
```

Then watch one real cycle. The log says plainly what it did:

```
throttled 'full grain leather belt' CA: Meta reports 476 ads and served none; retrying through the proxy
recovered 'full grain leather belt' CA: 81 ad(s) from 9 page(s), 464KB -> reached the 80-ad target
domain search for markhalston.com: recovered 30 ad(s) through the fallback proxy
```

The pass bar is `throttled_recovered == throttled_pages` and `brand_recovered == brand_withheld` on `/health`. A gap means searches are answering `503`.

**What it costs.** Measured on sourcing cycle 3931, 40 keywords: 24 throttled, 24 recovered, 64 pages, 6.62 MB decoded, ~1.3 MB billed wire, 6m30s. Roughly **31 MB/day** at the hourly schedule. The proxy bills the wire and Meta sends this zstd-compressed with no `Content-Length`, so the wire figure cannot be read off the response - 20% of decoded is one measurement from the proxy balance, not a rate card. Check the balance against it.

**Rolling it back.** Empty `FALLBACK_PROXY` and recreate. The service then answers withheld searches `503` instead of recovering them - which is the correct behaviour, not a failure: it costs the caller no retry. Brands stop being qualified until the throttle lifts or the proxy comes back.

**When Meta stops.** A direct page that carries ads clears the memory at once and the service returns to the free path by itself, one probe per `THROTTLE_MEMORY_S`. Nothing needs doing. `/health` `throttle.active` going `false` and staying there is the signal, and `FALLBACK_PROXY` can then be emptied to remove the last of the spend.

## What to watch

| Signal | Meaning |
|---|---|
| `/health` `results_missing` rising | Meta is skipping the prefetch more often than `SSR_RETRIES` covers; raise it and re-check the budget arithmetic in `.env.example` |
| `/health` `retried` a large share of `requests` | the miss rate has moved from the 1-in-4 measured; not a failure while `results_missing` stays at 0 |
| `/health` `blocked` rising | the page is being refused; read the message in the log (challenge, `403`, `400` fingerprint) before changing anything |
| `/health` `rate_limited` rising | a `429` on the page, which has not been seen; lower `RATE_LIMIT_PER_MIN` |
| `/health` `session_dead` rising | sessions are being refused mid-life; check `retired_by_reason` and the session ages, and lower `SESSION_MAX_REQUESTS` |
| `/health` `sessions.challenges` rising faster than `sessions_minted` | Meta is re-challenging live sessions; the service clears it, but it is a change worth noting |
| `/health` `empty` rising across many keywords | either the keywords are bad or Meta is answering empty pages; a real empty carries an empty results blob, so compare with `sessions.misses`. **If `ok` stops moving while `requests` climbs, suspect the throttle** and check `throttled_pages` |
| `/health` `throttled_pages` rising | Meta is withholding the ad payload from this address. Expected while the throttle is on; the number to watch is the one below |
| `/health` `throttled_recovered` **below** `throttled_pages` | the recovery is not working. Check `FALLBACK_PROXY` is set and the balance is not spent; a gap here means searches are answering `503` and brands are not being qualified |
| `/health` `brand_withheld` without `brand_recovered` | the same for brand lookups, and the more damaging of the two: without landing domains 01 cannot verify ownership |
| `/health` `throttle.active` `true` for hours with `direct_gets_skipped` climbing | normal while throttled. If it is `true` and `observations` is 1, the memory may be holding on a single stale observation - it expires after `THROTTLE_MEMORY_S`, so give it that long before acting |
| `/health` `paged_decoded_bytes` climbing faster than expected | this is what the proxy bills, roughly 20% of it on the wire. ~33 KB a keyword at `FALLBACK_MAX_PAGES=8`; four times that at 20 |
| `/health` `adyntel_not_found` a large share of `adyntel_requests` | the resolver, not Meta: read the log lines (`not-found adyntel company_domain=… (no ad among the 30 for … lands on it)`) and check the brands by hand with `brand --domain` |
| `/health` `sessions.plain_blocked` or `plain_dead` rising | the page plugin or the profile page is being refused to this address while the Ad Library page is not; set `BRAND_PROFILE_FALLBACK=false` if it is on, and send `page_id` from the stored table where the workflow can |
| `/health` `budget_exceeded` or `busy` rising | lookups are queueing behind each other or behind the limiter; either the callers overlap more than `MAX_CONCURRENCY` allows or `RATE_LIMIT_PER_MIN` is too low for the burst. `X-Queue-Seconds` on the responses says which |
| `/health` `sessions.sessions_minted` rising steadily | expected at the higher rate: `SESSION_MAX_REQUESTS` (200) retires a session after about 200 GETs; not a fault unless `retired_by_reason` shows `session_dead` or `miss_streak` |
| `docker stats` memory climbing | the caches are the only things that grow; check `cache.entries` and `brand_cache.entries` against `CACHE_MAX_ENTRIES` |

## Cutting the production workflow over

Not applied, and not to be applied without a decision. Recorded so the shape of the change is known. It is limited to one HTTP Request node; keep its name and its connection into `Extract Dedupe And Filter`.

| Node | Change |
|---|---|
| `Apify: Facebook Ad Library` | URL → `http://facebook-ad-library:8002/facebook`; authentication → the `facebook-scraper` Header Auth credential; drop the `maxTotalChargeUsd` query parameter. Body and the 300 s timeout unchanged. Check the node's On Error setting first: this service answers failures with a `503`, and the node must pass that on as an item rather than stop the run, the way the siblings' nodes in `scraper-testing` do with `onError: continueRegularOutput` |

Rollback is the URL and the credential.

The Adyntel cutover in `01 · Find The Founder` and `02 · Learn About The Brand` was applied and published on 23 Sep 2026 (01 version `4a531d73`, 02 version `5fc4124b`, `90 · Watch The Pipeline` version `ed346a7c`; the previous published versions are `7682a9f6`, `d39fa547` and `17863f59` in each workflow's history). Seven call sites; every body keeps its lookup fields, so the node code that reads the answer is untouched, and only the `api_key` and `email` lines went. Every node name is kept: the telemetry fields are keyed on them. The server-side version diff of each workflow was checked before publishing: only the nodes in the table and one added sticky note changed, no connections.

| Workflow / node | Change |
|---|---|
| 01 `Adyntel: Ad Count` (HTTP) | URL → `http://facebook-ad-library:8002/adyntel`; authentication → the `facebook-scraper` Header Auth credential; drop the `Content-Type` header parameter; `api_key` and `email` dropped from the body, `company_domain` kept; 60 s timeout and On Error unchanged. Later, optionally, add `"page_id"` from the stored table so the domain is not resolved at all |
| 01 `Resolve Ads Via Facebook Page`, `Resolve Ads Via Searched Page`, `Resolve Ad Count` (Code) | four edits each: the `IN_HOUSE_TOKEN` const at the top, the URL in the `httpRequest` call, `headers: { Authorization: 'Bearer ' + IN_HOUSE_TOKEN }` in it, the `api_key` and `email` lines deleted. `Resolve Ad Count`'s loop never runs (`is_result_complete` is always `true`) and can stay |
| 02 `Adyntel: Ad Creative` (HTTP) | URL, credential, drop the header parameter and the two identity lines; body keeps `company_domain` and `active_status: "all"` |
| 02 `Resolve Ads Via Facebook Page`, `Collect Video Ads` (Code) | the same four edits; bodies keep `facebook_url` + `active_status: 'all'` and `company_domain` + `media_type: 'video'`. Later, optionally, `page_id` in the video call fixes the known gap for brands that resolved through the page fallback |
| 01 and 02 `Build Run Telemetry` | `ADYNTEL_USD_PER_CALL` → `0`; the `adyntel_*` field names stay so `90 · Watch The Pipeline` and the cost audit keep parsing |
| `90 · Watch The Pipeline` | relabel the Adyntel vendor line as the in-house Ad Library; keep the call-count check |

Rollback is the URL and the auth at each site, with the old lines kept in a sticky note per workflow named `Rollback: Adyntel -> in-house Ad Library` (the key written as a pointer to where it lives, not the literal), or `restore_workflow_version` to the previous published version listed above followed by a publish. Revoke the Adyntel key once the swap has held.

The Brave replacement in `01 · Find The Founder` was applied and published on 23 Sep 2026 (01 version `54a5de58`, previous `4a531d73`). No service change: the domain lookup at `Adyntel: Ad Count` is already the Ad Library page search, so a brand that reaches the old Brave slot has already failed it. Evidence in [architecture.md](architecture.md#measured-against-brave).

| Workflow / node | Change |
|---|---|
| 01 `No Page Found` (Code, new) | takes the false branch of `IF: Has Stored Page?` and emits the `content: NONE` item that `Filter Facebook Candidates` emitted when Brave had no usable result, so `Resolve Ads Via Searched Page` sends the brand to Needs Review without spending a retry |
| 01 `Brave: Page Search`, `Filter Facebook Candidates`, `IF: Have Candidates?`, `Claude: Find Facebook Page` | parked: the inbound edge from `IF: Has Stored Page?` removed; the nodes, their own connections and the `Brave Search` credential left in place, not disabled (a disabled node passes items through) |
| 01 `Build Run Telemetry` | `stored_page_misses` counts `No Page Found` runs; new `page_search_exhausted`; `brave_calls` and `brave_errors` stay and read 0, so `90 · Watch The Pipeline` keeps parsing |

Rollback is the one edge (the sticky note `Rollback: Brave -> in-house Ad Library search` in 01 holds the Brave node's settings and the telemetry lines), or `restore_workflow_version` to `4a531d73` and a publish. Cancel the Brave subscription after seven days of `workflow1_cost` rows with `brave_calls: 0` and Needs Review and qualified counts steady; the parked nodes, the credential and the `brave_*` fields go together in a later cleanup.

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests and manual dispatch:

- **unit-tests**: `uv run pytest -q` on Python 3.11.
- **docker-image**: builds the image, starts it with a token minted for that run only, and checks `/health`, that a missing token is `401`, that a body without `query` is `400`, a live search with Stage 0's verbatim body, and a live brand lookup by page id. A GitHub runner is a datacenter address like the VPS; the page GET is expected to work from it, but a `503` whose `error.type` is one of this service's own (`ScrapeBlocked`, `RateLimited`, `ResultsMissing`, `ScrapeFailed`, `BudgetExceeded`, `Busy`) is still an accepted outcome of those live steps, because Meta decides per request whether the page carries its results. A `200` search must be an array whose items carry `page_name` and `snapshot.caption`; a `200` lookup must be an envelope with an integer `number_of_ads` above zero, never `{}`, because the page id is a real one. Anything else fails the build.
