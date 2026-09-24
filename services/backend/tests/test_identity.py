"""The backend's trust boundary: only gateway-signed tokens create an identity."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport

from pmp_backend.identity import (
    CurrentIdentity,
    OptionalIdentity,
    PlatformAdmin,
    TenantAdmin,
    TenantOrPlatformAdmin,
    build_identity_resolver,
)
from pmp_backend.settings import BackendSettings
from pmp_common.enums import TenantRole
from pmp_common.identity import Identity, InternalTokenIssuer
from pmp_common.web import create_app

KeyPair = tuple[bytes, Path]


@pytest.fixture(scope="module")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> KeyPair:
    key = Ed25519PrivateKey.generate()
    public = tmp_path_factory.mktemp("keys") / "internal-jwt.pub"
    public.write_bytes(
        key.public_key().public_bytes(ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo)
    )
    private = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    return private, public


@pytest.fixture
def app(keypair: KeyPair, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("DB_PASSWORD", "unused")
    monkeypatch.setenv("INTERNAL_JWT_PUBLIC_KEY_PATH", str(keypair[1]))
    monkeypatch.setenv("BACKEND_AUTH_MODE", "internal_jwt")
    settings = BackendSettings()

    application = create_app(title="t", service="backend-test")
    application.state.identity_resolver = build_identity_resolver(settings)

    @application.get("/optional")
    async def optional(identity: OptionalIdentity) -> dict[str, str | None]:
        return {"user": identity.user_id if identity else None}

    @application.get("/tenant")
    async def tenant(identity: CurrentIdentity) -> dict[str, str | None]:
        return {"tenant": identity.tenant_id}

    @application.get("/tenant-admin")
    async def tenant_admin(identity: TenantAdmin) -> dict[str, str]:
        return {"ok": identity.user_id}

    @application.get("/platform-admin")
    async def platform_admin(identity: PlatformAdmin) -> dict[str, str]:
        return {"ok": identity.user_id}

    @application.get("/tenant-or-admin")
    async def tenant_or_admin(identity: TenantOrPlatformAdmin) -> dict[str, str]:
        return {"ok": identity.user_id}

    return application


def token(private: bytes, identity: Identity, audience: str = "backend") -> str:
    return InternalTokenIssuer(private).mint(identity, audience=audience)


async def get(app: FastAPI, path: str, bearer: str | None = None, **headers: str) -> httpx.Response:
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://b") as c:
        return await c.get(path, headers=headers)


OWNER = Identity(user_id="u-owner", tenant_id="org_a", tenant_role=TenantRole.OWNER)
MEMBER = Identity(user_id="u-member", tenant_id="org_a", tenant_role=TenantRole.MEMBER)
ORPHAN = Identity(user_id="u-orphan")
ADMIN = Identity(user_id="u-admin", platform_role="admin")


async def test_no_token_is_anonymous_on_optional_routes(app: FastAPI) -> None:
    response = await get(app, "/optional")

    assert response.status_code == 200
    assert response.json() == {"user": None}


async def test_no_token_is_401_on_tenant_routes(app: FastAPI) -> None:
    assert (await get(app, "/tenant")).status_code == 401


async def test_identity_headers_without_a_token_are_ignored(app: FastAPI) -> None:
    """The backend never reads X-User-Id and friends — only the signed token."""
    response = await get(app, "/tenant", **{"x-user-id": "admin", "x-tenant-id": "org_a"})

    assert response.status_code == 401


async def test_a_valid_token_yields_the_tenant(app: FastAPI, keypair: KeyPair) -> None:
    response = await get(app, "/tenant", token(keypair[0], OWNER))

    assert response.status_code == 200
    assert response.json() == {"tenant": "org_a"}


async def test_a_token_minted_for_another_audience_is_rejected(
    app: FastAPI, keypair: KeyPair
) -> None:
    response = await get(app, "/tenant", token(keypair[0], OWNER, audience="auth"))

    assert response.status_code == 401
    assert response.json()["type"].endswith("/invalid-internal-token")


async def test_a_forged_token_is_401_even_on_optional_routes(app: FastAPI) -> None:
    """A present-but-invalid token is an attack, not an anonymous caller."""
    forged_key = Ed25519PrivateKey.generate().private_bytes(
        ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()
    )

    response = await get(app, "/optional", token(forged_key, ADMIN))

    assert response.status_code == 401


async def test_a_user_without_an_organization_cannot_use_tenant_routes(
    app: FastAPI, keypair: KeyPair
) -> None:
    response = await get(app, "/tenant", token(keypair[0], ORPHAN))

    assert response.status_code == 403
    assert response.json()["type"].endswith("/no-active-organization")


async def test_members_are_not_tenant_admins(app: FastAPI, keypair: KeyPair) -> None:
    assert (await get(app, "/tenant-admin", token(keypair[0], MEMBER))).status_code == 403
    assert (await get(app, "/tenant-admin", token(keypair[0], OWNER))).status_code == 200


async def test_platform_admin_role_is_rechecked_here(app: FastAPI, keypair: KeyPair) -> None:
    """The gateway enforces it too, but one control is not a control."""
    assert (await get(app, "/platform-admin", token(keypair[0], OWNER))).status_code == 403
    assert (await get(app, "/platform-admin", token(keypair[0], ADMIN))).status_code == 200


async def test_platform_admin_without_a_tenant_is_admitted_where_scope_depends_on_role(
    app: FastAPI,
    keypair: KeyPair,
) -> None:
    """Regression: /tiles/session refused platform admins for having no tenant."""
    assert (await get(app, "/tenant-or-admin", token(keypair[0], ADMIN))).status_code == 200
    assert (await get(app, "/tenant-or-admin", token(keypair[0], OWNER))).status_code == 200
    assert (await get(app, "/tenant-or-admin", token(keypair[0], ORPHAN))).status_code == 403


def test_dev_stub_mode_refuses_to_start_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "unused")
    monkeypatch.setenv("BACKEND_AUTH_MODE", "dev_stub")
    monkeypatch.setenv("ENVIRONMENT", "prod")

    with pytest.raises(ValueError, match="dev_stub is only allowed"):
        BackendSettings()
