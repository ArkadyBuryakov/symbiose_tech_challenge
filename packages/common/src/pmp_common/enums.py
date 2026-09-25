"""Domain enumerations shared by the API, the worker and the event schemas.

These values are persisted in Postgres and serialised into Kafka events, so
they are part of the contract: add members, never rename or remove them.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ErrorCode",
    "JobStatus",
    "PublicationResult",
    "TenantRole",
    "VersionStatus",
    "Visibility",
]


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PublicationResult(StrEnum):
    """What a successful publication actually did to the catalogue."""

    CREATED = "CREATED"
    DEDUPLICATED = "DEDUPLICATED"
    POINTER_MOVED = "POINTER_MOVED"


class VersionStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    RETIRED = "RETIRED"


class TenantRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

    @property
    def can_administer(self) -> bool:
        return self in (TenantRole.OWNER, TenantRole.ADMIN)


class ErrorCode(StrEnum):
    """Stable, machine-readable failure reasons recorded on a job.

    Whether a failure is retried is decided by the worker's exception type
    (``PermanentJobError`` vs ``TransientJobError``), not by the code.
    """

    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_FORBIDDEN = "SOURCE_FORBIDDEN"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    INVALID_PMTILES = "INVALID_PMTILES"
    EMPTY_SOURCE = "EMPTY_SOURCE"
    STORAGE_ERROR = "STORAGE_ERROR"
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
