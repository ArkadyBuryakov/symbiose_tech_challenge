"""RFC 9457 ``application/problem+json`` error bodies.

One error shape across the gateway, backend and edge-verifier, always carrying
the request id so a user-reported failure can be found in the logs.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CONTENT_TYPE",
    "AppError",
    "BadGateway",
    "Conflict",
    "Forbidden",
    "GatewayTimeout",
    "NotFound",
    "Unauthorized",
    "problem_dict",
]

CONTENT_TYPE = "application/problem+json"
_BASE_TYPE = "https://pmtiles.platform/problems"


class AppError(Exception):
    """An error that maps directly onto a problem+json response.

    Services raise this instead of ``HTTPException`` so that the error code is
    machine-readable and the shape is identical everywhere.
    """

    status: int = 500
    code: str = "internal-error"
    title: str = "Internal Server Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        status: int | None = None,
        code: str | None = None,
        title: str | None = None,
        **extra: Any,
    ) -> None:
        self.detail = detail
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        if title is not None:
            self.title = title
        self.extra = extra
        super().__init__(detail or self.title)


def problem_dict(
    *,
    status: int,
    title: str,
    code: str = "error",
    detail: str | None = None,
    instance: str | None = None,
    request_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """RFC 9457 body; ``None`` members are omitted, extra members are allowed."""
    body: dict[str, Any] = {
        "type": f"{_BASE_TYPE}/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": request_id,
        **extra,
    }
    return {k: v for k, v in body.items() if v is not None}


# --- common concrete errors -------------------------------------------------
class Unauthorized(AppError):
    status, code, title = 401, "unauthorized", "Unauthorized"


class Forbidden(AppError):
    status, code, title = 403, "forbidden", "Forbidden"


class NotFound(AppError):
    status, code, title = 404, "not-found", "Not Found"


class Conflict(AppError):
    status, code, title = 409, "conflict", "Conflict"


class BadGateway(AppError):
    status, code, title = 502, "bad-gateway", "Bad Gateway"


class GatewayTimeout(AppError):
    status, code, title = 504, "gateway-timeout", "Gateway Timeout"
