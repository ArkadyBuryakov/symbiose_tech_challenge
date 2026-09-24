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
