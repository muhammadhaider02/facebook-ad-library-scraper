"""Self-hosted Meta Ad Library scraper, a drop-in for the Apify actor used by the Stage 0 workflow."""

import argparse
import json
import logging
import sys

__version__ = "0.2.0"


def _print(payload, pretty: bool) -> None:
    # ensure_ascii keeps this safe on a cp1252 Windows console.
    print(json.dumps(payload, indent=2 if pretty else None, ensure_ascii=True))


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import settings

    uvicorn.run(
        "facebook_ad_library.api:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level="info",
    )
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from .mapping import to_item
    from .scraper import FacebookError, search
    from .session import counters, pool

    runs = []
    failed = 0
    for _ in range(max(1, args.repeat)):
        for query in args.query:
            try:
                result = search(query, args.country, args.max, pool=pool)
            except FacebookError as e:
                failed += 1
                runs.append({"query": query, "country": args.country, "error": {"type": type(e).__name__, "status": e.status, "message": str(e)}})
                print(f"{query!r} {args.country}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            items = [to_item(a, result.query, result.country) for a in result.ads]
            runs.append(
                {
                    "query": result.query,
                    "country": result.country,
                    "seconds": result.seconds,
                    "attempts": result.attempts,
                    "misses": result.misses,
                    "session_swaps": result.session_swaps,
                    "ads": len(items),
                    "unique_pages": len({i["page_id"] for i in items}),
                    "domains": sorted({(i["snapshot"]["caption"] or "") for i in items} - {""})[:20],
                    "items": items if not args.summary else [],
                }
            )
            print(
                f"{result.query!r} {result.country}: {len(items)} ads, {runs[-1]['unique_pages']} pages, "
                f"{result.attempts} GET(s), {result.misses} miss(es) in {result.seconds}s",
                file=sys.stderr,
            )
    _print({"runs": runs, "failed": failed, "sessions": counters}, args.pretty)
    return 2 if failed and failed == len(runs) else 0


def _cmd_diag(args: argparse.Namespace) -> int:
    """One session, step by step: the challenge, the cookies, and what shape each page came in."""
    from pathlib import Path

    from . import scraper as wire
    from .config import settings
    from .session import FbSession, RateLimiter, default_transport

    out = Path(args.save_dir) if args.save_dir else None
    if out:
        out.mkdir(parents=True, exist_ok=True)

    session = FbSession(default_transport(), RateLimiter(settings.rate_limit_per_min))
    print(f"impersonate={settings.impersonate} proxy={'yes' if settings.proxy else 'no'} retries={settings.ssr_retries}")
    print(f"page {wire.bootstrap_url(args.query, args.country)}")
    shapes: list[str] = []
    for n in range(1, max(1, args.repeat) + 1):
        try:
            page, ads = session.fetch(args.query, args.country)
        except wire.FacebookError as e:
            for line in session.trace:
                print("  " + line)
            print(f"GET {n}: {type(e).__name__}: {e}")
            if out and session.last_text:
                (out / f"page{n}_{type(e).__name__}.html").write_text(session.last_text, encoding="utf-8")
            return 2 if isinstance(e, wire.ScrapeBlocked) else 3
        for line in session.trace:
            print("  " + line)
        shapes.append(page.value)
        pages = {str(a.get("page_id")) for a in ads}
        first = (ads[0].get("snapshot") or {}) if ads else {}
        print(
            f"GET {n}: {page.value}, {len(ads)} ads, {len(pages)} unique pages"
            f"{'; first: ' + str(first.get('caption')) + ' ' + str(first.get('page_categories')) if ads else ''}"
        )
        if out:
            p = out / f"page{n}_{page.value}.html"
            p.write_text(session.last_text, encoding="utf-8")
            print(f"  saved {p} ({len(session.last_text)} bytes)")
    print(f"session: {session.requests_made} GET(s), {session.challenges} challenge(s), retired={session.retired}")
    if shapes and all(s == "miss" for s in shapes):
        print("every page came without results; retry, or raise SSR_RETRIES if this persists")
        return 5
    return 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="facebook-ad-library")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the HTTP service n8n calls")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=_cmd_serve)

    search_p = sub.add_parser("search", help="search one or more keywords from the command line and print JSON")
    search_p.add_argument("query", nargs="+")
    search_p.add_argument("--country", default="US")
    search_p.add_argument("--max", type=int, default=80)
    search_p.add_argument("--repeat", type=int, default=1, help="run the whole keyword list this many times")
    search_p.add_argument("--summary", action="store_true", help="omit the items, keep the counts")
    search_p.add_argument("--pretty", action="store_true")
    search_p.set_defaults(func=_cmd_search)

    diag = sub.add_parser("diag", help="fetch the search page on one session verbosely; the deploy check")
    diag.add_argument("--query", default="running shoes")
    diag.add_argument("--country", default="US")
    diag.add_argument("--repeat", type=int, default=1, help="fetch the same page this many times on the one session")
    diag.add_argument("--save-dir", help="write each raw page here (gitignored diag-out/)")
    diag.set_defaults(func=_cmd_diag)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))
