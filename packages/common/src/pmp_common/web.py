"""Shared FastAPI wiring: request context, metrics, problem+json, probes.

Every Python HTTP service in the platform is built with :func:`create_app`, so
they all behave identically for request ids, error shape, logging and
``/healthz`` + ``/readyz`` + ``/metrics``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from .ids import new_uuid
from .logging import bind_log_context, clear_log_context, get_logger
from .metrics import CONTENT_TYPE_LATEST, HTTP_REQUEST_DURATION, HTTP_REQUESTS, render_metrics
from .problem import CONTENT_TYPE, AppError, problem_dict

__all__ = [
    "REQUEST_ID_HEADER",
    "ReadinessCheck",
    "RequestContextMiddleware",
    "create_app",
    "problem_response",
    "request_id_of",
]

REQUEST_ID_HEADER = "x-request-id"
log = get_logger(__name__)

ReadinessCheck = Callable[[], Awaitable[None]]
_PROBE_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


def request_id_of(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else ""


def problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    code: str,
    detail: str | None = None,
    **extra: Any,
) -> JSONResponse:
    body = problem_dict(
        status=status,
        title=title,
        code=code,
        detail=detail,
        instance=str(request.url.path),
        request_id=request_id_of(request) or None,
        **extra,
    )
    return JSONResponse(
        body,
        status_code=status,
        media_type=CONTENT_TYPE,
        headers={REQUEST_ID_HEADER: request_id_of(request)},
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds log context and records HTTP metrics."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(new_uuid())
        request.state.request_id = request_id
        clear_log_context()
        bind_log_context(request_id=request_id)

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(request)
            if route not in _PROBE_PATHS:
                HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
                HTTP_REQUEST_DURATION.labels(request.method, route).observe(elapsed)
                log.info(
                    "http.request",
                    method=request.method,
                    path=request.url.path,
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                )


def _route_template(request: Request) -> str:
    """Use the route *pattern* as the metric label so cardinality stays bounded."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else request.url.path


def create_app(
    *,
    title: str,
    service: str,
    version: str = "unknown",
    readiness: ReadinessCheck | None = None,
    expose_metrics: bool = True,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Build a FastAPI app with the platform's standard behaviour."""
    app = FastAPI(title=title, version=version, **fastapi_kwargs)
    app.state.service = service
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        problem = exc.to_problem(
            instance=str(request.url.path), request_id=request_id_of(request) or None
        )
        if exc.status >= 500:
            log.error("request.failed", code=exc.code, detail=exc.detail)
        return JSONResponse(
            problem.model_dump(exclude_none=True),
            status_code=exc.status,
            media_type=CONTENT_TYPE,
            headers={REQUEST_ID_HEADER: request_id_of(request)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            request,
            status=exc.status_code,
            title=str(exc.detail) if exc.status_code < 500 else "Internal Server Error",
            code="http-error",
            detail=str(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            request,
            status=422,
            title="Unprocessable Entity",
            code="validation-error",
            detail="Request payload failed validation.",
            errors=[
                {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
                for e in exc.errors()
            ],
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("request.unhandled_error", error=type(exc).__name__)
        return problem_response(
            request,
            status=500,
            title="Internal Server Error",
            code="internal-error",
            detail="An unexpected error occurred.",
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up. Never touches dependencies."""
        return {"status": "ok", "service": service, "version": version}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        """Readiness: the dependencies this service cannot serve without are up."""
        if readiness is None:
            return JSONResponse({"status": "ok"})
        try:
            await readiness()
        except Exception as exc:
            log.warning("readiness.failed", error=str(exc))
            return JSONResponse({"status": "unavailable", "detail": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok"})

    if expose_metrics:

        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> Response:
            return PlainTextResponse(render_metrics(), media_type=CONTENT_TYPE_LATEST)

    return app
