# Deployment

## Where it runs

Not deployed yet. This is the topology it is built for, the same one the two siblings run in.

| | |
|---|---|
| Host | the Hostinger VPS that runs the self-hosted n8n at `n8n.srv1980669.hstgr.cloud` |
| Checkout | `/opt/facebook-ad-library-scraper`, a clone of `main`, following the siblings' convention |
| Container | `facebook-ad-library`, built from the `Dockerfile` by `docker-compose.yml` |
| Network | `n8n_default`, the network n8n's own Compose project created; declared external so this file never owns it |
| Address from n8n | `http://facebook-ad-library:8002/facebook` |
| Beside it | `trustpilot-reviews` on `8000` and `reddit-reviews` on `8001`, deployed the same way |
| Proxy | none configured; `/health` reports `proxy: false`. Whether the VPS needs one is the open question below |

Nothing is published to the host. Docker publishes straight past UFW, so even `8002:8002` behind the firewall would be public; the service is reachable only from containers on the network, and `API_TOKEN` still applies so a compromised container cannot drive it freely. The VPS already runs Traefik on 80/443 for n8n. There is no domain, no certificate and no reverse proxy for this service, and adding one would fail to bind.

## What the compose file sets, and why

| Setting | Value | Reason |
|---|---|---|
| `mem_limit` / `memswap_limit` | 512m / 512m | a Python process with no browser: 57 MiB resident and 9 PIDs measured while serving a search. The limit is a blast-radius guard for the host; hitting it means something leaked |
| `mem_reservation` | 128m | soft limit, enforced only when the host is short of memory |
| `stop_grace_period` | 250s | `SCRAPE_BUDGET_S` plus slack, so a redeploy never kills an in-flight search |
| logging | json-file, 10m × 3 | one line per search plus one per session mint; bounded anyway |
| `init`, `pids_limit`, `shm_size` | not set | those exist in the siblings for Chromium's process trees and shared memory; this image has no browser |

`RATE_LIMIT_PER_MIN` is what shapes the traffic Meta sees from the address; `MAX_CONCURRENCY` only bounds searches in flight. Raise the page cap and the limiter together, never one alone.

## First deploy

```bash
git clone https://github.com/haider-ecombench/facebook-ad-library-scraper.git /opt/facebook-ad-library-scraper
cd /opt/facebook-ad-library-scraper
cp .env.example .env          # set API_TOKEN
docker compose up -d --build  # a few seconds: no browser to download
```

If the network name is wrong the container refuses to start with `network n8n_default declared as external, but could not be found`; `docker network ls` gives the real one.

## The check that decides whether the address works

Everything measured so far came from a residential laptop IP, where every search succeeded. The first CI run (19 Sep 2026, a GitHub datacenter runner) showed what a datacenter address gets: the bootstrap went through, the challenge cleared, a session minted with every token and the `doc_id`, and the very first GraphQL call was answered with error `1675004` (rate limited); so was the retry after 60 s, and so was a fresh session. Meta throttles that class of address on this endpoint from call one. The Hostinger VPS is a datacenter address too, and until it is tested it should be assumed to behave the same way.

Run this from the VPS before anything is pointed at the service:

```bash
docker compose exec scraper uv run --no-sync facebook-ad-library diag --search --pages 3
```

| Outcome | Meaning | Next |
|---|---|---|
| three pages of ads | the address is fine at this pace | run the volume check below |
| `page 1: RateLimited …1675004` | the address is throttled from call one, as the CI runner was | set `SCRAPER_PROXY` to a residential sticky port (DataImpulse `gw.dataimpulse.com:<10000-20000>:<user>:<pass>`, or Decodo `us.decodo.com:<10001-10099>:<user>:<pass>`), `docker compose up -d`, re-run. Rotating ports (823, 7000) are refused at startup. At Stage 0's volume the whole path through residential is about 13 GB a month, about $13 |
| `BOOTSTRAP FAILED: ScrapeBlocked …400` | the TLS fingerprint was rejected | check `FB_IMPERSONATE` is a current Chrome profile for the installed `curl_cffi` |
| `BOOTSTRAP FAILED: DocIdStale` | the bundles no longer carry the module the regex looks for | capture `doc_id` from DevTools (Network, filter `graphql`, form field `doc_id`) into `FB_DOC_ID`, then fix `DOC_ID_RE` |

Then the volume check, `facebook.md` §10 B: fifty searches on one session pair at the production pace, then twenty more after a restart.

```bash
docker compose exec scraper uv run --no-sync facebook-ad-library search \
  "acupressure mat for back pain" "running shoes" "pickleball paddle" "dandruff shampoo" "minimalist belt" \
  --country NZ --max 80 --repeat 2 --summary
```

Record any `1675004` and the `calls` count it fired at. A clean run after the restart means the limit is per session and the pool design is enough; an immediate throttle on a fresh session means it is per address, and the proxy is the primary path, not the fallback. Put the numbers in this file.

## Update, rollback, logs

```bash
cd /opt/facebook-ad-library-scraper
git pull origin main && docker compose up -d --build        # deploy main
git checkout <sha> && docker compose up -d --build           # roll back to a known commit
docker compose logs -f --tail 100                            # follow the service log
```

`.env` is read at container start (`env_file`), so a changed value needs `docker compose up -d`, not a restart of the process inside. Keep a dated copy before editing it.

The container has no `curl`. Read health from inside it with:

```bash
docker exec facebook-ad-library python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8002/health').read().decode())"
```

## Verifying a deploy

1. Run a real search from the server:
   ```bash
   docker compose exec scraper uv run --no-sync facebook-ad-library search "running shoes" --country US --max 30 --summary
   ```
   Expect about 30 ads from 10 or more advertisers in 15 to 60 s. A `RateLimited` on the first call is the address, not the code; see above.
2. `/health` should show `auth: true`, `max_concurrency: 2`, and after the first search `doc_id_source: "discovered"` with a 17-digit `doc_id`.
3. Each successful call logs one line: `ok 'running shoes' US ads=30 pages=3 swaps=0 22.4s`. Each new session logs its bootstrap trace: `GET 403 481B rd=1; POST challenge 200 cookies=rd_challenge; GET 200 …B cookies=datr,rd_challenge; tokens …; doc_id … (discovered)`.

## Rotating credentials

`API_TOKEN`: change it in `.env`, recreate the container, then update the n8n Header Auth credential to match. The credential does not exist yet; create it as `facebook-scraper`, a Header Auth credential sending `Authorization: Bearer <token>`, the same shape as `trustpilot-scraper` and `reddit-scraper`. Retrieve tokens in a terminal, not in anything that keeps a transcript.

## Testing against the pipeline

The production workflow is not edited. The plan is to exercise the service in the n8n workflow **`scraper-testing`** (`0q7jtSF7FG0cbyBe`) on the same instance, the way the Trustpilot and Reddit lanes there already do. That lane does not exist yet; it needs the deploy above and the `facebook-scraper` credential first. When built it should hold:

| Node | What it does |
|---|---|
| `FB Health` | `GET http://facebook-ad-library:8002/health` over the network, which is the only place that address resolves |
| `FB Search` | one call with Stage 0's body verbatim (`acupressure mat for back pain`, `NZ`, `maxItems` 80), 300 s timeout, `onError: continueRegularOutput` and `alwaysOutputData` so an error body reaches the next node, the `facebook-scraper` credential |
| `Two Pairs Parallel` → `FB Search Batch` | two pairs as one batch with no interval, which is what happens when one hourly run overlaps the next; this is the `MAX_CONCURRENCY` and limiter test |
| `Extract Dedupe And Filter` | a verbatim copy of Stage 0's node, once exported from the n8n UI, to prove it runs unchanged on this output |

## What to watch

| Signal | Meaning |
|---|---|
| `/health` `rate_limited` rising | Meta is throttling the address or a session; `sessions.retired_by_reason.rate_limited` says which sessions paid. Lower `RATE_LIMIT_PER_MIN` before adding a proxy |
| `/health` `blocked` rising | the bootstrap is being refused; read the message in the log (challenge, `403`, `400` fingerprint, no `lsd`) before changing anything |
| `/health` `docid_stale` rising | Meta shipped a build that moved or renamed the persisted query; `diag` and DevTools, then `FB_DOC_ID` |
| `/health` `session_dead` rising | sessions are being refused mid-life; check `retired_by_reason` and the session ages, and lower `SESSION_MAX_REQUESTS` |
| `/health` `truncated` or `partial` rising | the budget or the sessions are biting; a `200` hides both |
| `/health` `empty` rising across many keywords | either the keywords are bad or a soft block is returning empty pages; `EMPTY_STREAK_RETIRE` retires the session after five in a row |
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
- **docker-image**: builds the image, starts it with a token minted for that run only, and checks `/health`, that a missing token is `401`, that a body without `query` is `400`, and a live search with Stage 0's verbatim body. Because a GitHub runner is a datacenter address, a `503` whose `error.type` is one of this service's own (`ScrapeBlocked`, `RateLimited`, `DocIdStale`, `ScrapeFailed`) is an accepted outcome of that last step; a `200` must be an array whose items carry `page_name` and `snapshot.caption`. Anything else fails the build. The first run on 19 Sep 2026 passed with the `503 RateLimited` outcome described above.
