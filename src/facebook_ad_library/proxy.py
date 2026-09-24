"""Proxy credential parsing, lifted from reddit-reviews' proxy.py minus the exit-port pool.

There is no residential fallback path in this service yet (facebook.md §5.5 lists where one would
belong). What remains is the one guard that has already bitten the sibling: every residential
vendor's dashboard hands out the ROTATING gateway by default, and a rotating exit under a session
whose cookie jar Meta ties to an IP is the pattern that gets that session refused. It is rejected
at config time, loudly, rather than debugged later as a mysterious block.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

from .config import settings

# Keyed by port because that is all the credential tells us. Decodo 7000, DataImpulse 823.
ROTATING_GATEWAY_PORTS = {"7000": "Decodo", "823": "DataImpulse"}


def fallback_proxy_url() -> str | None:
    """The proxy for the recovery path: FALLBACK_PROXY, or SCRAPER_PROXY when only that is set.

    Kept apart from `proxy_url` so the ordinary rendered-page path can stay direct while the
    GraphQL fallback is proxied. Proxying the rendered page costs ~1 MB for a page whose ads are
    being withheld anyway; the same search over GraphQL costs ~7 KB an ad.
    """
    return proxy_url(settings.fallback_proxy) if settings.fallback_proxy else proxy_url()


def proxy_url(raw: str | None = None) -> str | None:
    """SCRAPER_PROXY as a URL curl_cffi accepts, or None when unset.

    Two accepted forms. Anything containing '://' is a plain proxy URL and is passed through
    untouched. Otherwise it is the four-field `host:port:user:pass`, split at most THREE times
    because the password may itself contain ':'. Credentials are percent-encoded on the way into
    the URL: a password containing '%', '@' or ':' would otherwise authenticate with the wrong
    value or fail to parse outright.
    """
    raw = (settings.proxy if raw is None else raw) or ""
    raw = raw.strip()
    if not raw:
        return None
    if "://" in raw:
        _reject_rotating_gateway(urlsplit(raw).port)
        return raw
    # Deliberately unguarded: fewer than four fields raises ValueError here, and a config fault
    # should stop the service rather than silently let it run from the blocked address.
    host, port, user, pw = raw.split(":", 3)
    _reject_rotating_gateway(port)
    return f"http://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:{port}"


def _reject_rotating_gateway(port) -> None:
    vendor = ROTATING_GATEWAY_PORTS.get(str(port or ""))
    if vendor:
        raise ValueError(
            f"SCRAPER_PROXY points at {vendor}'s rotating gateway (port {port}). Use a sticky port: "
            f"a session's cookie jar and its exit IP are one identity to Meta."
        )
