"""Homepage fetches through a lane's exit: `POST /fetch`.

Stage 0 verifies a sourced brand by reading its homepage, and 21 % of those reads fail from the
VPS (measured 24-25 Sep 2026: 103 of 499, half of them 403s from bot walls that see a bare
Node.js request from a data-centre address). The lanes already hold residential exits and a
Chrome TLS profile, so this fetches the page the way a browser on a home connection would and
hands the text back. It is NOT a lane try: no Facebook cookie jar, no limiter, no throttle
memory, no cooldown - a homepage that refuses is the brand's problem, not the exit's.

The call never raises. A failed fetch is an answer (`ok: false`, the status and the error), so a
caller pairing pages to brands by position never loses a slot.
"""

from __future__ import annotations

import logging
import time

from .config import settings

log = logging.getLogger(__name__)


def normalise_url(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        raise ValueError("invalid request: `url` is required")
    if "://" not in u:
        u = "https://" + u.lstrip("/")
    if not u.lower().startswith(("http://", "https://")):
        raise ValueError(f"invalid request: unsupported url {u!r}")
    return u


def fetch_page(url: str, proxy: str | None, timeout_s: float, max_bytes: int) -> dict:
    """One GET with the Chrome profile through `proxy`. Returns a dict, never raises."""
    from curl_cffi import requests

    started = time.time()
    out = {"url": url, "ok": False, "status": None, "final_url": None, "bytes": 0, "text": "", "error": None, "seconds": 0.0}
    try:
        kwargs = {"impersonate": settings.impersonate, "timeout": timeout_s, "allow_redirects": True,
                  "headers": {"Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}}
        if proxy:
            kwargs["proxy"] = proxy
        r = requests.get(url, **kwargs)
        text = r.text or ""
        out.update(status=int(r.status_code), final_url=str(r.url or url), bytes=len(r.content or b""), text=text[:max_bytes])
        out["ok"] = 200 <= int(r.status_code) < 400 and bool(text.strip())
        if not out["ok"] and out["error"] is None:
            out["error"] = f"http {r.status_code}" if text.strip() else f"http {r.status_code}, empty body"
    except Exception as e:  # noqa: BLE001 - the whole point is to answer, not raise
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    out["seconds"] = round(time.time() - started, 2)
    return out
