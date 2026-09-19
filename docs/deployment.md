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
| Proxy | none; `/health` reports `proxy: false`. The page GET this service makes is served to the VPS's own address without a throttle ([address-classification.md](address-classification.md)) |

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

## The check that decides whether the address works

Run this from the VPS before anything is pointed at the service:

```bash
docker compose exec scraper uv run --no-sync facebook-ad-library diag --query "running shoes" --repeat 3
```

| Outcome | Meaning | Next |
|---|---|---|
| `GET 1: ads, 30 ads, …` on most of the three | the address is fine | run the volume check below |
| every GET `miss` | Meta is skipping the server-side prefetch for this address right now; the fix, if it persists, is a higher `SSR_RETRIES`, not a proxy | re-run in a few minutes; watch `results_missing` on `/health` |
| `GET 1: ScrapeBlocked …400` | the TLS fingerprint was rejected | check `FB_IMPERSONATE` is a current Chrome profile for the installed `curl_cffi` |
| `GET 1: ScrapeBlocked …403 without a challenge` | the address itself is refused the page, which no address has been so far | that would be new; save the page with `--save-dir` and read it before changing anything |

Then the volume check, twenty searches on one session pair at the production pace:

```bash
docker compose exec scraper uv run --no-sync facebook-ad-library search \
  "acupressure mat for back pain" "running shoes" "pickleball paddle" "dandruff shampoo" "minimalist belt" \
  "podcast recording headphones over ear" "on camera monitor field" "lavalier microphone wireless clip" \
  "camera backpack photography travel" "rc crawler bead lock wheel" \
  --country US --max 80 --repeat 2 --summary
```

Expect `failed: 0`, up to 30 ads on the productive keywords, `0` with `misses: 0` on the exhausted one, and `misses` of about a quarter of the GETs. Measured on the VPS on 19 Sep 2026 before this design was built: 41 consecutive page GETs from the VPS address, all `200`, no challenge after the first, no throttle. The numbers from the deployed service are recorded in the section below.

## Measured on the VPS

Deployed 19 Sep 2026 at commit `024d4c2`. The deploy check from the VPS: three GETs of `running shoes`, 30 ads and 16 advertisers each, one challenge, no misses, 1.1 MB pages. The `scraper-testing` run the same day ([n8n-test-2026-09-19.md](n8n-test-2026-09-19.md)): 12 calls from n8n over the Docker network, 0 failures, 170 ads, 1.4 to 5.8 s per call (45 s twice while the 4-a-minute limiter held the harness's burst), one page miss in 14 GETs retried once, `results_missing` 0. Container after the run: 63 MiB resident, 10 PIDs, 11 MB transferred for 14 GETs.

## Update, rollback, logs

```bash
cd /opt/facebook-ad-library-scraper
git pull origin main && docker compose up -d --build        # deploy main
git checkout <sha> && docker compose up -d --build           # roll back to a known commit
docker compose logs -f --tail 100                            # follow the service log
```

`.env` is read at container start (`env_file`), so a changed value needs `docker compose up -d`, not a restart of the process inside. Keep a dated copy before editing it. Check `/health` is idle (`in_flight: 0`) before recreating.

The container has no `curl`. Read health from inside it with:

```bash
docker exec facebook-ad-library python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8002/health').read().decode())"
```

## Verifying a deploy

1. Run a real search from the server:
   ```bash
   docker compose exec scraper uv run --no-sync facebook-ad-library search "running shoes" --country US --max 30 --summary
   ```
   Expect up to 30 ads from 10 or more advertisers in a few seconds; a second GET with `misses: 1` now and then is normal.
2. `/health` should show `auth: true`, `max_concurrency: 2`, and after the first search `sessions.challenges: 1` and `sessions.calls` equal to the GETs made.
3. Each successful call logs one line: `ok 'running shoes' US ads=30 attempts=1 misses=0 swaps=0 4.1s`. A retried one logs `page without results (1 of 3), retrying` before it.

## Rotating credentials

`API_TOKEN`: change it in `.env`, recreate the container, then update the n8n Header Auth credential `facebook-scraper` (`5K2ikYegpPkEPGfn`, sending `Authorization: Bearer <token>`, the same shape as `trustpilot-scraper` and `reddit-scraper`) to match. Edit that credential in place; do not rename another one into it. Retrieve tokens in a terminal, not in anything that keeps a transcript: `grep ^API_TOKEN /opt/facebook-ad-library-scraper/.env` on the VPS.

## Testing against the pipeline

The production workflow is not edited. The service is exercised in the n8n workflow **`scraper-testing`** (`0q7jtSF7FG0cbyBe`) on the same instance, the way the Trustpilot and Reddit lanes there already do. `fb.md` describes the harness that exists there: a `Start FB Test` trigger, `FB Keyword List` with 12 keyword-country pairs, a disabled clone of Stage 0's Apify node, `Measure FB Response` with Stage 0's extraction verbatim, and `Collect FB Results`. The clone was pointed at this service on 19 Sep 2026 (URL, the `facebook-scraper` credential, no query parameters, enabled; body unchanged) and the run is reported in [n8n-test-2026-09-19.md](n8n-test-2026-09-19.md).

## What to watch

| Signal | Meaning |
|---|---|
| `/health` `results_missing` rising | Meta is skipping the prefetch more often than `SSR_RETRIES` covers; raise it and re-check the budget arithmetic in `.env.example` |
| `/health` `retried` a large share of `requests` | the miss rate has moved from the 1-in-4 measured; not a failure while `results_missing` stays at 0 |
| `/health` `blocked` rising | the page is being refused; read the message in the log (challenge, `403`, `400` fingerprint) before changing anything |
| `/health` `rate_limited` rising | a `429` on the page, which has not been seen; lower `RATE_LIMIT_PER_MIN` |
| `/health` `session_dead` rising | sessions are being refused mid-life; check `retired_by_reason` and the session ages, and lower `SESSION_MAX_REQUESTS` |
| `/health` `sessions.challenges` rising faster than `sessions_minted` | Meta is re-challenging live sessions; the service clears it, but it is a change worth noting |
| `/health` `empty` rising across many keywords | either the keywords are bad or Meta is answering empty pages; a real empty carries an empty results blob, so compare with `sessions.misses` |
| `docker stats` memory climbing | the cache is the only thing that grows; check `cache.entries` against `CACHE_MAX_ENTRIES` |

## Cutting the production workflow over

Not applied, and not to be applied without a decision. Recorded so the shape of the change is known. It is limited to one HTTP Request node; keep its name and its connection into `Extract Dedupe And Filter`.

| Node | Change |
|---|---|
| `Apify: Facebook Ad Library` | URL → `http://facebook-ad-library:8002/facebook`; authentication → the `facebook-scraper` Header Auth credential; drop the `maxTotalChargeUsd` query parameter. Body and the 300 s timeout unchanged. Check the node's On Error setting first: this service answers failures with a `503`, and the node must pass that on as an item rather than stop the run, the way the siblings' nodes in `scraper-testing` do with `onError: continueRegularOutput` |

Rollback is the URL and the credential.

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests and manual dispatch:

- **unit-tests**: `uv run pytest -q` on Python 3.11.
- **docker-image**: builds the image, starts it with a token minted for that run only, and checks `/health`, that a missing token is `401`, that a body without `query` is `400`, and a live search with Stage 0's verbatim body. A GitHub runner is a datacenter address like the VPS; the page GET is expected to work from it, but a `503` whose `error.type` is one of this service's own (`ScrapeBlocked`, `RateLimited`, `ResultsMissing`, `ScrapeFailed`) is still an accepted outcome of that last step, because Meta decides per request whether the page carries its results. A `200` must be an array whose items carry `page_name` and `snapshot.caption`. Anything else fails the build.
