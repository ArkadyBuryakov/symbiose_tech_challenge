"""Proxy behaviour: header hygiene and multi-valued header preservation.

These are the bugs that do not show up until production: a dropped
``Set-Cookie`` means users silently cannot stay signed in, and a forwarded
hop-by-hop header is a request-smuggling vector.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from httpx import ASGITransport

from pmp_gateway.proxy import HOP_BY_HOP_HEADERS, proxy_request

RECORDED: dict[str, httpx.Request] = {}


class _Stream(httpx.AsyncByteStream):
    """A real (unconsumed) response stream.

    ``httpx.Response(content=...)`` arrives already consumed, which the proxy's
    ``aiter_raw()`` correctly refuses. Mock upstreams therefore hand back an
    actual stream, the way a real transport does.
    """

    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._data


def reply(
    status: int = 200,
    *,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
) -> httpx.Response:
    return httpx.Response(status, headers=headers or [], stream=_Stream(body))


def upstream_client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    def capture(request: httpx.Request) -> httpx.Response:
        RECORDED["request"] = request
        response: httpx.Response = handler(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(capture))


def make_app(client: httpx.AsyncClient, *, internal_token: str | None = "tok") -> FastAPI:
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def catch_all(request: Request, path: str) -> Response:
        return await proxy_request(
            request,
            client=client,
            target_url=f"http://upstream/{path}",
            internal_token=internal_token,
            request_id="req-1",
        )

    return app


async def call(app: FastAPI, method: str = "GET", path: str = "/x", **kwargs):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        return await client.request(method, path, **kwargs)


# --------------------------------------------------------------------------
# Multi-valued response headers
# --------------------------------------------------------------------------
async def test_every_set_cookie_survives() -> None:
    """A BetterAuth sign-in can set several cookies; losing one breaks sign-in
    in a way that looks like a flaky session bug."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return reply(
            200,
            headers=[
                ("set-cookie", "session=abc; Path=/; HttpOnly"),
                ("set-cookie", "csrf=def; Path=/"),
                ("set-cookie", "org=ghi; Path=/"),
                ("content-type", "application/json"),
            ],
            body=b'{"ok":true}',
        )

    response = await call(make_app(upstream_client(handler)))

    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 3
    assert any(c.startswith("session=") for c in cookies)
    assert any(c.startswith("csrf=") for c in cookies)
    assert any(c.startswith("org=") for c in cookies)


async def test_status_and_body_are_passed_through() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return reply(418, headers=[("content-type", "text/plain")], body=b"teapot")

    response = await call(make_app(upstream_client(handler)))

    assert response.status_code == 418
    assert response.content == b"teapot"


@pytest.mark.parametrize("header", sorted(HOP_BY_HOP_HEADERS - {"transfer-encoding"}))
async def test_hop_by_hop_response_headers_are_removed(header: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return reply(200, headers=[(header, "something")])

    response = await call(make_app(upstream_client(handler)))

    assert header not in {k.lower() for k in response.headers}


# --------------------------------------------------------------------------
# Outbound headers
# --------------------------------------------------------------------------
def ok(_request: httpx.Request) -> httpx.Response:
    return reply(200)


async def test_client_authorization_is_replaced_by_the_internal_token() -> None:
    """The upstream must never see a client-controlled Authorization header on
    a route where it trusts that header."""
    await call(
        make_app(upstream_client(ok)),
        headers={"authorization": "Bearer forged-by-the-client"},
    )

    sent = RECORDED["request"].headers
    assert sent["authorization"] == "Bearer tok"


async def test_public_routes_forward_the_caller_s_authorization_untouched() -> None:
    """With no internal token (a `public` pass-through route) the caller's own
    header belongs to the upstream."""
    await call(
        make_app(upstream_client(ok), internal_token=None),
        headers={"authorization": "Basic abc"},
    )

    assert RECORDED["request"].headers["authorization"] == "Basic abc"


async def test_hop_by_hop_request_headers_are_not_forwarded() -> None:
    await call(
        make_app(upstream_client(ok)),
        headers={"te": "trailers", "upgrade": "websocket", "proxy-authorization": "x"},
    )

    sent = {k.lower() for k in RECORDED["request"].headers}
    assert "te" not in sent
    assert "upgrade" not in sent
    assert "proxy-authorization" not in sent


async def test_forwarding_headers_are_set() -> None:
    await call(make_app(upstream_client(ok)))

    sent = RECORDED["request"].headers
    assert sent["x-request-id"] == "req-1"
    assert "x-forwarded-proto" in sent
    assert "x-forwarded-for" in sent


async def test_client_request_id_is_replaced_not_appended() -> None:
    """Appending would yield "id, id" and break log correlation."""
    await call(make_app(upstream_client(ok)), headers={"x-request-id": "client-supplied"})

    assert RECORDED["request"].headers["x-request-id"] == "req-1"


async def test_existing_forwarded_for_chain_is_extended() -> None:
    await call(make_app(upstream_client(ok)), headers={"x-forwarded-for": "203.0.113.7"})

    assert RECORDED["request"].headers["x-forwarded-for"].startswith("203.0.113.7,")


async def test_request_body_is_forwarded() -> None:
    await call(make_app(upstream_client(ok)), method="POST", content=b"payload")

    assert RECORDED["request"].read() == b"payload"


# --------------------------------------------------------------------------
# Upstream failures
# --------------------------------------------------------------------------
async def test_upstream_timeout_becomes_504() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    app = make_app(upstream_client(handler))

    from pmp_common.problem import GatewayTimeout

    with pytest.raises(GatewayTimeout):
        await call(app)


async def test_upstream_connection_error_becomes_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    from pmp_common.problem import BadGateway

    with pytest.raises(BadGateway):
        await call(make_app(upstream_client(handler)))
