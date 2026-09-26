<div align="center">

# Facebook Ad Library

**FETCH. READ. SERVE.**

[![CI](https://github.com/muhammadhaider02/facebook-ad-library-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/muhammadhaider02/facebook-ad-library-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![curl_cffi](https://img.shields.io/badge/HTTP-curl__cffi-FF6F00)](https://github.com/lexiforest/curl_cffi)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Self-hosted Meta Ad Library keyword search and brand lookup over HTTP: Chrome TLS, residential sticky exits, no browser.

[Architecture](docs/architecture.md) · [API](docs/api.md) · [Deployment](docs/deployment.md)

</div>

---

## What it is

A drop-in replacement for two paid vendors, answering the body each one takes in the shape each one returns:

- `POST /facebook` stands in for the Apify actor `igolaizola~facebook-ad-library-scraper`: one keyword and one country in, the first page of active ads the Ad Library shows for that search out (up to 30; deeper with `max_pages`).
- `POST /adyntel` stands in for the Adyntel API: a page id, page URL or domain in, that page's total ad count (live, lifetime or video) and its top ads out.

Beside them, `POST /jobs` takes a whole batch of searches, counts or homepage fetches and is polled for the results, and `POST /fetch` reads one homepage through the same exits. See [api.md](docs/api.md) for the endpoints, auth (`Authorization: Bearer`) and the error contract.

Every request to Facebook runs on a **lane**: one sticky residential proxy exit with its own cookie jars, pacing and limiter. A search reads the Ad Library page, which Meta serves with the results embedded; when Meta withholds the ads from an exit, the same lane recovers them over GraphQL, and a refused request moves to another lane (see [architecture.md](docs/architecture.md#lanes)).

It was built as one of three services behind an n8n brand-sourcing pipeline, beside the siblings `trustpilot-reviews` and `reddit-reviews`, and replaces the paid Apify actor and the Adyntel API there.

## Quickstart

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and a residential proxy with sticky ports. Runs as a single FastAPI process; there is no browser to install.

```bash
git clone https://github.com/muhammadhaider02/facebook-ad-library-scraper.git
cd facebook-ad-library-scraper

uv sync                               # dependencies into a uv-managed venv

cp .env.example .env                  # then set API_TOKEN and the lane proxy (see Configuration)

uv run facebook-ad-library serve      # HTTP service on :8003
```

Verify with `curl localhost:8003/health` (expects `"status":"ok"` and `"proxy":true`); interactive API docs are at `/docs`.

## Configuration

All configuration is environment variables in `.env`. **`.env.example` is the canonical list**; the reference with defaults is in [architecture.md](docs/architecture.md#configuration-environment-variables).

Required:

| Variable | |
|---|---|
| `API_TOKEN` | the bearer token callers send. Empty disables auth, for local testing only |
| `LANE_PROXY_TEMPLATE` + `LANE_PROXY_PORTS` | a proxy URL with `{port}` in it, and the sticky ports to fill it with (`11510-11529`). The first `LANE_COUNT` ports become lanes, the rest are the reserve a blocked lane rotates onto |
| or `LANE_PROXIES` | full proxy URLs, comma-separated, one per lane, used when the template is empty |

`LANE_REQUIRE_PROXY` defaults to `true`: a lane without a proxy refuses to start. Only CI, which has no residential exit, sets it `false`. `LANE_COUNT` (default 1) is the concurrency. The rest shape the traffic (pacing, retries, budgets), the caches and the job store.

## Development

```bash
uv run pytest            # 331 tests against saved page fixtures, no network
```

## Deployment

Runs as a Docker container on the host that runs n8n, attached to n8n's Docker network with nothing published to the host. Deploys are a `git pull` and `docker compose -f docker-compose.sourcing.yml up -d --build`; CI runs on push to `main`. The runbook (topology, env, checks, and wiring it into n8n) is in [deployment.md](docs/deployment.md).

## Disclaimer

This service reads Meta's public Ad Library pages. You are responsible for complying with Meta's terms and with the law that applies to you. It is not affiliated with Meta, Apify or Adyntel.

## License

[MIT](LICENSE)
