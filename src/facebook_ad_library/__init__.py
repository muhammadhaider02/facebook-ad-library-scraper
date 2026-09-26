"""Self-hosted Meta Ad Library scraper, a drop-in for the Apify actor used by a brand-sourcing workflow."""

import argparse
import json
import logging
import sys

__version__ = "0.4.3"


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


def _lane(n: int):
    """Lane `n` (0-based) built from the environment, exactly as the service would, with no
    worker threads: the CLI drives one try at a time. Nothing reaches Facebook outside a lane."""
    from .lanes import build_dispatcher

    d = build_dispatcher()
    if n < 0 or n >= len(d.lanes):
        raise SystemExit(f"--lane {n}: LANE_COUNT is {len(d.lanes)}")
    lane = d.lanes[n]
    lane.check_ip("cli")
    print(f"{lane.tag()} proxy={'yes' if lane.proxy_url else 'NO (direct; LANE_REQUIRE_PROXY is off)'}", file=sys.stderr)
    return lane


def _cmd_search(args: argparse.Namespace) -> int:
    import time

    from .config import settings
    from .lanes import Item, run_search_try
    from .mapping import to_item
    from .session import counters

    lane = _lane(args.lane)
    runs = []
    failed = 0
    for _ in range(max(1, args.repeat)):
        for query in args.query:
            item = Item("search", f"{query}|{args.country}", 1, time.time() + settings.scrape_budget_s, query=query, country=args.country.upper())
            t = run_search_try(lane, item)
            lane.record(t)
            if t.status not in ("ok", "no_ads"):
                failed += 1
                runs.append({"query": query, "country": args.country, "status": t.status, "error": {"type": t.error_type, "message": t.error}, "seconds": t.seconds})
                print(f"{query!r} {args.country}: {t.status} {t.error_type}: {t.error}", file=sys.stderr)
                continue
            items = [to_item(a, query, args.country.upper()) for a in t.ads][: args.max]
            r = t.result
            runs.append(
                {
                    "query": query,
                    "country": args.country.upper(),
                    "status": t.status,
                    "lane": lane.name,
                    "exit_ip": lane.exit_ip,
                    "seconds": t.seconds,
                    "attempts": r.attempts if r else 0,
                    "misses": r.misses if r else 0,
                    "session_swaps": r.session_swaps if r else 0,
                    "reported_total": t.count,
                    "recovered_over_graphql": t.run is not None,
                    "decoded_bytes": t.decoded_bytes,
                    "ads": len(items),
                    "unique_pages": len({i["page_id"] for i in items}),
                    "domains": sorted({(i["snapshot"]["caption"] or "") for i in items} - {""})[:20],
                    "items": items if not args.summary else [],
                }
            )
            print(
                f"{query!r} {args.country}: {t.status} {len(items)} ads, {runs[-1]['unique_pages']} pages, total={t.count}, "
                f"{runs[-1]['attempts']} GET(s), {runs[-1]['misses']} miss(es), {t.decoded_bytes // 1024} KB in {t.seconds}s on {lane.tag()}",
                file=sys.stderr,
            )
    _print({"runs": runs, "failed": failed, "lane": lane.snapshot(), "sessions": counters}, args.pretty)
    return 2 if failed and failed == len(runs) else 0


def _cmd_brand(args: argparse.Namespace) -> int:
    """One brand lookup, the way POST /adyntel makes it, printed as the vendor envelope."""
    import time

    from .adyntel_mapping import to_envelope
    from .config import settings
    from .lanes import Item, run_count_try
    from .session import counters

    kwargs = {}
    if args.page_id:
        kwargs["page_id"] = args.page_id
    elif args.url:
        kwargs["facebook_url"] = args.url
    elif args.domain:
        kwargs["company_domain"] = args.domain
    else:
        print("one of --page-id, --url or --domain is required", file=sys.stderr)
        return 2
    lane = _lane(args.lane)
    item = Item("count", "cli", 0, time.time() + settings.brand_budget_s, status=args.status, media=args.media, **kwargs)
    t = run_count_try(lane, item)
    lane.record(t)
    if t.status not in ("ok", "not_found"):
        print(f"{t.status} {t.error_type}: {t.error}", file=sys.stderr)
        _print({"error": {"type": t.error_type or t.status, "status": 503, "message": t.error, "kind": t.status}, "lane": lane.snapshot(), "sessions": counters}, args.pretty)
        return 3
    res = t.brand
    env = to_envelope(res, args.max)
    print(
        f"{res.resolver}={res.query}: {'found' if res.found else 'not found'}"
        f"{' page ' + str(res.page_id) + ' (' + str(res.page_name) + ')' if res.found else ' (' + res.note + ')'}"
        f"; count={res.count} ads={len(res.ads or [])} status={res.active_status} media={res.media_type}"
        f"; {res.attempts} page GET(s), {res.misses} miss(es), {res.plain_gets} plain GET(s), {res.session_swaps} swap(s), "
        f"{t.decoded_bytes // 1024} KB, {res.seconds}s on {lane.tag()}",
        file=sys.stderr,
    )
    if res.found and args.summary:
        env = {k: v for k, v in env.items() if k != "results"}
        env["results_summary"] = [
            {
                "ad_archive_id": r["ad_archive_id"], "is_active": r["is_active"], "format": r["snapshot"]["display_format"],
                "title": r["snapshot"]["title"], "link_url": r["snapshot"]["link_url"],
                "videos": [v["duration_s"] for v in r["snapshot"]["videos"]] + [c["duration_s"] for c in r["snapshot"]["cards"] if c["video_sd_url"]],
            }
            for r in to_envelope(res, args.max)["results"]
        ]
    _print(env, args.pretty)
    return 0


def _cmd_diag(args: argparse.Namespace) -> int:
    """One session on one lane, step by step: the exit IP, the challenge, the cookies, and what
    shape each page came in."""
    from pathlib import Path

    from . import scraper as wire
    from .config import settings
    from .session import FbSession

    out = Path(args.save_dir) if args.save_dir else None
    if out:
        out.mkdir(parents=True, exist_ok=True)

    lane = _lane(args.lane)
    if args.lane_ip:
        # The deploy check for a fresh exit: the same lane asked for its address `repeat` times.
        ips = [lane.check_ip("diag") for _ in range(max(1, args.repeat))]
        print(f"{lane.name} port={lane.port} exit ip over {len(ips)} check(s): {ips}")
        return 0 if ips and all(ips) and len(set(ips)) == 1 else 5

    session = FbSession(lane._make_transport(), lane.limiter)
    print(f"impersonate={settings.impersonate} {lane.tag()} retries={settings.ssr_retries}")

    if args.slug:
        for what, url, reader in (
            ("plugin", wire.plugin_url(args.slug), wire.page_id_from_plugin),
            ("profile", f"{wire.ORIGIN}/{args.slug}", wire.page_id_from_profile),
        ):
            try:
                r = session.get_plain(url)
                for line in session.trace:
                    print("  " + line)
                page_id = reader(r.text)
                print(f"{what} {url}: http {r.status}, {len(r.text)} bytes, page id {page_id or 'not found'}")
            except wire.FacebookError as e:
                for line in session.trace:
                    print("  " + line)
                print(f"{what} {url}: {type(e).__name__}: {e}")
                if out and session.last_text:
                    (out / f"{what}_{args.slug}_{type(e).__name__}.html").write_text(session.last_text, encoding="utf-8")
                return 2 if isinstance(e, wire.ScrapeBlocked) else 3
            if out:
                p = out / f"{what}_{args.slug}.html"
                p.write_text(r.text, encoding="utf-8")
                print(f"  saved {p}")
        return 0

    if args.page_id:
        url = wire.page_view_url(args.page_id, args.status, args.media)
    else:
        url = wire.bootstrap_url(args.query, args.country)
    print(f"page {url}")
    shapes: list[str] = []
    for n in range(1, max(1, args.repeat) + 1):
        try:
            if args.page_id:
                page, _, html = session.fetch_url(url)
                _, view = wire.classify_page_view(html)
                ads = view.ads if view else []
            else:
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
        total = f", count={view.count}, page={'known: ' + str(view.info.get('page_name')) if view.known else 'UNKNOWN'}" if args.page_id and page is not wire.Page.MISS else ""
        print(
            f"GET {n}: {page.value}, {len(ads)} ads, {len(pages)} unique pages{total}"
            f"{'; first: ' + str(first.get('caption')) + ' ' + str(first.get('page_categories')) if ads else ''}"
        )
        if out:
            p = out / f"page{n}_{page.value}.html"
            p.write_text(session.last_text, encoding="utf-8")
            print(f"  saved {p} ({len(session.last_text)} bytes)")
    print(f"session: {session.requests_made} GET(s), {session.challenges} challenge(s), retired={session.retired}; lane decoded {lane.decoded_bytes // 1024} KB")
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

    search_p = sub.add_parser("search", help="search one or more keywords on one lane and print JSON")
    search_p.add_argument("query", nargs="+")
    search_p.add_argument("--country", default="US")
    search_p.add_argument("--max", type=int, default=80)
    search_p.add_argument("--lane", type=int, default=0, help="which configured lane to use (0-based)")
    search_p.add_argument("--repeat", type=int, default=1, help="run the whole keyword list this many times")
    search_p.add_argument("--summary", action="store_true", help="omit the items, keep the counts")
    search_p.add_argument("--pretty", action="store_true")
    search_p.set_defaults(func=_cmd_search)

    brand_p = sub.add_parser("brand", help="look one brand up the way POST /adyntel does, on one lane, and print the vendor envelope")
    brand_p.add_argument("--page-id", help="the advertiser's numeric page id")
    brand_p.add_argument("--url", help="a Facebook page URL (vanity or with the id in it)")
    brand_p.add_argument("--domain", help="the brand's website domain")
    brand_p.add_argument("--status", default="active", help="active | inactive | all (video is always active)")
    brand_p.add_argument("--media", default="all", help="all | video")
    brand_p.add_argument("--max", type=int, default=10, help="results to print, up to the page's 30")
    brand_p.add_argument("--lane", type=int, default=0, help="which configured lane to use (0-based)")
    brand_p.add_argument("--summary", action="store_true", help="one line per ad instead of the full results")
    brand_p.add_argument("--pretty", action="store_true")
    brand_p.set_defaults(func=_cmd_brand)

    diag = sub.add_parser("diag", help="fetch the search page on one lane's session verbosely; the deploy check")
    diag.add_argument("--query", default="running shoes")
    diag.add_argument("--country", default="US")
    diag.add_argument("--page-id", help="fetch this advertiser's page view instead of a keyword search")
    diag.add_argument("--status", default="active")
    diag.add_argument("--media", default="all")
    diag.add_argument("--slug", help="fetch the page plugin and the profile page for this vanity handle and read the page id")
    diag.add_argument("--lane", type=int, default=0, help="which configured lane to use (0-based)")
    diag.add_argument("--lane-ip", action="store_true", help="only learn the lane's exit ip `--repeat` times and check it is stable")
    diag.add_argument("--repeat", type=int, default=1, help="fetch the same page this many times on the one session")
    diag.add_argument("--save-dir", help="write each raw page here (gitignored diag-out/)")
    diag.set_defaults(func=_cmd_diag)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))
