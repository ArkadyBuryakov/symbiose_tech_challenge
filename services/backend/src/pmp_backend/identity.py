"""Who is calling, and may they?

The backend trusts exactly one thing: an internal JWT signed by the gateway's
private key. It never sees session cookies or API keys, and it never trusts a
header such as ``X-User-Id``.

*AuthN at the gateway, AuthZ here.* The gateway has already decided that the
caller is authenticated and allowed to use this route at all; the checks in this
module and in the routers decide whether they may touch *this tenant's* data.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from pmp_common.enums import TenantRole
from pmp_common.identity import Identity, InternalTokenError, InternalTokenVerifier
from pmp_common.logging import bind_log_context
from pmp_common.problem import Forbidden, Unauthorized

from .settings import BackendSettings

__all__ = [
    "CurrentIdentity",
    "OptionalIdentity",
    "PlatformAdmin",
    "TenantAdmin",
    "build_identity_resolver",
    "require_identity",
]

_BEARER = "bearer "


class _Resolver:
    """Extracts an :class:`Identity` from a request."""

    def __init__(self, settings: BackendSettings) -> None:
        self._mode = settings.auth_mode
        self._settings = settings
        self._verifier: InternalTokenVerifier | None = None
        if settings.auth_mode == "internal_jwt":
            self._verifier = InternalTokenVerifier.from_file(
                settings.internal_jwt_public_key_path,
                audience=settings.internal_jwt_audience,
            )

    def __call__(self, request: Request) -> Identity | None:
        if self._mode == "dev_stub":
            return self._stub_identity()

        header = request.headers.get("authorization", "")
        if not header.lower().startswith(_BEARER):
            return None
        assert self._verifier is not None
        try:
            return self._verifier.verify(header[len(_BEARER) :].strip())
        except InternalTokenError as exc:
            # A present-but-invalid internal token is a hard failure: it means
            # something is forging identity, or the gateway's key has rotated.
            raise Unauthorized(
                "The internal identity token is missing, expired or invalid.",
                code="invalid-internal-token",
            ) from exc

    def _stub_identity(self) -> Identity:
        """Development-only fixed identity.

        Used before the gateway and auth service exist (phase 1). Settings
        refuse to start in this mode outside ``ENVIRONMENT=local``.
        """
        return Identity(
            user_id=self._settings.dev_stub_user_id,
            tenant_id=self._settings.dev_stub_tenant_id,
            tenant_role=TenantRole.OWNER,
            platform_role="admin",
            auth_method="session",
        )


def build_identity_resolver(settings: BackendSettings) -> _Resolver:
    return _Resolver(settings)


def _resolver(request: Request) -> _Resolver:
    resolver: _Resolver = request.app.state.identity_resolver
    return resolver


def optional_identity(request: Request) -> Identity | None:
    """Identity if a valid internal token is present, else ``None``.

    Used by the read-only dataset endpoints, which serve public datasets to
    anonymous callers.
    """
    identity = _resolver(request)(request)
    if identity is not None:
        bind_log_context(**identity.log_fields())
    return identity


def require_identity(request: Request) -> Identity:
    identity = optional_identity(request)
    if identity is None:
        raise Unauthorized("This endpoint requires an authenticated caller.")
    return identity


def require_tenant_identity(request: Request) -> Identity:
    """Authenticated *and* scoped to an organization.

    A signed-in user with no active organization has nothing to read or write
    here, and saying so explicitly is better than an empty list.
    """
    identity = require_identity(request)
    if not identity.tenant_id:
        raise Forbidden(
            "This caller has no active organization. Select one before using tenant-scoped "
            "endpoints.",
            code="no-active-organization",
        )
    return identity


def require_platform_admin(request: Request) -> Identity:
    identity = require_identity(request)
    if not identity.is_platform_admin:
        raise Forbidden("Platform administrator role required.", code="not-platform-admin")
    return identity


def require_tenant_admin(request: Request) -> Identity:
    """Tenant ``owner``/``admin``, or a platform administrator."""
    identity = require_tenant_identity(request)
    if not identity.can_administer_tenant:
        raise Forbidden(
            "Tenant owner or admin role required for this operation.",
            code="insufficient-tenant-role",
        )
    return identity


CurrentIdentity = Annotated[Identity, Depends(require_tenant_identity)]
OptionalIdentity = Annotated[Identity | None, Depends(optional_identity)]
PlatformAdmin = Annotated[Identity, Depends(require_platform_admin)]
TenantAdmin = Annotated[Identity, Depends(require_tenant_admin)]
