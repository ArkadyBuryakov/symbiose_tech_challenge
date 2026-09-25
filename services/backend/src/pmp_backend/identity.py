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

from pmp_common.identity import Identity, InternalTokenError, InternalTokenVerifier
from pmp_common.logging import bind_log_context
from pmp_common.problem import Forbidden, Unauthorized

from .settings import BackendSettings

__all__ = [
    "CurrentIdentity",
    "OptionalIdentity",
    "PlatformAdmin",
    "TenantAdmin",
    "TenantOrPlatformAdmin",
    "build_token_verifier",
]

_BEARER = "bearer "


def build_token_verifier(settings: BackendSettings) -> InternalTokenVerifier:
    return InternalTokenVerifier.from_file(
        settings.internal_jwt_public_key_path, audience=settings.internal_jwt_audience
    )


def optional_identity(request: Request) -> Identity | None:
    """Identity if a valid internal token is present, else ``None``.

    Used by the read-only dataset endpoints, which serve public datasets to
    anonymous callers. A present-but-invalid token is a hard 401: it means
    something is forging identity, or the gateway's key has rotated.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith(_BEARER):
        return None
    verifier: InternalTokenVerifier = request.app.state.token_verifier
    try:
        identity = verifier.verify(header[len(_BEARER) :].strip())
    except InternalTokenError as exc:
        raise Unauthorized(
            "The internal identity token is missing, expired or invalid.",
            code="invalid-internal-token",
        ) from exc
    bind_log_context(**identity.log_fields())
    return identity


def require_identity(request: Request) -> Identity:
    identity = optional_identity(request)
    if identity is None:
        raise Unauthorized("This endpoint requires an authenticated caller.")
    return identity


def require_tenant_identity(request: Request) -> Identity:
    """Authenticated *and* scoped to an organization.

    Handlers behind this dependency may rely on ``identity.tenant_id`` being set.
    """
    identity = require_identity(request)
    if not identity.tenant_id:
        raise Forbidden(
            "This caller has no active organization. Select one before using tenant-scoped "
            "endpoints.",
            code="no-active-organization",
        )
    return identity


def require_tenant_or_platform_admin(request: Request) -> Identity:
    """A tenant member, or a platform administrator who belongs to no tenant."""
    identity = require_identity(request)
    if identity.is_platform_admin:
        return identity
    return require_tenant_identity(request)


def require_platform_admin(request: Request) -> Identity:
    identity = require_identity(request)
    if not identity.is_platform_admin:
        raise Forbidden("Platform administrator role required.", code="not-platform-admin")
    return identity


def require_tenant_admin(request: Request) -> Identity:
    """Tenant ``owner``/``admin`` of the caller's active organization."""
    identity = require_tenant_identity(request)
    if not identity.can_administer_tenant:
        raise Forbidden(
            "Tenant owner or admin role required for this operation.",
            code="insufficient-tenant-role",
        )
    return identity


CurrentIdentity = Annotated[Identity, Depends(require_tenant_identity)]
TenantOrPlatformAdmin = Annotated[Identity, Depends(require_tenant_or_platform_admin)]
OptionalIdentity = Annotated[Identity | None, Depends(optional_identity)]
PlatformAdmin = Annotated[Identity, Depends(require_platform_admin)]
TenantAdmin = Annotated[Identity, Depends(require_tenant_admin)]
