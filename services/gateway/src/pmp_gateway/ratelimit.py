"""In-memory token-bucket rate limiting.

Buckets are keyed by user id when the caller is identified and by client IP
otherwise, so one noisy tenant cannot starve the rest and an anonymous flood is
still bounded.

Scope, stated plainly: this limiter is **per replica**. With N gateway replicas
the effective limit is N times the configured rate, which is fine as a blunt
abuse control and is not a quota system. Moving to a shared counter (Redis with
`INCR`/`EXPIRE`, or a sliding window) is the next step once there is more than
one replica and the limit needs to mean something exact; the interface here does
not change when that happens.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass

from prometheus_client import Counter

__all__ = ["RATE_LIMITED", "RateLimiter"]

RATE_LIMITED = Counter(
    "gateway_rate_limited_total",
    "Requests rejected by the gateway's rate limiter.",
    ["rate_limit_class"],
)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class RateLimiter:
    """Token bucket: ``rate`` tokens per second, up to ``burst`` in reserve."""

    def __init__(self, *, rate_per_second: float, burst: int, max_buckets: int = 50_000) -> None:
        self._rate = rate_per_second
        self._burst = float(burst)
        self._max_buckets = max_buckets
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Consume one token for ``key``; False when the bucket is empty."""
        now = time.monotonic() if now is None else now
        bucket = self._buckets.get(key)

        if bucket is None:
            bucket = _Bucket(tokens=self._burst, updated_at=now)
            self._buckets[key] = bucket
            # Evicting the least-recently-used bucket bounds memory. An evicted
            # caller gets a full bucket back, so eviction is generous rather
            # than punitive — acceptable, because the memory bound is the point.
            while len(self._buckets) > self._max_buckets:
                self._buckets.popitem(last=False)
        else:
            elapsed = max(now - bucket.updated_at, 0.0)
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.updated_at = now
            self._buckets.move_to_end(key)

        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until a token is available, for `Retry-After`."""
        bucket = self._buckets.get(key)
        if bucket is None or self._rate <= 0:
            return 1
        return max(1, math.ceil((1 - bucket.tokens) / self._rate))

    def __len__(self) -> int:
        return len(self._buckets)
