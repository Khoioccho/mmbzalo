from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], Deque[float]] = defaultdict(deque)

    def allow(self, *, action: str, key: str, limit: int, window_seconds: int = 3600) -> bool:
        if limit <= 0:
            return False

        now = time.monotonic()
        cutoff = now - window_seconds
        bucket_key = (action, key)
        with self._lock:
            bucket = self._hits[bucket_key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


rate_limiter = InMemoryRateLimiter()
