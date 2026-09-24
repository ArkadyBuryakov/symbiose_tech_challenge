"""Internal identity tokens: the trust boundary between gateway and services."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pmp_common.enums import TenantRole
from pmp_common.identity import (
    Identity,
    InternalTokenError,
    InternalTokenIssuer,
    InternalTokenVerifier,
)

ALICE = Identity(
    user_id="user_alice",
    tenant_id="org_a",
    tenant_role=TenantRole.OWNER,
    platform_role=None,
    auth_method="session",
)


def issuer(private: bytes, **kw: object) -> InternalTokenIssuer:
    return InternalTokenIssuer(private, **kw)  # type: ignore[arg-type]


def test_round_trip_preserves_every_claim(ed25519_keypair: tuple[bytes, bytes]) -> None:
    private, public = ed25519_keypair

    token = issuer(private).mint(ALICE, audience="backend")
    got = InternalTokenVerifier(public, audience="backend").verify(token)

    assert got.user_id == "user_alice"
    assert got.tenant_id == "org_a"
    assert got.tenant_role is TenantRole.OWNER
    assert got.platform_role is None
    assert got.auth_method == "session"
    assert got.jti


def test_token_for_another_upstream_is_rejected(ed25519_keypair: tuple[bytes, bytes]) -> None:
    """A token minted for the backend must not be replayable against the auth service."""
    private, public = ed25519_keypair
    token = issuer(private).mint(ALICE, audience="backend")

    with pytest.raises(InternalTokenError):
        InternalTokenVerifier(public, audience="auth").verify(token)


def test_expired_token_is_rejected(ed25519_keypair: tuple[bytes, bytes]) -> None:
    private, public = ed25519_keypair
    token = issuer(private, ttl_seconds=-60).mint(ALICE, audience="backend")

    with pytest.raises(InternalTokenError, match=r"[Ee]xpired"):
        InternalTokenVerifier(public, audience="backend").verify(token)


def test_token_signed_by_a_different_key_is_rejected(
    ed25519_keypair: tuple[bytes, bytes],
) -> None:
    """The attack this defends against: something inside the cluster forging identity."""
    _, public = ed25519_keypair
    attacker = Ed25519PrivateKey.generate().private_bytes(
        ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()
    )
    token = issuer(attacker).mint(ALICE, audience="backend")

    with pytest.raises(InternalTokenError):
        InternalTokenVerifier(public, audience="backend").verify(token)


def test_unsigned_alg_none_token_is_rejected(ed25519_keypair: tuple[bytes, bytes]) -> None:
    _, public = ed25519_keypair
    forged = jwt.encode(
        {
            "iss": "gateway",
            "aud": "backend",
            "sub": "user_alice",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "platform_role": "admin",
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(InternalTokenError):
        InternalTokenVerifier(public, audience="backend").verify(forged)


def test_token_from_an_unexpected_issuer_is_rejected(
    ed25519_keypair: tuple[bytes, bytes],
) -> None:
    private, public = ed25519_keypair
    token = issuer(private, issuer="not-the-gateway").mint(ALICE, audience="backend")

    with pytest.raises(InternalTokenError):
        InternalTokenVerifier(public, audience="backend").verify(token)


def test_user_without_an_active_organization(ed25519_keypair: tuple[bytes, bytes]) -> None:
    private, public = ed25519_keypair
    orphan = Identity(user_id="user_nobody", tenant_id=None, auth_method="session")

    got = InternalTokenVerifier(public, audience="backend").verify(
        issuer(private).mint(orphan, audience="backend")
    )

    assert got.tenant_id is None
    with pytest.raises(PermissionError):
        got.require_tenant()


def test_role_helpers() -> None:
    admin = Identity(user_id="u", platform_role="admin")
    member = Identity(user_id="u", tenant_id="t", tenant_role=TenantRole.MEMBER)
    tenant_admin = Identity(user_id="u", tenant_id="t", tenant_role=TenantRole.ADMIN)

    assert admin.is_platform_admin and admin.can_administer_tenant
    assert not member.is_platform_admin and not member.can_administer_tenant
    assert tenant_admin.can_administer_tenant
