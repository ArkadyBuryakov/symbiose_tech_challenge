"""Streaming reverse proxy.

Bodies are streamed in both directions, so uploading a multi-gigabyte archive
through the gateway costs a buffer, not a copy of the file.

Two details matter more than they look:

* **Hop-by-hop headers are removed** in both directions (RFC 9110 §7.6.1).
  Forwarding ``Connection`` or ``Transfer-Encoding`` to a different connection
  is a protocol violation and a request-smuggling vector.
* **Headers are handled as a multi-valued list, never a dict.** Collapsing them
  into a mapping silently drops all but one ``Set-Cookie``, and a BetterAuth
  sign-in sets several. That bug is invisible until someone cannot stay signed
  in, so it has a dedicated test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from pmp_common.logging import get_logger
from pmp_common.problem import BadGateway, GatewayTimeout

__all__ = ["HOP_BY_HOP_HEADERS", "proxy_request"]

log = get_logger(__name__)

# RFC 9110 §7.6.1 — meaningful only for a single transport connection.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Headers the gateway itself owns on the way out; whatever the client sent is
# replaced, never appended to.
_GATEWAY_OWNED = frozenset(
    {
        "host",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        # Replaced, not appended: forwarding the client's copy as well would
        # produce "id, id" and break log correlation.
        "x-request-id",
    }
)


def _outbound_headers(
    request: Request, *, internal_token: str | None, request_id: str
) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for name, value in request.headers.items():
        lowered = name.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered in _GATEWAY_OWNED:
            continue
        if lowered.startswith("proxy-"):
            continue
        if internal_token is not None and lowered == "authorization":
            # Replaced below by the internal token; a client-supplied
            # Authorization header must never reach an upstream that trusts it.
            continue
        headers.append((name, value))

    client_host = request.client.host if request.client else ""
    forwarded_for = request.headers.get("x-forwarded-for")
    chain = f"{forwarded_for}, {client_host}" if forwarded_for else client_host

    headers.append(("x-forwarded-for", chain))
    headers.append(("x-forwarded-proto", request.url.scheme))
    headers.append(("x-forwarded-host", request.headers.get("host", "")))
    headers.append(("x-request-id", request_id))
    if internal_token is not None:
        headers.append(("authorization", f"Bearer {internal_token}"))
    return headers


def _response_headers(upstream: httpx.Response) -> list[tuple[str, str]]:
    """Copy upstream headers, preserving repeats such as ``Set-Cookie``."""
    return [
        (name, value)
        for name, value in upstream.headers.multi_items()
        if name.lower() not in HOP_BY_HOP_HEADERS
    ]


async def proxy_request(
    request: Request,
    *,
    client: httpx.AsyncClient,
    target_url: str,
    internal_token: str | None,
    request_id: str,
) -> StreamingResponse:
    """Forward ``request`` to ``target_url`` and stream the response back."""
    upstream_request = client.build_request(
        method=request.method,
        url=target_url,
        headers=_outbound_headers(request, internal_token=internal_token, request_id=request_id),
        content=request.stream(),
    )

    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        raise GatewayTimeout(
            "The upstream service did not respond in time.", upstream=target_url
        ) from exc
    except httpx.HTTPError as exc:
        raise BadGateway(
            f"Could not reach the upstream service: {type(exc).__name__}.",
            upstream=target_url,
        ) from exc

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            # Closing here (rather than only in a BackgroundTask) guarantees the
            # connection returns to the pool even if the client disconnects.
            await upstream.aclose()

    response = StreamingResponse(body(), status_code=upstream.status_code)
    # Assigning `raw_headers` rather than passing `headers=` is deliberate:
    # Starlette's `headers` parameter is a *mapping*, so a sign-in response with
    # three `Set-Cookie` headers would arrive at the browser with one.
    response.raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in _response_headers(upstream)
    ]
    return response
