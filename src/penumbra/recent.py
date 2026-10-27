from collections import OrderedDict
from collections.abc import Callable, Hashable
from time import monotonic


class RecentlySent:
    """
    Bounded LRU of keys sent within the last `ttl_seconds`.

    Expiry runs from when a key was added, not when it was last looked up, so a
    hot key still lapses. A `max_size` of 0 disables it: nothing is remembered.
    """

    def __init__(
        self,
        max_size: int,
        ttl_seconds: float,
        clock: Callable[[], float] = monotonic,
    ):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._sent_at: OrderedDict[Hashable, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._sent_at)

    def __contains__(self, key: Hashable) -> bool:
        sent_at = self._sent_at.get(key)
        if sent_at is None:
            return False
        if self.clock() - sent_at >= self.ttl_seconds:
            del self._sent_at[key]
            return False
        self._sent_at.move_to_end(key)
        return True

    def add(self, key: Hashable) -> None:
        if self.max_size <= 0:
            return
        self._sent_at[key] = self.clock()
        self._sent_at.move_to_end(key)
        while len(self._sent_at) > self.max_size:
            self._sent_at.popitem(last=False)
