from conftest import Clock

from facebook_ad_library.cache import TTLCache


def test_key_normalises_query_country_and_status():
    assert TTLCache.key("  Running Shoes ", "nz", "Active") == TTLCache.key("running shoes", "NZ") == TTLCache.key("running shoes", "NZ", "active")
    assert TTLCache.key("a", "US") != TTLCache.key("a", "US", "all")


def test_miss_put_hit_and_expiry():
    clock = Clock()
    c = TTLCache(100, clock=clock)
    k = TTLCache.key("a", "US")
    assert c.get(k) is None
    c.put(k, [{"x": 1}])
    assert c.get(k) == [{"x": 1}]
    clock.advance(99)
    assert c.get(k) == [{"x": 1}]
    clock.advance(2)
    assert c.get(k) is None
    assert c.stats() == {"entries": 0, "hits": 2, "misses": 2, "evictions": 0, "expired_dropped": 0}


def test_returned_lists_are_copies():
    c = TTLCache(100)
    k = TTLCache.key("a", "US")
    c.put(k, [1])
    c.get(k).append(2)
    assert c.get(k) == [1]


def test_empty_results_expire_sooner():
    clock = Clock()
    c = TTLCache(1000, empty_ttl_s=10, clock=clock)
    k = TTLCache.key("a", "US")
    c.put(k, [])
    assert c.get(k) == []
    clock.advance(11)
    assert c.get(k) is None


def test_eviction_drops_the_soonest_expiring():
    clock = Clock()
    c = TTLCache(100, max_entries=2, clock=clock)
    c.put(("a",), [1])
    clock.advance(1)
    c.put(("b",), [2])
    clock.advance(1)
    c.put(("c",), [3])
    assert c.get(("a",)) is None and c.get(("b",)) == [2] and c.get(("c",)) == [3]
    assert c.stats()["evictions"] == 1 and c.stats()["entries"] == 2


def test_expired_entries_are_swept_on_a_later_put_without_being_read():
    """00 never reads a key twice: dead entries must leave on their own (the 26 Sep 2026 OOM)."""
    clock = Clock()
    c = TTLCache(60, clock=clock)
    for i in range(50):
        c.put(("q", i), [i] * 30)
    clock.advance(61)
    c.put(("fresh",), [1])
    assert c.stats()["entries"] == 1 and c.stats()["expired_dropped"] == 50
    assert c.get(("fresh",)) == [1]


def test_clear():
    c = TTLCache(100)
    c.put(("a",), [1])
    c.clear()
    assert c.get(("a",)) is None and c.stats()["misses"] == 1
