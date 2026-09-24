"""Identity verification against the auth service, with a short-lived cache.

The gateway is the only component that talks to the auth service. Every
request on a non-public route costs one ``GET /internal/verify`` unless the
same credential was verified very recently.

Cache design, and why each choice matters:

* **Keyed by SHA-256 of the credential.** The cache never holds a session token
  or an API key in memory or in a log line, only a digest of one.
* **Only successes are cached.** Caching a failure would let a transient auth
  outage lock a user out for the whole TTL.
* **TTL is the revocation bound.** A revoked session keeps working for at most
  ``GATEWAY_VERIFY_CACHE_TTL`` (default 10 s). That number is a deliberate
  trade-off and is documented as such in ``docs/DECISIONS.md``.
* **Bounded LRU.** An attacker sending a stream of distinct credentials must
  not be able to grow the process's memory.

The cache is per-replica and in-memory. With several gateway replicas the bound
is unchanged (each replica caches independently); moving it to Redis would only
be needed to cut the load on the auth service, and is noted as the next step.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx
from prometheus_client import Counter

from pmp_common.enums import TenantRole
from pmp_common.identity import Identity
from pmp_common.logging import get_logger

__all__ = ["VerificationError", "Verifier", "VerifyCache"]

log = get_logger(__name__)

VERIFY_CACHE = Counter(
    "gateway_verify_cache_total",
    "Identity verification cache outcomes.",
    ["result"],  # hit | miss
)
VERIFY_CALLS = Counter(
    "gateway_verify_calls_total",
    "Calls to the auth service's /internal/verify.",
    ["outcome"],  # ok | unauthenticated | error
)

SESSION_COOKIE_PREFIX = "better-auth"
API_KEY_HEADER = "x-api-key"


class VerificationError(Exception):
    """The auth service could not be reached or answered unexpectedly."""


@dataclass(frozen=True, slots=True)
class _Entry:
    identity: Identity
    expires_at: float


class VerifyCache:
    """Bounded, TTL'd LRU of successful verifications."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    @staticmethod
    def key(credential: str) -> str:
        return hashlib.sha256(credential.encode()).hexdigest()

    def get(self, key: str) -> Identity | None:
        if self._ttl <= 0:
            return None
        entry = self._entries.get(key)
        if entry is None:
            VERIFY_CACHE.labels("miss").inc()
            return None
        if entry.expires_at <= time.monotonic():
            del self._entries[key]
            VERIFY_CACHE.labels("miss").inc()
            return None
        self._entries.move_to_end(key)
        VERIFY_CACHE.labels("hit").inc()
        return entry.identity

    def put(self, key: str, identity: Identity) -> None:
        if self._ttl <= 0:
            return
        self._entries[key] = _Entry(identity, time.monotonic() + self._ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class Verifier:
    """Calls the auth service and caches successful answers."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        auth_base_url: str,
        cache: VerifyCache,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._url = f"{auth_base_url.rstrip('/')}/internal/verify"
        self._cache = cache
        self._timeout = timeout_seconds

    async def verify(
        self,
        *,
        cookie_header: str | None,
        api_key: str | None,
        request_id: str,
    ) -> Identity | None:
        """Return the caller's identity, or ``None`` if the credential is invalid.

        Raises :class:`VerificationError` when the auth service is unreachable —
        which is a 503, not a 401: "we cannot tell who you are" is a different
        answer from "you are nobody".
        """
        credential = None
        if api_key:
            credential = f"k:{api_key}"
        elif cookie_header and SESSION_COOKIE_PREFIX in cookie_header:
            credential = f"c:{cookie_header}"
        if credential is None:
            return None

        cache_key = VerifyCache.key(credential)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        headers = {"x-request-id": request_id}
        if api_key:
            headers[API_KEY_HEADER] = api_key
        elif cookie_header:
            headers["cookie"] = cookie_header

        try:
            response = await self._client.get(self._url, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            VERIFY_CALLS.labels("error").inc()
            raise VerificationError(f"auth service unreachable: {exc}") from exc

        if response.status_code == 401:
            # Deliberately not cached: a failure must not outlive the request.
            VERIFY_CALLS.labels("unauthenticated").inc()
            return None
        if response.status_code != 200:
            VERIFY_CALLS.labels("error").inc()
            raise VerificationError(
                f"auth service returned {response.status_code} from /internal/verify"
            )

        identity = _identity_from(response.json())
        VERIFY_CALLS.labels("ok").inc()
        self._cache.put(cache_key, identity)
        return identity

    async def healthy(self) -> bool:
        try:
            response = await self._client.get(
                self._url.replace("/internal/verify", "/healthz"), timeout=self._timeout
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200


def _identity_from(payload: dict[str, object]) -> Identity:
    role = payload.get("tenant_role")
    return Identity(
        user_id=str(payload["user_id"]),
        tenant_id=str(payload["tenant_id"]) if payload.get("tenant_id") else None,
        tenant_role=TenantRole(str(role)) if role else None,
        platform_role=str(payload["platform_role"]) if payload.get("platform_role") else None,
        auth_method="api_key" if payload.get("auth_method") == "api_key" else "session",
    )
