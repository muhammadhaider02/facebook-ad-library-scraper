"""POST /fetch: a homepage through a lane exit, always answered, never raised."""

import pytest
from fastapi.testclient import TestClient

from facebook_ad_library import api, fetch


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def test_a_fetch_rides_a_lane_exit_and_answers_with_the_text(client, monkeypatch):
    seen = []

    def _fetch(url, proxy, timeout_s, max_bytes):
        seen.append((url, proxy, timeout_s, max_bytes))
        return {"url": url, "ok": True, "status": 200, "final_url": url + "/", "bytes": 12, "text": "<html>hi</html>", "error": None, "seconds": 0.3}

    monkeypatch.setattr(api, "fetch_page", _fetch)
    r = client.post("/fetch", json={"url": "brand.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["text"] == "<html>hi</html>" and body["lane"] == "lane-1" and body["final_url"] == "https://brand.com/"
    assert r.headers["X-Status"] == "ok" and r.headers["X-Lane"] == "lane-1"
    assert seen[0][0] == "https://brand.com" and seen[0][2] == 15 and seen[0][3] == 300_000
    assert api.counters["fetch_requests"] == 1 and api.counters["fetch_ok"] == 1


def test_a_failed_fetch_is_an_answer_not_an_error(client, monkeypatch):
    monkeypatch.setattr(api, "fetch_page", lambda url, proxy, t, m: {"url": url, "ok": False, "status": 403, "final_url": url, "bytes": 0, "text": "", "error": "http 403", "seconds": 0.2})
    r = client.post("/fetch", json={"url": "https://walled.example", "timeout_s": 5, "max_bytes": 1000})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["error"] == "http 403" and r.headers["X-Status"] == "failed"
    assert api.counters["fetch_failed"] == 1


def test_a_malformed_url_is_400(client):
    assert client.post("/fetch", json={"url": ""}).status_code == 400
    assert client.post("/fetch", json={"url": "ftp://x"}).status_code == 400


def test_fetch_page_never_raises(monkeypatch):
    """A dead host is an answer with the exception's name, so the caller's pairing survives."""
    out = fetch.fetch_page("https://127.0.0.1:9", None, 1, 1000)
    assert out["ok"] is False and out["error"] and out["text"] == "" and out["seconds"] >= 0
