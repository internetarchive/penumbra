from penumbra.recent import RecentlySent


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_remembers_what_was_added():
    recent = RecentlySent(max_size=10, ttl_seconds=60)
    recent.add("a")
    assert "a" in recent
    assert "b" not in recent


def test_entries_lapse_after_the_ttl():
    clock = FakeClock()
    recent = RecentlySent(max_size=10, ttl_seconds=60, clock=clock)
    recent.add("a")
    clock.now = 59
    assert "a" in recent
    clock.now = 60
    assert "a" not in recent
    assert len(recent) == 0


def test_a_lookup_does_not_extend_the_ttl():
    """Otherwise a hot asset would never lapse across a crawl boundary."""
    clock = FakeClock()
    recent = RecentlySent(max_size=10, ttl_seconds=60, clock=clock)
    recent.add("a")
    clock.now = 50
    assert "a" in recent
    clock.now = 61
    assert "a" not in recent


def test_evicts_the_least_recently_used():
    recent = RecentlySent(max_size=2, ttl_seconds=60)
    recent.add("a")
    recent.add("b")
    assert "a" in recent  # now more recent than "b"
    recent.add("c")
    assert "a" in recent
    assert "b" not in recent
    assert "c" in recent
    assert len(recent) == 2


def test_zero_size_remembers_nothing():
    recent = RecentlySent(max_size=0, ttl_seconds=60)
    recent.add("a")
    assert "a" not in recent
    assert len(recent) == 0
