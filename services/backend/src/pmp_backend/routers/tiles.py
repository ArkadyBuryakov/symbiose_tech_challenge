"""Signed tile cookies for private datasets.

A private archive is read with dozens of HTTP range requests to one URL, so it
is authorised with CloudFront signed *cookies* rather than signed URLs: one
signature covers every read, and the object URL stays stable (and cacheable).

The cookie's policy is scoped to ``/tiles/private/{tenant_id}/*`` — the whole
tenant, not one dataset — so a map page can switch between a tenant's private
datasets without re-issuing. Platform administrators get ``/tiles/private/*``.

Revocation: the cookie is valid until it expires (``TILE_COOKIE_TTL_SECONDS``,
default 10 minutes) and there is no revocation list, exactly as with CloudFront.
What *is* immediate is that a revoked session can no longer obtain a fresh
cookie, because this endpoint sits behind the gateway's identity check.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from pmp_common.cloudfront import CloudFrontSigner
from pmp_common.logging import get_logger

from ..deps import Settings
from ..identity import TenantOrPlatformAdmin
from ..schemas import TileSession

__all__ = ["COOKIE_PATH", "router"]

router = APIRouter(prefix="/tiles", tags=["tiles"])
log = get_logger(__name__)

# The cookies are only ever needed on private tile requests; scoping their path
# keeps them off every API call and static asset.
COOKIE_PATH = "/tiles/private/"


def _signer(request: Request) -> CloudFrontSigner:
    signer: CloudFrontSigner = request.app.state.tile_signer
    return signer


@router.post(
    "/session",
    response_model=TileSession,
    summary="Issue signed cookies for this tenant's private tiles",
)
async def create_tile_session(
    request: Request,
    response: Response,
    identity: TenantOrPlatformAdmin,
    settings: Settings,
) -> TileSession:
    if identity.is_platform_admin:
        scope = f"{settings.public_base_url}{COOKIE_PATH}*"
    else:
        scope = f"{settings.public_base_url}{COOKIE_PATH}{identity.require_tenant()}/*"

    cookies = _signer(request).sign(scope, ttl_seconds=settings.tile_cookie_ttl_seconds)
    secure = settings.public_base_url.startswith("https://")

    for name, value in cookies.as_dict().items():
        response.set_cookie(
            name,
            value,
            max_age=settings.tile_cookie_ttl_seconds,
            path=COOKIE_PATH,
            httponly=True,
            secure=secure,
            samesite="lax",
        )

    # The response must never be cached: it carries credentials in Set-Cookie.
    response.headers["Cache-Control"] = "no-store"
    log.info("tiles.session_issued", scope=scope, ttl=settings.tile_cookie_ttl_seconds)
    return TileSession(
        resource=scope,
        expires_at=cookies.expires_at,
        expires_in=settings.tile_cookie_ttl_seconds,
    )
