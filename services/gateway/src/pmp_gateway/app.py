"""The gateway.

Single entry point for ``/api/*``. It answers exactly two questions about every
request — *who is this?* and *may they use this route at all?* — and then gets
out of the way. There is no business logic here: tenant ownership and role
permissions are the backend's job.

Pipeline, in order:

1. Request id and trace context (``RequestContextMiddleware`` from the shared
   library).
2. **Strip client-supplied identity headers.** A client must not be able to
   assert who it is.
3. Match the route table. No match is a 404 — the gateway is an allowlist.
4. Verify the credential with the auth service (cached briefly, successes only).
5. Rate limit, by user id when known and by client IP otherwise.
6. Mint a 60-second internal JWT for the chosen upstream.
7. Stream the request and the response through, preserving repeated headers.
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware

from pmp_common.identity import Identity, InternalTokenIssuer
from pmp_common.logging import bind_log_context, configure_logging, get_logger
from pmp_common.tracing import configure_tracing, instrument_fastapi
from pmp_common.web import REQUEST_ID_HEADER, create_app, problem_response, request_id_of

from .proxy import proxy_request
from .ratelimit import RATE_LIMITED, RateLimiter
from .routes import AuthPolicy, RouteTable, load_route_table
from .settings import GatewaySettings, get_settings
from .verify import VerificationError, Verifier, VerifyCache

__all__ = ["build_app"]

log = get_logger(__name__)

PROXY_REQUESTS = Counter(
    "gateway_proxy_requests_total",
    "Requests proxied by the gateway.",
    ["route", "upstream", "status"],
)
PROXY_DURATION = Histogram(
    "gateway_proxy_duration_seconds",
    "Time spent proxying, including the upstream response.",
    ["route", "upstream"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

# Headers a client must never be able to set, because something downstream
# might believe them. `authorization` is stripped per-route (see proxy.py):
# on a public pass-through route it is the *caller's* header and belongs to the
# upstream, everywhere else it is replaced by the internal token.
SPOOFABLE_HEADER_PREFIXES = ("x-user-", "x-tenant-", "x-internal-", "x-platform-")
SPOOFABLE_HEADERS = frozenset(
    {
        "x-user-id",
        "x-tenant-id",
        "x-tenant-role",
        "x-platform-role",
        "x-auth-method",
        "x-internal-token",
        "x-gateway-identity",
    }
)


class _RejectAllCookies(DefaultCookiePolicy):
    """A cookie policy that never stores anything."""

    def set_ok(self, cookie: Cookie, request: urllib.request.Request) -> bool:
        return False

    def return_ok(self, cookie: Cookie, request: urllib.request.Request) -> bool:
        return False


def make_upstream_client(**kwargs: Any) -> httpx.AsyncClient:
    """The shared upstream client, with cookie persistence disabled.

    An ``httpx.AsyncClient`` keeps a cookie jar by default: every ``Set-Cookie``
    that passes through it is stored and then *sent on every later request*.
    In a proxy shared by all callers that turns one user's sign-in into every
    user's session. Cookies must only ever travel as the caller's own
    ``Cookie`` header, forwarded verbatim, so the jar is made unable to hold
    anything.
    """
    return httpx.AsyncClient(cookies=CookieJar(policy=_RejectAllCookies()), **kwargs)


class StripIdentityHeadersMiddleware(BaseHTTPMiddleware):
    """Remove any identity headers the client tried to set.

    Nothing downstream reads these — the backend trusts only the signed internal
    token — but stripping them means a future service that *does* read one
    cannot be tricked by a client, and it keeps the audit story simple: identity
    enters the system at exactly one place.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        raw: list[tuple[bytes, bytes]] = request.scope.get("headers", [])
        kept = [
            (name, value)
            for name, value in raw
            if not _is_spoofable(name.decode("latin-1").lower())
        ]
        if len(kept) != len(raw):
            request.scope["headers"] = kept
            log.warning(
                "gateway.identity_headers_stripped",
                count=len(raw) - len(kept),
                path=request.url.path,
            )
        return await call_next(request)


def _is_spoofable(name: str) -> bool:
    return name in SPOOFABLE_HEADERS or name.startswith(SPOOFABLE_HEADER_PREFIXES)


def build_app(settings: GatewaySettings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(
        service=settings.service_name,
        level=settings.observability.log_level,
        fmt=settings.observability.log_format,
        git_sha=settings.git_sha,
    )
    configure_tracing(
        service=settings.service_name,
        version=settings.git_sha,
        endpoint=settings.observability.otlp_endpoint,
        sample_ratio=settings.observability.trace_sample_ratio,
        environment=settings.environment,
    )

    route_table: RouteTable = load_route_table(settings.routes_file)
    issuer = InternalTokenIssuer.from_file(
        settings.internal_jwt_private_key_path, ttl_seconds=settings.internal_jwt_ttl_seconds
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One pooled client for every upstream: connection reuse is most of the
        # gateway's latency budget.
        client = make_upstream_client(
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.read_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=settings.max_connections,
                max_keepalive_connections=settings.max_keepalive_connections,
            ),
            follow_redirects=False,
        )
        app.state.client = client
        app.state.settings = settings
        app.state.routes = route_table
        app.state.issuer = issuer
        app.state.verifier = Verifier(
            client,
            auth_base_url=settings.auth_base_url,
            cache=VerifyCache(
                ttl_seconds=settings.verify_cache_ttl_seconds,
                max_entries=settings.verify_cache_max_entries,
            ),
            timeout_seconds=settings.verify_timeout_seconds,
        )
        app.state.limiter = RateLimiter(
            rate_per_second=settings.rate_limit_rps,
            burst=settings.rate_limit_burst,
            max_buckets=settings.rate_limit_max_buckets,
        )
        log.info(
            "gateway.started",
            routes=[f"{r.prefix} -> {r.upstream} ({r.policy})" for r in route_table.routes],
            verify_cache_ttl=settings.verify_cache_ttl_seconds,
        )
        try:
            yield
        finally:
            await client.aclose()
            log.info("gateway.stopped")

    async def readiness() -> None:
        """Ready means the auth service is reachable.

        Without it the gateway cannot authenticate anyone, so it should be taken
        out of rotation rather than 503-ing every request individually.
        """
        if not await app.state.verifier.healthy():
            raise RuntimeError("auth service is not reachable")

    app = create_app(
        title="PMTiles Gateway",
        service=settings.service_name,
        version=settings.git_sha,
        readiness=readiness,
        lifespan=lifespan,
        # The gateway exposes no API of its own; a docs page would only be a
        # misleading duplicate of the backend's.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(StripIdentityHeadersMiddleware)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def gateway(request: Request, path: str) -> Response:
        return await _handle(request, settings)

    instrument_fastapi(app)
    return app


async def _handle(request: Request, settings: GatewaySettings) -> Response:
    state = request.app.state
    request_id = request_id_of(request)
    started = time.perf_counter()

    route = state.routes.match(request.url.path, request.method)
    if route is None:
        # Unmatched paths — including /internal/* and /metrics — are simply not
        # here as far as the outside world is concerned.
        return problem_response(
            request,
            status=404,
            title="Not Found",
            code="no-route",
            detail="No route is configured for this path.",
        )

    # --- identity ---------------------------------------------------------
    identity: Identity | None = None
    if route.policy is not AuthPolicy.PUBLIC:
        try:
            identity = await state.verifier.verify(
                cookie_header=request.headers.get("cookie"),
                api_key=request.headers.get("x-api-key"),
                request_id=request_id,
            )
        except VerificationError as exc:
            log.error("gateway.verify_failed", error=str(exc), path=request.url.path)
            return problem_response(
                request,
                status=503,
                title="Service Unavailable",
                code="auth-unavailable",
                detail="Could not verify the caller's identity; try again shortly.",
            )

        if identity is None and route.policy in (
            AuthPolicy.AUTHENTICATED,
            AuthPolicy.PLATFORM_ADMIN,
        ):
            return problem_response(
                request,
                status=401,
                title="Unauthorized",
                code="unauthenticated",
                detail="Sign in, or present a valid API key.",
            )

        if route.policy is AuthPolicy.PLATFORM_ADMIN and not (
            identity and identity.is_platform_admin
        ):
            return problem_response(
                request,
                status=403,
                title="Forbidden",
                code="not-platform-admin",
                detail="This route requires the platform administrator role.",
            )

    if identity is not None:
        bind_log_context(**identity.log_fields())

    # --- rate limit -------------------------------------------------------
    bucket_key = _bucket_key(request, identity, route.rate_limit)
    if not state.limiter.allow(bucket_key):
        RATE_LIMITED.labels(route.rate_limit).inc()
        retry_after = state.limiter.retry_after(bucket_key)
        response = problem_response(
            request,
            status=429,
            title="Too Many Requests",
            code="rate-limited",
            detail="Slow down and retry.",
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    # --- forward ----------------------------------------------------------
    internal_token = None
    if identity is not None and route.policy is not AuthPolicy.PUBLIC:
        internal_token = state.issuer.mint(identity, audience=route.upstream)

    base = settings.upstream_url(route.upstream)
    target = f"{base}{request.url.path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    try:
        proxied: Response = await proxy_request(
            request,
            client=state.client,
            target_url=target,
            internal_token=internal_token,
            request_id=request_id,
        )
    finally:
        PROXY_DURATION.labels(route.prefix, route.upstream).observe(time.perf_counter() - started)

    PROXY_REQUESTS.labels(route.prefix, route.upstream, str(proxied.status_code)).inc()
    proxied.headers[REQUEST_ID_HEADER] = request_id
    return proxied


def client_ip(request: Request) -> str:
    """The caller's address, as established by the edge.

    ``X-Real-IP`` is *overwritten* by the edge with the address of the TCP peer
    it saw, and the edge is the only thing that can reach the gateway, so it is
    trustworthy here. The first ``X-Forwarded-For`` entry is not: nginx appends
    to whatever the client sent, so a client can put any address it likes at
    the front — keying a rate limit on it lets an attacker mint a fresh bucket
    per request.
    """
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def _bucket_key(request: Request, identity: Identity | None, rate_class: str) -> str:
    """Per-user when the caller is known, per-IP otherwise.

    Keying anonymous traffic by IP is imperfect behind a shared NAT, but the
    alternative — one global bucket — lets a single client deny service to
    everyone.
    """
    if identity is not None:
        return f"{rate_class}:u:{identity.user_id}"
    return f"{rate_class}:ip:{client_ip(request)}"
