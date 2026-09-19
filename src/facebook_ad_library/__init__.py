"""Self-hosted Meta Ad Library scraper, a drop-in for the Apify actor used by the Stage 0 workflow."""

import argparse
import json
import logging
import sys

__version__ = "0.1.0"


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
                    "pages": result.pages_fetched,
                    "truncated": result.truncated,
                    "partial": result.partial,
                    "session_swaps": result.session_swaps,
                    "ads": len(items),
                    "unique_pages": len({i["page_id"] for i in items}),
                    "domains": sorted({(i["snapshot"]["caption"] or "") for i in items} - {""})[:20],
                    "items": items if not args.summary else [],
                }
            )
            print(
                f"{result.query!r} {result.country}: {len(items)} ads, {runs[-1]['unique_pages']} pages, "
                f"{result.pages_fetched} page(s) in {result.seconds}s",
                file=sys.stderr,
            )
    _print({"runs": runs, "failed": failed, "sessions": counters}, args.pretty)
    return 2 if failed and failed == len(runs) else 0


def _cmd_diag(args: argparse.Namespace) -> int:
    """facebook.md §10 test A and the shape check: bootstrap one session verbosely, then search."""
    import uuid
    from pathlib import Path

    from . import scraper as wire
    from .config import settings
    from .session import FbSession, RateLimiter, default_transport

    out = Path(args.save_dir) if args.save_dir else None
    if out:
        out.mkdir(parents=True, exist_ok=True)

    def save(name: str, text: str) -> None:
        if out:
            (out / name).write_text(text, encoding="utf-8")
            print(f"  saved {out / name} ({len(text)} bytes)")

    transport = default_transport()
    if out:
        # Wrap the transport so the raw pages land on disk for fixture trimming.
        real_get = transport.get

        def recording_get(url, headers=None):
            r = real_get(url, headers=headers)
            if "/ads/library/" in url:
                save(f"bootstrap_{r.status}.html", r.text)
            return r

        transport.get = recording_get  # type: ignore[method-assign]

    session = FbSession(transport, RateLimiter(settings.rate_limit_per_min))
    print(f"impersonate={settings.impersonate} proxy={'yes' if settings.proxy else 'no'} doc_id_override={settings.doc_id or '-'}")
    print(f"bootstrap {wire.bootstrap_url(args.query, args.country)}")
    try:
        session.mint(args.query, args.country)
    except wire.FacebookError as e:
        for line in session.trace:
            print("  " + line)
        print(f"BOOTSTRAP FAILED: {type(e).__name__}: {e}")
        return 4 if isinstance(e, wire.DocIdStale) else 2
    for line in session.trace:
        print("  " + line)
    t = session.tokens
    assert t is not None
    print("tokens:")
    for name in wire.TOKEN_PATTERNS:
        value = getattr(t, name, "")
        print(f"  {name:17s} {'ok  ' if value else 'MISSING'} {str(value)[:48]}")
    print(f"doc_id: {session.doc_id} ({session.doc_id_source})")

    if args.print_form:
        variables = wire.build_variables(
            query=args.query, country=args.country, cursor=None, collation_token=str(uuid.uuid4()),
            session_id=session.session_id, first=settings.page_size, extra=wire.variables_extra(),
        )
        form = wire.build_form(t, session.doc_id, variables, 1)
        _print({"form": form, "variables": variables}, True)

    if not args.search:
        return 0

    cursor = None
    collation = str(uuid.uuid4())
    for page in range(1, args.pages + 1):
        try:
            ads, cursor = session.search_page(args.query, args.country, cursor, collation, settings.page_size)
        except wire.FacebookError as e:
            print(f"page {page}: {type(e).__name__}: {e}")
            return 3
        pages = {str(a.get("page_id")) for a in ads}
        first = (ads[0].get("snapshot") or {}) if ads else {}
        print(
            f"page {page}: {len(ads)} ads, {len(pages)} unique pages, next_cursor={'yes' if cursor else 'no'}"
            f"{'; first: ' + str(first.get('caption')) + ' ' + str(first.get('page_categories')) if ads else ''}"
        )
        save(f"search_page{page}_raw.json", session.last_text)
        save(f"search_page{page}.json", json.dumps({"ads": ads, "cursor": cursor}, ensure_ascii=False))
        if not cursor:
            break
    print(f"session: {session.requests_made} call(s), retired={session.retired}")
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

    diag = sub.add_parser("diag", help="bootstrap one session verbosely and optionally search (facebook.md §10 A)")
    diag.add_argument("--query", default="running shoes")
    diag.add_argument("--country", default="US")
    diag.add_argument("--search", action="store_true", help="also run search pages on the minted session")
    diag.add_argument("--pages", type=int, default=1)
    diag.add_argument("--print-form", action="store_true", help="dump the form body and variables for diffing against DevTools")
    diag.add_argument("--save-dir", help="write the raw bootstrap HTML and search JSON here (gitignored diag-out/)")
    diag.set_defaults(func=_cmd_diag)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))
