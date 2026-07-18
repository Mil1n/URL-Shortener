"""Rate limiter abstraction.

The in-memory implementation is used for local development. A production Redis
implementation can be provided behind the same interface without changing the
WSGI routing layer.
"""

from __future__ import annotations

import time


class InMemoryRateLimiter:
    def __init__(self, window_seconds: int):
        self.window_seconds = window_seconds
        self.buckets: dict[tuple[str, str], list[float]] = {}

    def is_limited(self, scope: str, key: str, limit: int) -> bool:
        now = time.monotonic()
        bucket_key = (scope, key)
        bucket = [ts for ts in self.buckets.get(bucket_key, []) if now - ts < self.window_seconds]
        if len(bucket) >= limit:
            self.buckets[bucket_key] = bucket
            return True
        bucket.append(now)
        self.buckets[bucket_key] = bucket
        return False
