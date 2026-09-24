"""Verify-cache behaviour, including the revocation bound it defines."""

from __future__ import annotations

from pmp_common.enums import TenantRole
from pmp_common.identity import Identity
from pmp_gateway.verify import VerifyCache

ALICE = Identity(user_id="u1", tenant_id="t1", tenant_role=TenantRole.OWNER)
BOB = Identity(user_id="u2", tenant_id="t2", tenant_role=TenantRole.MEMBER)


def test_hit_within_the_ttl() -> None:
    cache = VerifyCache(ttl_seconds=10, max_entries=10)
    key = VerifyCache.key("cookie-value")

    cache.put(key, ALICE)

    assert cache.get(key) == ALICE


def test_entry_expires(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The TTL is the advertised revocation bound, so expiry must be real."""
    import time as time_module

    now = [1000.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: now[0])

    cache = VerifyCache(ttl_seconds=10, max_entries=10)
    key = VerifyCache.key("cookie-value")
    cache.put(key, ALICE)

    now[0] += 9.0
    assert cache.get(key) == ALICE

    now[0] += 2.0
    assert cache.get(key) is None


def test_the_credential_is_never_stored_verbatim() -> None:
    """Only a digest of the session token lives in memory."""
    secret = "better-auth.session_token=super-secret-value"
    key = VerifyCache.key(secret)

    assert secret not in key
    assert len(key) == 64
    assert key == VerifyCache.key(secret)


def test_different_credentials_do_not_share_an_entry() -> None:
    cache = VerifyCache(ttl_seconds=10, max_entries=10)
    cache.put(VerifyCache.key("alice-cookie"), ALICE)

    assert cache.get(VerifyCache.key("bob-cookie")) is None


def test_lru_eviction_bounds_memory() -> None:
    """A stream of distinct credentials must not grow the process."""
    cache = VerifyCache(ttl_seconds=60, max_entries=3)
    for i in range(10):
        cache.put(VerifyCache.key(f"cred-{i}"), ALICE)

    assert len(cache) == 3
    assert cache.get(VerifyCache.key("cred-0")) is None
    assert cache.get(VerifyCache.key("cred-9")) == ALICE


def test_reading_an_entry_makes_it_recently_used() -> None:
    cache = VerifyCache(ttl_seconds=60, max_entries=2)
    first, second = VerifyCache.key("a"), VerifyCache.key("b")
    cache.put(first, ALICE)
    cache.put(second, BOB)

    cache.get(first)  # first is now the most recently used
    cache.put(VerifyCache.key("c"), ALICE)

    assert cache.get(first) == ALICE
    assert cache.get(second) is None


def test_ttl_of_zero_disables_caching_entirely() -> None:
    """Setting GATEWAY_VERIFY_CACHE_TTL=0 makes revocation immediate."""
    cache = VerifyCache(ttl_seconds=0, max_entries=10)
    key = VerifyCache.key("cookie")

    cache.put(key, ALICE)

    assert cache.get(key) is None
    assert len(cache) == 0
