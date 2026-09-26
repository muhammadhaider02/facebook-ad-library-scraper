# Deployment

## Where it runs

The same topology the two siblings run in.

| | |
|---|---|
| Host | a Docker host that also runs n8n |
| Checkout | `~/facebook-ad-library-sourcing`, a clone of `main` |
| Container | `facebook-ad-library-sourcing`, built from the `Dockerfile` by `docker-compose.sourcing.yml` (Compose project and image tag pinned to the same name) |
| Env file | `.env` |
| Network | `n8n_default`, the network n8n's own Compose project created; declared external so this file never owns it |
| Address from n8n | `http://facebook-ad-library-sourcing:8003/facebook`; the old name `facebook-ad-library-lanes` still resolves as a network alias |
| Beside it | `trustpilot-reviews` on `8000` and `reddit-reviews` on `8001`, deployed the same way |
| Proxy | required: every lane is one sticky residential exit (`LANE_PROXY_TEMPLATE` + `LANE_PROXY_PORTS`, or `LANE_PROXIES`); the host's own address is never used. `/health` reports `proxy: true` when every lane has one |

Nothing is published to the host. Docker publishes straight past UFW, so even `8003:8003` behind the firewall would be public; the service is reachable only from containers on the network, and `API_TOKEN` still applies so a compromised container cannot drive it freely. If the host already runs a reverse proxy on 80/443 for n8n, leave it alone: this service needs no domain, certificate or route of its own.

Every `docker compose` command below runs in the checkout and names the file. To type it once per shell: `export COMPOSE_FILE=docker-compose.sourcing.yml`, then drop the `-f`.

## What the compose file sets, and why

| Setting | Value | Reason |
|---|---|---|
| `mem_limit` / `memswap_limit` | 3g / 3g | a Python process with no browser, one page of up to 1.7 MB in hand per lane while a search is parsed; the headroom is for a deep job's answer, which is serialised in one piece (768m was OOM-killed on one). With `MALLOC_ARENA_MAX=2` in the env file, freed memory goes back to the pool |
| `mem_reservation` | 128m | soft limit, enforced only when the host is short of memory |
| `init`, `pids_limit` | true / 128 | a fork guard, not a budget: eight lane workers, the fetch pool and uvicorn stay well under it. No `shm_size`: that exists in the siblings for Chromium and this image has no browser |
| `stop_grace_period` | 250s | `SCRAPE_BUDGET_S` plus slack, so a redeploy never kills an in-flight search |
| logging | json-file, 20m × 3 | one line per search plus one per session and lane event; bounded anyway |

`LANE_COUNT` is the concurrency: one request in flight per lane. `RATE_LIMIT_PER_MIN` is per lane, so it shapes the traffic Meta sees from each exit. Ramp `LANE_COUNT` in steps (1, 2, 4, 8), stepping up only while `/health` `block_rate_1h` stays under 5 %.

## First deploy

```bash
git clone https://github.com/muhammadhaider02/facebook-ad-library-scraper.git ~/facebook-ad-library-sourcing
cd ~/facebook-ad-library-sourcing
cp .env.example .env             # set API_TOKEN, LANE_COUNT, LANE_PROXY_TEMPLATE, LANE_PROXY_PORTS, FB_DOC_ID; raise JOB_MAX_ITEMS and JOB_ITEM_MAX_WAIT_S for large jobs
docker compose -f docker-compose.sourcing.yml up -d --build   # a few seconds: no browser to download
```

If the network name is wrong the container refuses to start with `network n8n_default declared as external, but could not be found`; `docker network ls` gives the real one. A lane without a proxy refuses to start while `LANE_REQUIRE_PROXY` is `true`, which is the default and the right setting everywhere but CI. Keep `SESSION_MAX_AGE_S` at or below the proxy's sticky rotation interval (7200 s fits a 120 min interval), so a cookie jar and its exit live and die together.

**Before any Facebook traffic, prove the ports are sticky.** From the host, with the proxy credential: each port should keep one address across three checks 15 s apart, and differ from the others.

```bash
for port in 11510 11511; do for i in 1 2 3; do curl -s -x "http://USER:PASS@gw.dataimpulse.com:$port" https://api.ipify.org; echo " ($port)"; sleep 15; done; done
```

## Update, rollback, logs

```bash
cd ~/facebook-ad-library-sourcing
git pull origin main && docker compose -f docker-compose.sourcing.yml up -d --build   # deploy main
git checkout <sha> && docker compose -f docker-compose.sourcing.yml up -d --build      # roll back to a known commit
docker compose -f docker-compose.sourcing.yml logs -f --tail 100                       # follow the service log
```

`.env` is read at container start (`env_file`), so a changed value needs `up -d`, not a restart of the process inside. Keep a dated copy before editing it. Check `/health` is idle (`in_flight: 0`, `jobs.running: 0`) before recreating: jobs live in memory, and a redeploy mid-job loses it (the caller sees a `404`).

After an upgrade, compare `.env` with `.env.example` and copy in any new variables; a value left out takes the default. Retired variables are ignored.

The container has no `curl`. Read health from inside it with:

```bash
docker exec facebook-ad-library-sourcing python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8003/health').read().decode())"
```

## Verifying a deploy

The CLI runs inside the container on the configured lanes (lane 0 unless `--lane` says otherwise).

1. The deploy check, before anything is pointed at the service:
   ```bash
   docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library diag --lane 0 --lane-ip --repeat 3
   docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library diag --query "running shoes" --repeat 3
   ```

   | Outcome | Meaning | Next |
   |---|---|---|
   | the same exit IP three times, then `GET 1: ads, 30 ads, …` on most of the three | the exit is sticky and served | step 2 |
   | `diag --lane-ip` exits `5` | the port is not sticky: the address moved between checks | fix the proxy's sticky setting before anything else |
   | every GET `miss` | Meta is skipping the server-side prefetch for this exit right now; the fix, if it persists, is a higher `SSR_RETRIES` | re-run in a few minutes; watch `results_missing` on `/health` |
   | `GET 1: ScrapeBlocked …400` | the TLS fingerprint was rejected | check `FB_IMPERSONATE` is a current Chrome profile for the installed `curl_cffi` |
   | `GET 1: ScrapeBlocked …403 without a challenge` | this exit is refused the page | try another lane with `--lane`; save the page with `--save-dir` and read it before changing anything |

   Measured on a first deploy: 30 ads and 16 advertisers on each of the three GETs, one challenge, no misses, 1.1 MB pages.
2. A real search:
   ```bash
   docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library search "running shoes" --country US --max 30 --summary
   ```
   Expect up to 30 ads from 10 or more advertisers in a few seconds; a second GET with `misses: 1` now and then is normal.
3. `/health` should show `auth: true`, `proxy: true`, `max_concurrency` equal to `LANE_COUNT`, every lane `up` with an `exit_ip`, no two lanes on the same exit, and after the first search `sessions.challenges: 1`.
4. Each call logs one line: `ok 'running shoes' US ads=30 total=… lane=lane-1 tries=1 attempts=1 misses=0 4.1s`. A retried one logs `page without results (1 of 3), retrying` before it; a lane that skipped its rendered GET adds `(direct GET skipped: throttled)`.
5. The brand lookup, the three ways it resolves. The page view is the same page class as the search; the plugin and the profile page are two more page classes, so this is where a refusal of either would show:
   ```bash
   docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library brand --page-id 775991435791863 --summary
   docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library brand --url https://www.facebook.com/shaktimats --summary
   docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library brand --domain gymshark.com --summary
   docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library diag --slug shaktimats
   ```

   | Outcome | Meaning | Next |
   |---|---|---|
   | `found page 775991435791863 (Shakti Mat); count=~1000` on the first two, `found page 129669023798560 (Gymshark)` on the third, `diag --slug` reads the id from both pages | every page class is served to this exit | done; `BRAND_PROFILE_FALLBACK=true` may be set |
   | `--url` is `not found (no page for facebook.com/shaktimats)` while `--page-id` works | the plugin rendered but without the page: read `diag --slug`'s saved page before changing anything | the id-carrying URL forms and stored page ids still work; keep the fallback off |
   | `diag --slug` reports `SessionDead` or `ScrapeBlocked` on the profile page only | that page class is refused to this exit | leave `BRAND_PROFILE_FALLBACK=false`; nothing else is affected |
   | `ScrapeBlocked …400` on any of them | the TLS fingerprint was rejected | as for the search |
6. Once the lookups pass, the burst timing that backs `RATE_LIMIT_PER_MIN`: `docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library diag --page-id 775991435791863 --repeat 20` makes 20 page views on one session at the configured pace; every GET a `200` with `page=known` and no challenge after the first is the pass.

Measured over the Docker network from n8n, 12 calls: 0 failures, 1.4 to 5.8 s per call, one page miss in 14 GETs retried once, `results_missing` 0, 63 MiB resident, 10 PIDs.

## Pointing an n8n workflow at it

For the Apify actor, in the caller's HTTP Request node:

- URL → `http://facebook-ad-library-sourcing:8003/facebook`.
- Authentication → a Header Auth credential sending `Authorization: Bearer <API_TOKEN>`.
- Drop Apify's `maxTotalChargeUsd` query parameter. The body stays unchanged, and so can the 300 s timeout.
- Set On Error to continue (`continueRegularOutput`), so a `503` arrives as an item the workflow can route instead of stopping the run.

For Adyntel, the same URL change to `/adyntel` and the same credential; drop `api_key` and `email` from the body (they are ignored anyway) and keep the lookup fields (`company_domain`, `facebook_url`, `active_status`, `media_type`), so the code that reads the answer is untouched. Add `page_id` where the workflow already stores it: the lookup then skips resolution.

A Code node that calls the service with `this.helpers.httpRequest` must carry the token itself, because Code nodes cannot read stored credentials: `headers: { Authorization: 'Bearer ' + TOKEN }`. Keep the token in one constant at the top of each such node.

**Rotating the token.** Change `API_TOKEN` in `.env`, recreate the container, then update the Header Auth credential in place and the constant in every Code node that calls the service. Retrieve tokens in a terminal, not in anything that keeps a transcript.

## The throttle, and how lanes answer it

**What it looks like.** Meta stops serving the ad payload to an address. The page renders, the total is correct, `edges` is empty, and there is no error of any kind, so every search looks like a keyword with no inventory. The mechanics are in [architecture.md](architecture.md#the-throttle).

**What answers it.** The lane proxy is the fix: every request leaves through a residential exit, and a refused request moves to another lane (`LANE_MAX_TRIES`). When a rendered page on a lane reports ads and carries none, the same lane pages the search over GraphQL through the same exit (at most `FALLBACK_MAX_PAGES`, 8) and answers with what it recovers. The lane remembers the withholding for `THROTTLE_MEMORY_S` and goes to GraphQL first on its later searches (`X-Direct-Skipped: 1`); one rendered GET per window checks whether Meta has stopped. `LANE_WITHHELD_ROTATE` withheld pages in a row move the lane to a reserve port, a fresh exit. What no lane recovers answers `503` (`kind: blocked`), never an empty `200`.

Pin the GraphQL query id, so a mint does not pay for discovery:

```dotenv
FB_DOC_ID=24922295957467452   # discovery costs ~21 MB a mint; pinned, ~0.9 MB
```

**Verifying it.** From the host, about ten seconds:

```bash
docker exec facebook-ad-library-sourcing python -c "import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8003/health')); print(d['proxy'], d['lanes_summary'], d['throttle'])"
# expect: True {'total': N, 'up': N, ...} {'active': ..., 'lanes_throttled': ...}

docker compose -f docker-compose.sourcing.yml exec scraper uv run --no-sync facebook-ad-library brand --page-id 775991435791863 --summary
# expect: count=~1000 ads=30.   A count with ads=0 means the recovery is not working on that lane.
```

The pass bar is `throttled_recovered == throttled_pages` and `brand_recovered == brand_withheld` on `/health`. A gap means searches are answering `503`.

**What it costs.** Measured on one throttled run of 40 keywords: 24 throttled, 24 recovered, 64 GraphQL pages, 6.62 MB decoded, ~1.3 MB billed wire, 6 min 30 s. The proxy bills the wire and Meta sends this zstd-compressed with no `Content-Length`, so the wire figure cannot be read off the response: 20% of decoded is one measurement, not a rate card. Check it against the proxy account.

**When Meta stops.** A rendered page that carries ads clears the lane's memory at once and the lane returns to the rendered GET by itself. `/health` `throttle.active` going `false` and staying there is the signal. Nothing needs doing.

## What to watch

| Signal | Meaning |
|---|---|
| `/health` `block_rate_1h` rising | lanes are being refused more often; hold `LANE_COUNT` where it is (step up only while this stays under 5 %) and read `lanes[].last_error` |
| `/health` `lanes_summary.blocked` or `cooling` above zero for long | exits are being refused or erroring; check `ports.reserve` is not empty, `ip_changes` and `rotations` per lane, and the proxy account |
| two lanes with the same `exit_ip` | the proxy gave two ports one address; the service moves one to a reserve port and logs `share exit`. Persisting means the ports are not sticky |
| `/health` `results_missing` rising | Meta is skipping the prefetch more often than `SSR_RETRIES` covers; raise it and re-check the budget arithmetic in `.env.example` |
| `/health` `retried` a large share of `requests` | the miss rate has moved from the 1-in-4 measured; not a failure while `results_missing` stays at 0 |
| `/health` `blocked` rising | the page is being refused; read the message in the log (challenge, `403`, `400` fingerprint) before changing anything |
| `/health` `rate_limited` rising | `429`s on the page; lower `RATE_LIMIT_PER_MIN` |
| `/health` `sessions.session_dead` rising | sessions are being refused mid-life; check `retired_by_reason` and lower `SESSION_MAX_REQUESTS` |
| `/health` `sessions.challenges` rising faster than `sessions_minted` | Meta is re-challenging live sessions; the service clears it, but it is a change worth noting |
| `/health` `empty` rising across many keywords | either the keywords are bad or Meta is answering empty pages; a real empty carries an empty results blob, so compare with `sessions.misses`. **If `ok` stops moving while `requests` climbs, suspect the throttle** and check `throttled_pages` |
| `/health` `throttled_pages` rising | Meta is withholding the ad payload from some exits. Expected while the throttle is on; the number to watch is the one below |
| `/health` `throttled_recovered` **below** `throttled_pages` | the recovery is not working: check `mint_breaker`, `doc_id_stale` and the proxy account; a gap here means searches are answering `503` |
| `/health` `brand_withheld` without `brand_recovered` | the same for brand lookups, and the more damaging of the two: without landing domains a caller cannot verify ownership |
| `/health` `mint_breaker.open` `true` | GraphQL session mints are failing in a row on a lane (`MINT_FAILURE_LIMIT`); no more are minted until `MINT_COOLDOWN_S` passes |
| `/health` `doc_id_stale` `true` | Meta changed the GraphQL schema; set a current `FB_DOC_ID` |
| `/health` `lanes[].decoded_bytes` or `paged_decoded_bytes` climbing faster than expected | this is what the proxy bills, roughly 20% of it on the wire |
| `/health` `adyntel_not_found` a large share of `adyntel_requests` | the resolver, not Meta: read the log lines (`not-found adyntel company_domain=… (no ad among the 30 for … lands on it)`) and check the brands by hand with `brand --domain` |
| `/health` `sessions.plain_blocked` or `plain_dead` rising | the page plugin or the profile page is being refused while the Ad Library page is not; set `BRAND_PROFILE_FALLBACK=false` if it is on, and send `page_id` where the caller can |
| `/health` `budget_exceeded` or `busy` rising | requests are queueing behind each other or behind the limiter; either the callers overlap more than `LANE_COUNT` allows or `RATE_LIMIT_PER_MIN` is too low for the burst. `X-Queue-Seconds` on the responses says which |
| `/health` `sessions.sessions_minted` rising steadily | expected: `SESSION_MAX_REQUESTS` (200) and `SESSION_MAX_AGE_S` retire sessions; not a fault unless `retired_by_reason` shows `session_dead` or `miss_streak` |
| `docker stats` memory climbing | the caches and finished jobs are what grow: check `cache.entries`, `brand_cache.entries` (`expired_dropped` should rise) and `jobs.store`; finished jobs live `JOB_TTL_S` (900 s). The compose limit is 3g with `MALLOC_ARENA_MAX=2` |

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests and manual dispatch:

- **unit-tests**: `uv run pytest -q` on Python 3.11.
- **docker-image**: builds the image and starts it with one proxy-less lane (`LANE_REQUIRE_PROXY=false`, the only place that setting is right) and a token minted for that run only, then checks `/health`, that a missing token is `401`, that a body without `query` is `400`, a live search with the Apify-shaped body, and a live brand lookup by page id. A GitHub runner is a datacenter address, so a `503` whose `error.type` is one of this service's own (`ScrapeBlocked`, `RateLimited`, `ResultsMissing`, `ScrapeFailed`, `BudgetExceeded`, `Busy`) is an accepted outcome of those live steps. A `200` search must be an array whose items carry `page_name` and `snapshot.caption`; a `200` lookup must be an envelope with an integer `number_of_ads` above zero, never `{}`, because the page id is a real one. Anything else fails the build.
