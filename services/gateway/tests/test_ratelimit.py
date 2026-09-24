"""Token-bucket behaviour."""

from __future__ import annotations

from pmp_gateway.ratelimit import RateLimiter


def a_limiter(rate: float = 10.0, burst: int = 5, max_buckets: int = 100) -> RateLimiter:
    return RateLimiter(rate_per_second=rate, burst=burst, max_buckets=max_buckets)


def test_burst_is_allowed_then_refused() -> None:
    limiter = a_limiter(rate=10, burst=5)

    assert all(limiter.allow("u1", now=100.0) for _ in range(5))
    assert not limiter.allow("u1", now=100.0)


def test_tokens_refill_over_time() -> None:
    limiter = a_limiter(rate=8, burst=5)
    for _ in range(5):
        limiter.allow("u1", now=100.0)
    assert not limiter.allow("u1", now=100.0)

    # 0.25s at 8/s refills exactly 2 tokens (chosen to be exact in binary
    # floating point, so the test is about the algorithm, not about rounding).
    assert limiter.allow("u1", now=100.25)
    assert limiter.allow("u1", now=100.25)
    assert not limiter.allow("u1", now=100.25)


def test_refill_is_capped_at_the_burst_size() -> None:
    limiter = a_limiter(rate=10, burst=5)
    limiter.allow("u1", now=100.0)

    for _ in range(5):
        assert limiter.allow("u1", now=1_000.0)
    assert not limiter.allow("u1", now=1_000.0)


def test_buckets_are_independent_per_caller() -> None:
    """One noisy tenant must not starve everyone else."""
    limiter = a_limiter(rate=10, burst=2)
    assert limiter.allow("u1", now=100.0)
    assert limiter.allow("u1", now=100.0)
    assert not limiter.allow("u1", now=100.0)

    assert limiter.allow("u2", now=100.0)


def test_bucket_count_is_bounded() -> None:
    """An attacker cycling identities must not grow the process."""
    limiter = a_limiter(max_buckets=4)
    for i in range(50):
        limiter.allow(f"caller-{i}", now=100.0)

    assert len(limiter) == 4


def test_retry_after_is_at_least_one_second() -> None:
    limiter = a_limiter(rate=1, burst=1)
    limiter.allow("u1", now=100.0)

    assert limiter.retry_after("u1") >= 1


def test_retry_after_reflects_the_deficit() -> None:
    limiter = a_limiter(rate=1, burst=4)
    for _ in range(4):
        limiter.allow("u1", now=100.0)

    # Empty bucket at 1 token/s: roughly a second to get one back.
    assert limiter.retry_after("u1") == 1
