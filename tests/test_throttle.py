"""The throttle memory: skipping a GET that is known to be useless, without latching."""

from conftest import Clock, set_frozen

from facebook_ad_library.config import settings
from facebook_ad_library.throttle import Throttle


def test_nothing_is_skipped_until_a_page_is_withheld():
    t = Throttle(ttl_s=600, clock=Clock())
    assert not t.active()


def test_one_withheld_page_suppresses_the_direct_get():
    """Measured 24 Sep 2026: 24 of the 24 searches with ads to give came back empty in one cycle,
    so the second search need not re-learn what the first established."""
    t = Throttle(ttl_s=600, clock=Clock())
    t.seen()
    assert t.active() and t.active()
    assert t.skipped == 2


def test_the_memory_expires_so_the_direct_path_is_re_probed():
    """It must not latch: while it is set every search pays the proxy, so being wrong costs money."""
    c = Clock()
    t = Throttle(ttl_s=600, clock=c)
    t.seen()
    c.advance(599)
    assert t.active()
    c.advance(2)
    assert not t.active(), "after the window one search goes direct again to see if Meta stopped"


def test_a_direct_page_with_ads_clears_it_at_once():
    t = Throttle(ttl_s=600, clock=Clock())
    t.seen()
    t.clear()
    assert not t.active()


def test_a_zero_window_disables_the_shortcut():
    t = Throttle(ttl_s=0, clock=Clock())
    t.seen()
    assert not t.active(), "0 means always check direct first"


def test_the_window_follows_the_setting_when_not_pinned():
    t = Throttle(clock=Clock())
    before = settings.throttle_memory_s
    try:
        set_frozen(settings, "throttle_memory_s", 5)
        assert t.ttl == 5
    finally:
        set_frozen(settings, "throttle_memory_s", before)


def test_the_snapshot_reports_what_it_has_seen():
    c = Clock()
    t = Throttle(ttl_s=600, clock=c)
    t.seen()
    c.advance(30)
    s = t.snapshot()
    assert s["active"] and s["seconds_since_seen"] == 30 and s["observations"] == 1
