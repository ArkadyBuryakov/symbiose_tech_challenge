"""Request and response models.

Every endpoint declares an explicit response model, so the generated OpenAPI
document is the API contract rather than a description of whatever the handlers
happened to return.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pmp_common.enums import ErrorCode, JobStatus, PublicationResult, VersionStatus, Visibility

__all__ = [
    "CreatePublicationRequest",
    "CurrentVersionResponse",
    "DatasetDetail",
    "DatasetSummary",
    "DemoUploadRequest",
    "DemoUploadResponse",
    "JobResponse",
    "Page",
    "PublicationAccepted",
    "RollbackRequest",
    "VersionSummary",
]

# A dataset slug is part of a public URL and of the S3 key prefix, so it is
# restricted to a conservative, case-insensitive-safe character set.
Slug = Annotated[
    str,
    Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$"),
]
# The layer/style spec is caller-supplied and deliberately open-ended; the
# frontend reads it, the platform only stores and serves it.
Spec = dict[str, Any]


class Page[T](BaseModel):
    """Cursor-free page. Datasets and jobs are small, bounded collections."""

    items: list[T]
    total: int = Field(description="Total rows matching the filter, ignoring limit/offset.")
    limit: int
    offset: int


# --------------------------------------------------------------------------
# Publications
# --------------------------------------------------------------------------
class CreatePublicationRequest(BaseModel):
    """Ask the platform to publish an object that is already in staging."""

    model_config = ConfigDict(extra="forbid")

    dataset_slug: Slug = Field(
        description="Identifies the dataset within the tenant. Created on first use."
    )
    source_key: str = Field(
        min_length=1,
        max_length=1024,
        description="Key of the staged object. Must be under the caller's tenant prefix.",
    )
    name: str | None = Field(
        default=None, max_length=200, description="Display name, used only when creating."
    )
    visibility: Visibility | None = Field(
        default=None,
        description="Only honoured when the dataset is created; change it explicitly afterwards.",
    )
    spec: Spec | None = Field(
        default=None, description="Layer/style spec stored with the resulting version."
    )

    @field_validator("source_key")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("source_key must be a plain object key without '..' segments")
        return value


class PublicationAccepted(BaseModel):
    """202 response: the work is queued, not done."""

    job_id: UUID
    dataset_id: UUID
    status: JobStatus
    idempotent_replay: bool = Field(
        default=False,
        description="True when this Idempotency-Key had already been used; no new job was created.",
    )


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: str
    dataset_id: UUID
    status: JobStatus
    source_key: str
    idempotency_key: str
    attempts: int
    result: PublicationResult | None = None
    result_version_id: UUID | None = None
    result_version_seq: int | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    requested_by: str
    trace_id: str | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Datasets and versions
# --------------------------------------------------------------------------
class DatasetSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: str
    slug: str
    name: str
    visibility: Visibility
    latest_seq: int
    current_seq: int | None = None
    created_at: datetime
    updated_at: datetime


class VersionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seq: int
    sha256: str
    size_bytes: int
    status: VersionStatus
    is_current: bool
    job_id: UUID
    worker_build: str | None = None
    created_at: datetime


class DatasetDetail(DatasetSummary):
    current_version: VersionSummary | None = None


class CurrentVersionResponse(BaseModel):
    """What the map client needs to render a dataset.

    ``url`` is an edge path to the immutable object; it changes only when the
    dataset's current version changes, which is what lets the tile archive be
    cached forever while this response is cached for ~30 seconds.
    """

    dataset_id: UUID
    seq: int
    sha256: str
    size_bytes: int
    url: str
    visibility: Visibility
    spec: Spec | None = None
    pmtiles_header: dict[str, Any]
    created_at: datetime


class RollbackRequest(BaseModel):
    """Move the dataset pointer to an existing AVAILABLE version."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1, description="Sequence number of the version to make current.")


# --------------------------------------------------------------------------
# Demo upload
# --------------------------------------------------------------------------
class DemoUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Hex SHA-256 the browser computed; bound into the presigned PUT.",
    )
    content_length: int | None = Field(default=None, ge=1)


class DemoUploadResponse(BaseModel):
    upload_id: UUID
    source_key: str
    url: str = Field(description="Presigned PUT URL, rewritten to go through the edge.")
    headers: dict[str, str] = Field(
        description="Headers the browser MUST send verbatim, or the signature will not match."
    )
    expires_in: int
