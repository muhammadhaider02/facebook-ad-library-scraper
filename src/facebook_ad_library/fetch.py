"""Homepage fetches through a lane's exit: `POST /fetch` and the `fetch` items of a job.

The sourcing workflow verifies a sourced brand by reading its homepage, and 21 % of those reads
fail from the server (measured 24-25 Sep 2026: 103 of 499, half of them 403s from bot walls that see a bare
Node.js request from a data-centre address). The lanes already hold residential exits and a
Chrome TLS profile, so this fetches the page the way a browser on a home connection would and
hands the text back. It is NOT a lane try: no Facebook cookie jar, no limiter, no throttle
memory, no cooldown - a homepage that refuses is the brand's problem, not the exit's.

The call never raises. A failed fetch is an answer (`ok: false`, the status and the error), so a
caller pairing pages to brands by position never loses a slot.

`page_summary` turns the page into the few facts the DTC verdict needs (26 Sep 2026): the sourcing
workflow used to carry up to 300 KB of raw HTML per brand through n8n to show Claude its first 1,200 stripped
characters, mostly menu and cart text, while the meta description and the product data the shop
publishes for search engines were thrown away. Plain parsing, no model: about 3 KB per brand.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .config import settings

log = logging.getLogger(__name__)

SUMMARY_TEXT_CHARS = 1500
PARSE_MAX_CHARS = 1_500_000


def normalise_url(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        raise ValueError("invalid request: `url` is required")
    if "://" not in u:
        u = "https://" + u.lstrip("/")
    if not u.lower().startswith(("http://", "https://")):
        raise ValueError(f"invalid request: unsupported url {u!r}")
    return u


def validate_public_url(url: str) -> str:
    """`normalise_url`, then refuse what is not a public website: localhost, a dotless name (a
    container on the Docker network), or an IP literal that is private, loopback, link-local or
    reserved. No DNS lookup here: the name resolves at the proxy, not on this host."""
    u = normalise_url(url)
    try:
        host = (urlsplit(u).hostname or "").strip(".").lower()
    except ValueError as e:
        raise ValueError(f"invalid request: unparseable url {u!r}") from e
    if not host:
        raise ValueError(f"invalid request: no host in {u!r}")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        raise ValueError(f"invalid request: {host!r} is not a public host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise ValueError(f"invalid request: {host!r} is not a public address")
    elif "." not in host:
        raise ValueError(f"invalid request: {host!r} is not a public host")
    return u


def www_variant(url: str) -> str | None:
    """`https://brand.com` -> `https://www.brand.com`, for a retry after a connection or DNS error;
    None when the host already starts with www. or is an address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host or host.startswith("www.") or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        return None
    return f"{parts.scheme}://www.{parts.netloc}{parts.path or ''}" + (f"?{parts.query}" if parts.query else "")


def fetch_page(url: str, proxy: str | None, timeout_s: float, max_bytes: int, summarise: bool = False) -> dict:
    """One GET with the Chrome profile through `proxy`. Returns a dict, never raises. With
    `summarise`, `summary` holds `page_summary` of the page (parsed from up to 1.5 MB of it)."""
    from curl_cffi import requests

    started = time.time()
    out = {"url": url, "ok": False, "status": None, "final_url": None, "bytes": 0, "text": "", "error": None, "seconds": 0.0}
    try:
        kwargs = {"impersonate": settings.impersonate, "timeout": timeout_s, "allow_redirects": True, "max_redirects": 5,
                  "headers": {"Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}}
        if proxy:
            kwargs["proxy"] = proxy
        r = requests.get(url, **kwargs)
        text = r.text or ""
        out.update(status=int(r.status_code), final_url=str(r.url or url), bytes=len(r.content or b""), text=text[:max_bytes])
        out["ok"] = 200 <= int(r.status_code) < 400 and bool(text.strip())
        if not out["ok"] and out["error"] is None:
            out["error"] = f"http {r.status_code}" if text.strip() else f"http {r.status_code}, empty body"
        if summarise and out["ok"]:
            out["summary"] = page_summary(text[:PARSE_MAX_CHARS], url, out["final_url"])
    except Exception as e:  # noqa: BLE001 - the whole point is to answer, not raise
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    out["seconds"] = round(time.time() - started, 2)
    return out


def is_connection_error(error: str | None) -> bool:
    """A curl failure before any HTTP answer (DNS, refused, reset, TLS, proxy tunnel), as opposed
    to a site that answered with a status."""
    return bool(error) and not str(error).startswith("http ")


# --------------------------------------------------------------------------- page summary

# Not `head`: an unclosed <head> would hide the whole body, and it holds no visible text anyway.
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
_CHROME_TAGS = {"nav", "header", "footer"}
_BLOCK_TAGS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "td", "tr", "button"}
_MARKETPLACES = re.compile(r"(^|\.)(amazon|walmart|etsy|ebay|target|bestbuy|aliexpress|temu|wayfair|homedepot|lowes|costco|kohls|macys|sephora|ulta)\.", re.I)
_WHERE_TO_BUY = re.compile(r"stockists?|where to buy|find a (retailer|store|stockist)|store locator|wholesale|become a (retailer|stockist|dealer)", re.I)
_SAAS = re.compile(r"free trial|start (your )?free|book a demo|request a demo|schedule a demo|get a demo|pricing plans?|per (user|seat)|/mo(nth)? per|sign up free|no credit card required|api docs|integrations", re.I)
_CART = re.compile(r"add[\s_-]to[\s_-](cart|bag|basket)|buy now|shop now|add to order", re.I)
_PLATFORMS = (("shopify", re.compile(r"cdn\.shopify\.com|shopify\.theme|myshopify\.com", re.I)),
              ("woocommerce", re.compile(r"woocommerce", re.I)),
              ("bigcommerce", re.compile(r"bigcommerce", re.I)),
              ("wix", re.compile(r"wixstatic\.com|wix\.com", re.I)),
              ("squarespace", re.compile(r"squarespace", re.I)),
              ("magento", re.compile(r"magento|mage-cache", re.I)))


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.lang = ""
        self.headings: list[str] = []
        self.jsonld: list[str] = []
        self.chunks: list[str] = []
        self.hrefs: list[str] = []
        self._skip = 0
        self._chrome = 0
        self._in_title = False
        self._in_jsonld = False
        self._heading: list[str] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"][:12]
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description", "og:site_name", "og:title", "og:type") and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld = True
            self._buf = []
            return
        if tag == "title" and not self.title:
            self._in_title = True
            self._buf = []
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _CHROME_TAGS:
            self._chrome += 1
        elif tag in ("h1", "h2") and not self._skip:
            self._heading = []
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.append("".join(self._buf))
            return
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = " ".join("".join(self._buf).split())
            return
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _CHROME_TAGS and self._chrome:
            self._chrome -= 1
        elif tag in ("h1", "h2") and self._heading is not None:
            h = " ".join(" ".join(self._heading).split())
            if h and h not in self.headings:
                self.headings.append(h)
            self._heading = None
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._in_jsonld or self._in_title:
            self._buf.append(data)
            return
        if self._skip:
            return
        if self._heading is not None:
            self._heading.append(data)
        if not self._chrome:
            self.chunks.append(data)


def _site(host: str) -> str:
    parts = [p for p in (host or "").lower().strip(".").split(".") if p]
    if parts and parts[0] == "www":
        parts = parts[1:]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _walk_jsonld(node, types: list[str], products: list[dict], brands: list[str]) -> None:
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, types, products, brands)
        return
    if not isinstance(node, dict):
        return
    t = node.get("@type")
    for x in (t if isinstance(t, list) else [t]):
        if isinstance(x, str) and x not in types:
            types.append(x)
    tl = [str(x).lower() for x in (t if isinstance(t, list) else [t]) if x]
    if "product" in tl and len(products) < 5:
        offers = node.get("offers")
        offer = offers[0] if isinstance(offers, list) and offers else offers
        price = cur = None
        if isinstance(offer, dict):
            price = offer.get("price") or offer.get("lowPrice")
            cur = offer.get("priceCurrency")
        b = node.get("brand")
        products.append({"name": str(node.get("name") or "")[:100], "price": str(price)[:20] if price is not None else None,
                         "currency": str(cur)[:5] if cur else None, "brand": str(b.get("name") if isinstance(b, dict) else (b or ""))[:60] or None})
    if tl and tl[0] in ("organization", "brand", "onlinestore", "store", "website") and node.get("name"):
        n = str(node["name"])[:80]
        if n not in brands:
            brands.append(n)
    for k in ("@graph", "itemListElement", "item", "mainEntity"):
        if k in node:
            _walk_jsonld(node[k], types, products, brands)


def page_summary(html: str, requested_url: str, final_url: str | None) -> dict:
    """What a homepage says about the business, in about 3 KB. Never raises: a parse failure
    falls back to a regex strip of the text."""
    html = html or ""
    try:
        p = _Page()
        p.feed(html)
        p.close()
    except Exception as e:  # noqa: BLE001 - a malformed page still gets a summary
        log.debug("page_summary parse failed: %s", e)
        stripped = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
        stripped = re.sub(r"<[^>]+>", "\n", stripped)
        p = _Page()
        p.chunks = [stripped]

    lines, seen = [], set()
    for line in "".join(p.chunks).split("\n"):
        line = " ".join(line.split())
        if len(line) < 3 or line.lower() in seen:
            continue
        seen.add(line.lower())
        lines.append(line)
    text = " · ".join(lines)
    types: list[str] = []
    products: list[dict] = []
    brands: list[str] = []
    for raw in p.jsonld[:20]:
        try:
            _walk_jsonld(json.loads(raw.strip()), types, products, brands)
        except (ValueError, TypeError, RecursionError):
            continue

    hosts, product_links, marketplace, cart_links = [], 0, set(), False
    for h in p.hrefs:
        hl = h.lower()
        if "/products/" in hl or "/product/" in hl or "/collections/" in hl or "/shop/" in hl:
            product_links += 1
        if "/cart" in hl or "/checkout" in hl or "/basket" in hl:
            cart_links = True
        if hl.startswith(("http://", "https://", "//")):
            host = urlsplit(h if not hl.startswith("//") else "https:" + h).hostname or ""
            m = _MARKETPLACES.search(host)
            if m:
                marketplace.add(m.group(2).lower())
    visible = text.lower()
    req_site = _site(urlsplit(requested_url).hostname or "")
    fin_site = _site(urlsplit(final_url or requested_url).hostname or "")
    platform = next((name for name, rx in _PLATFORMS if rx.search(html[:400_000])), None)
    return {
        "title": p.title[:200],
        "description": (p.meta.get("description") or p.meta.get("og:description") or "")[:300],
        "site_name": (p.meta.get("og:site_name") or "")[:80],
        "lang": p.lang,
        "platform": platform,
        "has_cart": bool(_CART.search(visible)) or cart_links,
        "product_links": product_links,
        "products": products,
        "jsonld_types": types[:10],
        "brand_names": brands[:3],
        "marketplace_links": sorted(marketplace)[:6],
        "where_to_buy": bool(_WHERE_TO_BUY.search(visible)),
        "saas_markers": sorted({m.group(0).lower() for m in _SAAS.finditer(visible)})[:5],
        "redirected_to": fin_site if fin_site and req_site and fin_site != req_site else None,
        "headings": [h[:120] for h in p.headings[:8]],
        "text": text[:SUMMARY_TEXT_CHARS],
    }
