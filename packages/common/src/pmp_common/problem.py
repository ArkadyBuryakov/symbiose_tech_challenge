"""RFC 9457 ``application/problem+json`` error bodies.

One error shape across the gateway, backend and edge-verifier, always carrying
the request id so a user-reported failure can be found in the logs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CONTENT_TYPE", "AppError", "Problem", "problem_dict"]

CONTENT_TYPE = "application/problem+json"
_BASE_TYPE = "https://pmtiles.platform/problems"


class Problem(BaseModel):
    """RFC 9457 problem detail. Extra members are allowed by the RFC."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(default="about:blank", description="URI identifying the problem type.")
    title: str = Field(description="Short, human-readable summary, stable per type.")
    status: int = Field(ge=100, le=599)
    detail: str | None = Field(default=None, description="Explanation specific to this occurrence.")
    instance: str | None = Field(default=None, description="URI of this specific occurrence.")


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

    def to_problem(self, *, instance: str | None = None, request_id: str | None = None) -> Problem:
        return Problem(
            type=f"{_BASE_TYPE}/{self.code}",
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
            request_id=request_id,
            **self.extra,
        )


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
    """Build a problem+json body without needing an exception instance."""
    return Problem(
        type=f"{_BASE_TYPE}/{code}",
        title=title,
        status=status,
        detail=detail,
        instance=instance,
        request_id=request_id,
        **extra,
    ).model_dump(exclude_none=True)


# --- common concrete errors -------------------------------------------------
class BadRequest(AppError):
    status, code, title = 400, "bad-request", "Bad Request"


class Unauthorized(AppError):
    status, code, title = 401, "unauthorized", "Unauthorized"


class Forbidden(AppError):
    status, code, title = 403, "forbidden", "Forbidden"


class NotFound(AppError):
    status, code, title = 404, "not-found", "Not Found"


class Conflict(AppError):
    status, code, title = 409, "conflict", "Conflict"


class UnprocessableEntity(AppError):
    status, code, title = 422, "unprocessable-entity", "Unprocessable Entity"


class TooManyRequests(AppError):
    status, code, title = 429, "too-many-requests", "Too Many Requests"


class BadGateway(AppError):
    status, code, title = 502, "bad-gateway", "Bad Gateway"


class ServiceUnavailable(AppError):
    status, code, title = 503, "service-unavailable", "Service Unavailable"


class GatewayTimeout(AppError):
    status, code, title = 504, "gateway-timeout", "Gateway Timeout"


__all__ += [
    "BadGateway",
    "BadRequest",
    "Conflict",
    "Forbidden",
    "GatewayTimeout",
    "NotFound",
    "ServiceUnavailable",
    "TooManyRequests",
    "Unauthorized",
    "UnprocessableEntity",
]
