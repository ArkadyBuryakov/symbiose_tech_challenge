"""Kafka event schemas.

Every event carries a common envelope. ``schema_version`` is an integer that
is bumped when a payload changes incompatibly; consumers must reject versions
they do not understand rather than guess.

Correlation (``traceparent``, ``x-request-id``) travels in Kafka *headers*,
not in the body, so it stays uniform across topics and is cheap to read.

JSON Schema for these models is exported to ``docs/events/`` by
``scripts/export-event-schemas.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import ErrorCode, JobStatus, PublicationResult, Visibility
from .ids import new_uuid

__all__ = [
    "SCHEMA_VERSION",
    "EventEnvelope",
    "PublicationFailed",
    "PublicationRequested",
    "PublicationResultEvent",
    "PublicationSucceeded",
    "decode_event",
    "encode_event",
]

SCHEMA_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EventEnvelope(BaseModel):
    """Fields shared by every event on every topic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(
        default_factory=new_uuid,
        description="Unique id of this event; consumers deduplicate on it.",
    )
    event_type: str = Field(description="Dotted event name, e.g. 'publication.requested'.")
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    occurred_at: datetime = Field(default_factory=_utcnow)

    tenant_id: str = Field(description="Owning organization id.")
    dataset_id: UUID = Field(description="Dataset the event is about; also the partition key.")
    job_id: UUID = Field(description="Publication job the event belongs to.")


class PublicationRequested(EventEnvelope):
    """A client asked for a staged object to be published.

    Emitted by the backend through the transactional outbox path (and re-emitted
    by the worker's reconciler), consumed by the worker.
    """

    event_type: Literal["publication.requested"] = "publication.requested"

    dataset_slug: str
    source_key: str = Field(description="Key of the staged object in the staging bucket.")
    visibility: Visibility
    requested_by: str = Field(description="User id that requested the publication.")
    idempotency_key: str
    attempt: int = Field(default=1, ge=1, description="1 on first emit; >1 when re-emitted.")


class PublicationSucceeded(EventEnvelope):
    event_type: Literal["publication.succeeded"] = "publication.succeeded"
    status: Literal[JobStatus.SUCCEEDED] = JobStatus.SUCCEEDED

    result: PublicationResult
    version_id: UUID
    version_seq: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    object_key: str
    visibility: Visibility


class PublicationFailed(EventEnvelope):
    event_type: Literal["publication.failed"] = "publication.failed"
    status: Literal[JobStatus.FAILED] = JobStatus.FAILED

    error_code: ErrorCode
    error_message: str = Field(max_length=2000)
    attempts: int = Field(ge=1)
    dead_lettered: bool = False


PublicationResultEvent = Annotated[
    PublicationSucceeded | PublicationFailed,
    Field(discriminator="event_type"),
]

AnyEvent = Annotated[
    PublicationRequested | PublicationSucceeded | PublicationFailed,
    Field(discriminator="event_type"),
]


def encode_event(event: EventEnvelope) -> bytes:
    """Serialise an event to compact UTF-8 JSON for a Kafka value."""
    return event.model_dump_json(by_alias=True).encode("utf-8")


def decode_event[T: EventEnvelope](model: type[T], raw: bytes | str) -> T:
    """Parse and validate a Kafka value, rejecting unknown schema versions."""
    data: dict[str, Any] = json.loads(raw)
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version {version!r} for {model.__name__}; "
            f"this build understands {SCHEMA_VERSION}"
        )
    return model.model_validate(data)
