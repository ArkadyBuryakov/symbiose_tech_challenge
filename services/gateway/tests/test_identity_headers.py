"""Client-supplied identity headers must never reach an upstream."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from httpx import ASGITransport

from pmp_gateway.proxy import proxy_request


class _Stream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b""


@pytest.fixture
def seen() -> list[httpx.Request]:
    return []


@pytest.fixture
def app(seen: list[httpx.Request]) -> FastAPI:
    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=_Stream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    application = FastAPI()

    @application.get("/echo")
    async def echo(request: Request) -> Response:
        return await proxy_request(
            request,
            client=client,
            target_url="http://upstream/echo",
            internal_token=None,
            request_id="r",
        )

    return application


async def seen_headers(
    app: FastAPI, seen: list[httpx.Request], headers: dict[str, str]
) -> set[str]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        await client.get("/echo", headers=headers)
    return {k.lower() for k in seen[-1].headers}


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
async def test_identity_headers_are_stripped(app, seen, header: str) -> None:  # type: ignore[no-untyped-def]
    assert header not in await seen_headers(app, seen, {header: "attacker-controlled"})


async def test_ordinary_headers_are_left_alone(app, seen) -> None:  # type: ignore[no-untyped-def]
    forwarded = await seen_headers(
        app,
        seen,
        {"x-api-key": "pmp_key", "content-type": "application/json", "accept": "text/plain"},
    )

    assert {"x-api-key", "content-type", "accept"} <= forwarded


async def test_stripping_is_case_insensitive(app, seen) -> None:  # type: ignore[no-untyped-def]
    assert "x-tenant-id" not in await seen_headers(app, seen, {"X-Tenant-Id": "org_other"})


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

    assert _bucket_key(a, None) == _bucket_key(b, None)  # type: ignore[arg-type]


def test_client_ip_falls_back_to_the_socket_peer() -> None:
    from pmp_gateway.app import client_ip

    assert client_ip(_request({}, peer="10.0.0.7")) == "10.0.0.7"  # type: ignore[arg-type]
