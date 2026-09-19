<div align="center">

# Facebook Ad Library

**SEARCH. COLLATE. SERVE.**

[![CI](https://github.com/haider-ecombench/facebook-ad-library-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/haider-ecombench/facebook-ad-library-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![curl_cffi](https://img.shields.io/badge/HTTP-curl__cffi-orange)](https://github.com/lexiforest/curl_cffi)

Self-hosted Meta Ad Library keyword search with an HTTP API.

</div>

---

## Platform

This repo is a standalone ad-collection service, the third sibling of `trustpilot-reviews` and `reddit-reviews`. Given a keyword and a country it returns the active ads the Meta Ad Library shows for that search, with the advertiser's page, category, like count and the domain each ad points at. It serves the result over a small authenticated HTTP API in the same shape the Apify actor it replaces returned, so the workflow that called Apify calls this instead.

**No browser.** The Ad Library is a logged-out public site whose React frontend loads ads through `POST /api/graphql/` with the persisted query `AdLibrarySearchPaginationQuery`. This service replays that call with plain HTTP and Chrome TLS impersonation (`curl_cffi`), which is what every working scraper of the site does. Measured 2026-09-19: the site's only gate is a one-shot `__rd_verify` challenge on the first request, cleared with one POST, after which the search page hands out the session tokens. Chrome impersonation is load-bearing, not cosmetic: a plain curl clears the challenge and is then answered with a 400 error page on every request.

**No captured ids.** The persisted-query `doc_id` changes when Meta ships a new frontend build. Rather than copying it from DevTools, a session reads it out of the page's own JS bundles when it is minted (about 8 MB, once per session, not per search). `FB_DOC_ID` overrides it if that ever needs pinning.

**What a page is.** One GraphQL call returns ~10 *collated* results (an ad and its variants count once) regardless of the `first: 30` the frontend asks for, measured on every search so far. `MAX_PAGES=3` therefore yields ~30 ads per pair, from ~10–13 advertisers; Stage 0's `maxItems: 80` would need 8 calls. Measured on a real keyword, 30 ads from 3 calls in 22 s covered 13 advertisers, and later pages mostly repeat advertisers already seen. Raise `MAX_PAGES` and `RATE_LIMIT_PER_MIN` together if more depth is wanted; the budget arithmetic in `.env.example` explains the cost.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Runs as a single FastAPI process.

```bash
git clone https://github.com/haider-ecombench/facebook-ad-library-scraper.git
cd facebook-ad-library-scraper

uv sync                       # dependencies into a uv-managed venv

cp .env.example .env          # then set API_TOKEN (see Configuration)

uv run facebook-ad-library serve                   # HTTP service on :8002
```

Verify with `curl localhost:8002/health` (expects `"status":"ok"`); interactive API docs are at `/docs`.

The service is driven over its HTTP API: `POST /facebook` takes the Apify body Stage 0 already sends (`maxItems`, `query`, `country`; the rest is ignored) with `Authorization: Bearer <API_TOKEN>` and returns a JSON array of ads, while `GET /health` is unauthenticated and returns counters. A `200` may be a partial result: past `SCRAPE_BUDGET_S` the search returns what it has with `X-Truncated: true`; a session failure after a good page returns what it has with `X-Pages-Failed: 1`. Errors are `400` (bad request), `401` (token), `503` (blocked, rate limited, session refused twice, or a stale doc_id), all as `{"error": {"type", "status", "message"}}`.

One-off searches from the command line, no server needed:

```bash
uv run facebook-ad-library search "acupressure mat for back pain" --country NZ --max 80 --pretty
```

## Configuration

All configuration is environment variables in `.env`. **`.env.example` is the canonical list.** Copy it and fill it in; every variable is documented there alongside the measurement its default is based on.

`API_TOKEN` is the bearer token callers must send, and is required in production. The rest shape the traffic: how many sessions stay warm, when a session is retired, the gap between calls, the global calls-per-minute ceiling, the page cap and the wall-clock budget per search. `SCRAPER_PROXY` is optional and off by default; the primary path is the server's own IP.

## Development

```bash
uv run pytest        # 118 tests against saved pages and responses, no network
```

Layout: `scraper.py` (URL, tokens, form body, response classification, and the search with its budget and recovery rules), `session.py` (one fake browser session, its pacing and retirement, and the pool), `mapping.py` (output shape), `cache.py` (24 h result cache), `api.py` (FastAPI surface), `config.py` (env). Fixtures in `tests/fixtures/` are trimmed real page dumps; `tests/fixtures/README.md` says how each was cut and how to refresh them.

`diag` is the first thing to run on any new host, and the tool for when Meta changes something:

```bash
uv run facebook-ad-library diag --search --pages 2 --save-dir diag-out
```

It bootstraps one session step by step (challenge seen and cleared, cookies, each token found or missing, the discovered `doc_id`), then runs search pages and prints what came back. `--print-form` dumps the exact form body and `variables` for diffing against a DevTools capture. Exit code 2 means the bootstrap was refused, 3 the search failed, 4 no `doc_id` could be found.

## Deployment

Production runs as a single Docker container on the same VPS as n8n, attached to n8n's Docker network beside `trustpilot-reviews` and `reddit-reviews`. `docker-compose.yml` is the deployment topology and its comments record why each limit is what it is. There is no browser in the image, so the build is a few seconds and the container idles at a few tens of MB.

```bash
git clone https://github.com/haider-ecombench/facebook-ad-library-scraper.git
cd facebook-ad-library-scraper
cp .env.example .env          # set API_TOKEN
docker compose up -d --build
docker compose exec scraper uv run --no-sync facebook-ad-library diag --search   # from the VPS IP
```

n8n reaches the service by container name over the shared network:

```
http://facebook-ad-library:8002/facebook
```

Nothing is published to the host and nothing is reachable from the internet, so there is no domain, no TLS certificate and no reverse proxy to maintain. The n8n HTTP node uses a Header Auth credential carrying `Authorization: Bearer <API_TOKEN>`, the same pattern as `trustpilot-scraper` and `reddit-scraper`.

The network is declared external in `docker-compose.yml` as `n8n_default`, the default Compose creates for n8n's project. If that name differs the container refuses to start and says so; `docker network ls` gives the real one.

CI runs on pushes to `main` and on pull requests: unit tests, plus a full image build that starts the container and exercises `/health`, token enforcement, the `400` path and a live search with Stage 0's verbatim body. A `503` from a GitHub runner IP is an accepted outcome of that last step; anything else fails.
