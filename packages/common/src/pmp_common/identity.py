"""Internal identity: the token the gateway mints and the services trust.

Trust model
-----------
*AuthN at the gateway, AuthZ in the service.*

The gateway is the only component that talks to the auth service. Once it has
established who the caller is, it mints a short-lived (60 s) asymmetrically
signed JWT and forwards it to the upstream service as
``Authorization: Bearer ...``. Upstream services never see the end-user session
cookie or API key, and they verify the token with the gateway's **public** key
only — so a compromised backend cannot mint identities.

A signed token is used instead of plain ``X-User-Id`` style trusted headers
because the services must stay safe even if something else in the cluster can
reach them directly (a misconfigured NetworkPolicy, a port-forward, a sidecar).
The alternatives are recorded in ``docs/DECISIONS.md``.
"""

from __future__ import annotations

import time
from typing import Any, Literal, Self

import jwt
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, Field

from .enums import TenantRole
from .ids import new_uuid

__all__ = [
    "DEFAULT_ALGORITHM",
    "ISSUER",
    "Identity",
    "InternalTokenError",
    "InternalTokenIssuer",
    "InternalTokenVerifier",
]

ISSUER = "gateway"
DEFAULT_ALGORITHM = "EdDSA"  # Ed25519: small keys, fast verification, no parameter choices.
AuthMethod = Literal["session", "api_key"]
_LEEWAY_SECONDS = 5


class InternalTokenError(Exception):
    """The internal token is missing, malformed, expired or not for this audience."""


class Identity(BaseModel):
    """Who the caller is, as asserted by the gateway.

    ``tenant_id`` is ``None`` for a signed-in user with no active organization;
    tenant-scoped endpoints must reject those callers.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    user_id: str = Field(description="Stable auth-service user id.")
    tenant_id: str | None = Field(default=None, description="Active organization id.")
    tenant_role: TenantRole | None = None
    platform_role: str | None = Field(
        default=None, description="'admin' for platform administrators, otherwise None."
    )
    auth_method: AuthMethod = "session"
    jti: str = Field(default="", description="Token id, for log correlation only.")

    @property
    def is_platform_admin(self) -> bool:
        return self.platform_role == "admin"

    @property
    def can_administer_tenant(self) -> bool:
        return self.is_platform_admin or bool(self.tenant_role and self.tenant_role.can_administer)

    def require_tenant(self) -> str:
        """Return the active tenant id or raise; used by tenant-scoped endpoints."""
        if not self.tenant_id:
            raise PermissionError("no active organization for this caller")
        return self.tenant_id

    def log_fields(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "auth_method": self.auth_method,
        }


class InternalTokenIssuer:
    """Mints internal identity tokens. Only the gateway holds the private key."""

    def __init__(
        self,
        private_key_pem: str | bytes,
        *,
        algorithm: str = DEFAULT_ALGORITHM,
        ttl_seconds: int = 60,
        issuer: str = ISSUER,
    ) -> None:
        pem = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
        self._key = serialization.load_pem_private_key(pem, password=None)
        self._algorithm = algorithm
        self._ttl = ttl_seconds
        self._issuer = issuer

    @classmethod
    def from_file(cls, path: str, **kwargs: Any) -> Self:
        from .config import read_secret_file

        return cls(read_secret_file(path, what="internal JWT private key"), **kwargs)

    def mint(self, identity: Identity, *, audience: str) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": audience,
            "sub": identity.user_id,
            "iat": now,
            "exp": now + self._ttl,
            "jti": identity.jti or str(new_uuid()),
            "tenant_id": identity.tenant_id,
            "tenant_role": identity.tenant_role.value if identity.tenant_role else None,
            "platform_role": identity.platform_role,
            "auth_method": identity.auth_method,
        }
        return jwt.encode(claims, self._key, algorithm=self._algorithm)  # type: ignore[arg-type]


class InternalTokenVerifier:
    """Verifies internal identity tokens with the gateway's public key."""

    def __init__(
        self,
        public_key_pem: str | bytes,
        *,
        audience: str,
        algorithm: str = DEFAULT_ALGORITHM,
        issuer: str = ISSUER,
    ) -> None:
        pem = public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
        self._key = serialization.load_pem_public_key(pem)
        self._audience = audience
        self._algorithms = [algorithm]
        self._issuer = issuer

    @classmethod
    def from_file(cls, path: str, **kwargs: Any) -> Self:
        from .config import read_secret_file

        return cls(read_secret_file(path, what="internal JWT public key"), **kwargs)

    def verify(self, token: str) -> Identity:
        try:
            claims = jwt.decode(
                token,
                self._key,  # type: ignore[arg-type]
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InternalTokenError(str(exc)) from exc

        role = claims.get("tenant_role")
        return Identity(
            user_id=str(claims["sub"]),
            tenant_id=claims.get("tenant_id"),
            tenant_role=TenantRole(role) if role else None,
            platform_role=claims.get("platform_role"),
            auth_method=claims.get("auth_method", "session"),
            jti=str(claims.get("jti", "")),
        )
