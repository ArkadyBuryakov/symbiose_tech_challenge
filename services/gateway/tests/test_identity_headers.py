"""Client-supplied identity headers must never reach an upstream."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport

from pmp_gateway.app import StripIdentityHeadersMiddleware


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(StripIdentityHeadersMiddleware)

    @application.get("/echo")
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"headers": sorted(k.lower() for k in request.headers)})

    return application


async def seen_headers(app: FastAPI, headers: dict[str, str]) -> set[str]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.get("/echo", headers=headers)
    return set(response.json()["headers"])


@pytest.mark.parametrize(
    "header",
    [
        "x-user-id",
        "x-tenant-id",
        "x-tenant-role",
        "x-platform-role",
        "x-auth-method",
        "x-internal-token",
        "x-gateway-identity",
        "x-user-email",  # matched by the x-user- prefix rule
        "x-tenant-anything",  # matched by the x-tenant- prefix rule
        "x-internal-secret",
    ],
)
async def test_identity_headers_are_stripped(app: FastAPI, header: str) -> None:
    seen = await seen_headers(app, {header: "attacker-controlled"})

    assert header not in seen


async def test_ordinary_headers_are_left_alone(app: FastAPI) -> None:
    seen = await seen_headers(
        app,
        {
            "x-request-id": "abc",
            "x-api-key": "pmp_key",
            "content-type": "application/json",
            "x-forwarded-for": "203.0.113.1",
        },
    )

    assert {"x-request-id", "x-api-key", "content-type", "x-forwarded-for"} <= seen


async def test_stripping_is_case_insensitive(app: FastAPI) -> None:
    seen = await seen_headers(app, {"X-Tenant-Id": "org_other"})

    assert "x-tenant-id" not in seen


async def test_a_request_with_no_identity_headers_is_unchanged(app: FastAPI) -> None:
    seen = await seen_headers(app, {"accept": "application/json"})

    assert "accept" in seen


# --------------------------------------------------------------------------
# Rate-limit keying must not trust client-controlled addresses
# --------------------------------------------------------------------------
def _request(headers: dict[str, str], peer: str = "10.0.0.5") -> object:
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 12345),
    }
    return StarletteRequest(scope)


def test_client_ip_prefers_the_edge_s_x_real_ip() -> None:
    from pmp_gateway.app import client_ip

    request = _request({"x-real-ip": "203.0.113.9", "x-forwarded-for": "1.2.3.4, 203.0.113.9"})

    assert client_ip(request) == "203.0.113.9"  # type: ignore[arg-type]


def test_a_forged_x_forwarded_for_does_not_change_the_bucket() -> None:
    """A client can put anything at the front of X-Forwarded-For. If that chose
    the rate-limit bucket, rotating it would evade the limit entirely."""
    from pmp_gateway.app import _bucket_key

    a = _request({"x-real-ip": "203.0.113.9", "x-forwarded-for": "1.1.1.1, 203.0.113.9"})
    b = _request({"x-real-ip": "203.0.113.9", "x-forwarded-for": "2.2.2.2, 203.0.113.9"})

    assert _bucket_key(a, None, "read") == _bucket_key(b, None, "read")  # type: ignore[arg-type]


def test_client_ip_falls_back_to_the_socket_peer() -> None:
    from pmp_gateway.app import client_ip

    assert client_ip(_request({}, peer="10.0.0.7")) == "10.0.0.7"  # type: ignore[arg-type]
