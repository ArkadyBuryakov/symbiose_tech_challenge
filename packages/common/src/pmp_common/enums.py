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

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


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

    ``PERMANENT_*`` codes never retry; anything else is treated as transient by
    the worker and retried with backoff before being dead-lettered.
    """

    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_FORBIDDEN = "SOURCE_FORBIDDEN"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    INVALID_PMTILES = "INVALID_PMTILES"
    UNSUPPORTED_PMTILES_VERSION = "UNSUPPORTED_PMTILES_VERSION"
    EMPTY_SOURCE = "EMPTY_SOURCE"
    DATASET_NOT_FOUND = "DATASET_NOT_FOUND"
    STORAGE_ERROR = "STORAGE_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    @property
    def is_permanent(self) -> bool:
        return self in _PERMANENT


_PERMANENT = frozenset(
    {
        ErrorCode.SOURCE_NOT_FOUND,
        ErrorCode.SOURCE_FORBIDDEN,
        ErrorCode.TENANT_MISMATCH,
        ErrorCode.INVALID_PMTILES,
        ErrorCode.UNSUPPORTED_PMTILES_VERSION,
        ErrorCode.EMPTY_SOURCE,
        ErrorCode.DATASET_NOT_FOUND,
    }
)
