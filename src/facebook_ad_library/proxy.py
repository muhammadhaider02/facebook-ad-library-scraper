"""Proxy credentials, and since 0.4.0 the sticky ports the lanes run on.

One guard has already bitten the sibling service and is kept loudly: every residential vendor's
dashboard hands out the ROTATING gateway by default, and a rotating exit under a session whose
cookie jar Meta ties to an IP is the pattern that gets that session refused. Rotating ports are
rejected at config time, not debugged later as a mysterious block.

Lanes (lanes.py) each hold ONE sticky port. DataImpulse binds an exit IP to a port in 10000-20000
for the rotation interval set in its dashboard (1-120 minutes; production asks for 120 so a jar
and its IP live and die together with SESSION_MAX_AGE_S). The ports beyond LANE_COUNT are the
reserve a blocked lane rotates onto for a fresh exit.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from .config import settings

# Keyed by port because that is all the credential tells us. Decodo 7000, DataImpulse 823.
ROTATING_GATEWAY_PORTS = {"7000": "Decodo", "823": "DataImpulse"}


def fallback_proxy_url() -> str | None:
    """The retired recovery exit: FALLBACK_PROXY, or SCRAPER_PROXY when only that is set. Kept for
    the legacy GraphQL session used by `page_search` without a lane (CLI probes and tests)."""
    return proxy_url(settings.fallback_proxy) if settings.fallback_proxy else proxy_url()


def proxy_url(raw: str | None = None) -> str | None:
    """A proxy credential as a URL curl_cffi accepts, or None when unset.

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
            f"proxy points at {vendor}'s rotating gateway (port {port}). Use a sticky port: "
            f"a session's cookie jar and its exit IP are one identity to Meta."
        )


# --------------------------------------------------------------------------- lanes


def parse_ports(spec: str) -> list[int]:
    """`11510-11513,11540` -> [11510, 11511, 11512, 11513, 11540]. Order kept, duplicates refused."""
    ports: list[int] = []
    for part in str(spec or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (p.strip() for p in part.split("-", 1))
            a, b = int(lo), int(hi)
            if b < a:
                raise ValueError(f"LANE_PROXY_PORTS range {part!r} runs backwards")
            ports.extend(range(a, b + 1))
        else:
            ports.append(int(part))
    seen: set[int] = set()
    for p in ports:
        if p in seen:
            raise ValueError(f"LANE_PROXY_PORTS lists port {p} twice; two lanes must never share an exit")
        seen.add(p)
        _reject_rotating_gateway(p)
    return ports


@dataclass(frozen=True)
class LaneProxy:
    """One exit a lane can run on: the port (None for a spec without one) and the proxy URL
    (None only when LANE_REQUIRE_PROXY is off, i.e. CI's direct lane)."""

    port: int | None
    url: str | None


def lane_proxies() -> list[LaneProxy]:
    """Every exit the configuration provides, lanes first, reserve after. Raises ValueError for
    anything that would silently run a lane on the wrong address."""
    n = int(settings.lane_count)
    if n < 1:
        raise ValueError("LANE_COUNT must be at least 1: every request to Facebook goes through a lane")
    template = settings.lane_proxy_template
    if template:
        if "{port}" not in template:
            raise ValueError("LANE_PROXY_TEMPLATE must contain `{port}`; each lane fills in its own sticky port")
        ports = parse_ports(settings.lane_proxy_ports)
        if not ports:
            raise ValueError("LANE_PROXY_TEMPLATE is set but LANE_PROXY_PORTS is empty")
        exits = [LaneProxy(p, template.replace("{port}", str(p))) for p in ports]
    elif settings.lane_proxies:
        exits = []
        for raw in settings.lane_proxies.split(","):
            raw = raw.strip()
            if not raw:
                continue
            url = proxy_url(raw)
            exits.append(LaneProxy(urlsplit(url).port if url else None, url))
        if len({e.url for e in exits}) != len(exits):
            raise ValueError("LANE_PROXIES lists the same proxy twice; two lanes must never share an exit")
    else:
        if settings.lane_require_proxy:
            raise ValueError(
                "no lane proxy configured: set LANE_PROXY_TEMPLATE + LANE_PROXY_PORTS (or LANE_PROXIES). "
                "Lanes never use this host's own address; LANE_REQUIRE_PROXY=false is for CI only."
            )
        exits = [LaneProxy(None, None) for _ in range(n)]
    if len(exits) < n:
        raise ValueError(f"LANE_COUNT={n} but only {len(exits)} proxy exit(s) configured; a lane never shares an exit")
    return exits


class PortAllocator:
    """Hands exits to lanes under a lock, so two lanes can never hold the same one. `rotate` gives
    a lane the oldest reserve exit and puts its old one at the back of the reserve."""

    def __init__(self, exits: list[LaneProxy]) -> None:
        self._free: list[LaneProxy] = list(exits)
        self._held: dict[int, LaneProxy] = {}
        self._lock = threading.Lock()

    def take(self, lane_id: int) -> LaneProxy:
        with self._lock:
            if lane_id in self._held:
                return self._held[lane_id]
            if not self._free:
                raise ValueError("no free proxy exit for a new lane")
            e = self._free.pop(0)
            self._held[lane_id] = e
            return e

    def rotate(self, lane_id: int) -> LaneProxy | None:
        """A fresh exit for the lane, or None when there is no reserve (the lane then re-mints on
        the exit it has)."""
        with self._lock:
            if not self._free:
                return None
            old = self._held.get(lane_id)
            new = self._free.pop(0)
            self._held[lane_id] = new
            if old is not None:
                self._free.append(old)
            return new

    def snapshot(self) -> dict:
        with self._lock:
            return {"held": {k: v.port for k, v in self._held.items()}, "reserve": [e.port for e in self._free]}
