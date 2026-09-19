<div align="center">

# Facebook Ad Library

**FETCH. READ. SERVE.**

[![CI](https://github.com/haider-ecombench/facebook-ad-library-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/haider-ecombench/facebook-ad-library-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![curl_cffi](https://img.shields.io/badge/HTTP-curl__cffi-FF6F00)](https://github.com/lexiforest/curl_cffi)

Self-hosted Meta Ad Library keyword search with an HTTP API: one page GET per search, no GraphQL, no browser.

[Architecture](docs/architecture.md) · [API](docs/api.md) · [Deployment](docs/deployment.md) · [n8n test](docs/n8n-test-2026-09-19.md)

</div>

---

## Platform

This repo is a standalone ad-collection service behind the SmartLead brand-sourcing pipeline, running over its own HTTP API. It is the sibling of `trustpilot-reviews` and `reddit-reviews`, which serve their sites the same way, and all three stand in for the Apify actors the pipeline used to call.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Runs as a single FastAPI process; there is no browser to install.

```bash
git clone https://github.com/haider-ecombench/facebook-ad-library-scraper.git
cd facebook-ad-library-scraper

uv sync                               # dependencies into a uv-managed venv

cp .env.example .env                  # then set API_TOKEN (see Configuration)

uv run facebook-ad-library serve      # HTTP service on :8002
```

Verify with `curl localhost:8002/health` (expects `"status":"ok"`); interactive API docs are at `/docs`.

The service is driven over its HTTP API: one keyword and one country in, the first page of active ads the Ad Library shows for that search out (up to 30), in the Apify actor's request and response shape. It reads them from the search page itself, which Meta serves with the results embedded; it never calls the GraphQL endpoint, which Meta refuses from datacenter addresses ([why](docs/address-classification.md)). See [api.md](docs/api.md) for the endpoint, auth (`Authorization: Bearer`) and the error contract.

## Configuration

All configuration is environment variables in `.env`. **`.env.example` is the canonical list.** Copy it and fill it in; the full reference with defaults lives in [architecture.md](docs/architecture.md#configuration-environment-variables).

The only required value is the API bearer token. The rest shape the traffic (sessions, pacing, retries and the budget), the result cache and an optional proxy.

## Development

```bash
uv run pytest            # 94 tests against saved page fixtures, no network
```

## Deployment

Production runs as a single Docker container on the Hostinger VPS that hosts n8n, attached to n8n's Docker network beside the two siblings, with nothing published to the host. Deploys are a `git pull` and `docker compose up -d --build`; CI runs on push to `main`. The operational runbook (topology, env, checks and gotchas) is in [deployment.md](docs/deployment.md).
