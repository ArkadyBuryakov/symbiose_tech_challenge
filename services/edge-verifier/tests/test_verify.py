"""The edge verifier as nginx's auth_request sees it."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport

from pmp_common.cloudfront import CloudFrontSigner
from pmp_edge_verifier.app import EdgeVerifierSettings, build_app

ORIGIN = "http://localhost:8080"


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> tuple[bytes, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    directory = tmp_path_factory.mktemp("keys")
    public = directory / "cloudfront.pub"
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return private, public


@pytest.fixture
def app(keys: tuple[bytes, Path], monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CLOUDFRONT_PUBLIC_KEY_PATH", str(keys[1]))
    monkeypatch.setenv("CLOUDFRONT_KEY_PAIR_ID", "TESTKEY")
    monkeypatch.setenv("PUBLIC_BASE_URL", ORIGIN)
    return build_app(EdgeVerifierSettings())


def cookies_for(private: bytes, resource: str) -> dict[str, str]:
    return (
        CloudFrontSigner(private, key_pair_id="TESTKEY").sign(resource, ttl_seconds=600).as_dict()
    )


async def ask(app, uri: str, cookies: dict[str, str] | None = None) -> int:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://verifier", cookies=cookies or {}
    ) as client:
        response = await client.get("/verify", headers={"x-original-uri": uri})
    return response.status_code


async def test_own_tenant_is_allowed(app, keys) -> None:  # type: ignore[no-untyped-def]
    cookies = cookies_for(keys[0], f"{ORIGIN}/tiles/private/org_a/*")

    status = await ask(app, "/tiles/private/org_a/ds/sha/data.pmtiles", cookies)

    assert status == 204


async def test_other_tenant_is_forbidden(app, keys) -> None:  # type: ignore[no-untyped-def]
    cookies = cookies_for(keys[0], f"{ORIGIN}/tiles/private/org_a/*")

    status = await ask(app, "/tiles/private/org_b/ds/sha/data.pmtiles", cookies)

    assert status == 403


async def test_no_cookie_is_forbidden(app) -> None:  # type: ignore[no-untyped-def]
    assert await ask(app, "/tiles/private/org_a/ds/sha/data.pmtiles") == 403


async def test_platform_admin_scope_covers_every_tenant(app, keys) -> None:  # type: ignore[no-untyped-def]
    cookies = cookies_for(keys[0], f"{ORIGIN}/tiles/private/*")

    assert await ask(app, "/tiles/private/org_a/x/y/data.pmtiles", cookies) == 204
    assert await ask(app, "/tiles/private/org_b/x/y/data.pmtiles", cookies) == 204


async def test_cookie_scoped_to_another_origin_is_forbidden(app, keys) -> None:  # type: ignore[no-untyped-def]
    """A cookie minted for another deployment must not work here."""
    cookies = cookies_for(keys[0], "https://elsewhere.example/tiles/private/org_a/*")

    assert await ask(app, "/tiles/private/org_a/ds/sha/data.pmtiles", cookies) == 403


async def test_range_request_query_strings_do_not_escape_the_scope(app, keys) -> None:  # type: ignore[no-untyped-def]
    cookies = cookies_for(keys[0], f"{ORIGIN}/tiles/private/org_a/*")

    assert await ask(app, "/tiles/private/org_b/x?/tiles/private/org_a/", cookies) == 403
