"""Event envelope contract: strict, versioned, and safe to evolve."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from pmp_common.enums import ErrorCode, PublicationResult, Visibility
from pmp_common.events import (
    SCHEMA_VERSION,
    PublicationFailed,
    PublicationRequested,
    PublicationSucceeded,
    decode_event,
    encode_event,
)


def a_request(**overrides: object) -> PublicationRequested:
    base: dict[str, object] = {
        "tenant_id": "org_a",
        "dataset_id": uuid4(),
        "job_id": uuid4(),
        "dataset_slug": "crowns",
        "source_key": "org_a/upload-1/data.pmtiles",
        "visibility": Visibility.PUBLIC,
        "requested_by": "user_alice",
        "idempotency_key": "key-1",
    }
    return PublicationRequested(**(base | overrides))


def test_requested_round_trip() -> None:
    event = a_request()

    decoded = decode_event(PublicationRequested, encode_event(event))

    assert decoded == event
    assert decoded.event_type == "publication.requested"
    assert decoded.schema_version == SCHEMA_VERSION
    assert decoded.attempt == 1


def test_occurred_at_is_timezone_aware() -> None:
    assert a_request().occurred_at.tzinfo is not None


def test_unknown_fields_are_rejected() -> None:
    """``extra='forbid'`` keeps a producer typo from silently vanishing."""
    with pytest.raises(ValidationError):
        a_request(souce_key="typo")


def test_future_schema_version_is_refused_loudly() -> None:
    raw = json.loads(encode_event(a_request()))
    raw["schema_version"] = SCHEMA_VERSION + 1

    with pytest.raises(ValueError, match="unsupported schema_version"):
        decode_event(PublicationRequested, json.dumps(raw))


def test_success_event_carries_the_catalogue_outcome() -> None:
    event = PublicationSucceeded(
        tenant_id="org_a",
        dataset_id=uuid4(),
        job_id=uuid4(),
        result=PublicationResult.CREATED,
        version_id=uuid4(),
        version_seq=1,
        sha256="a" * 64,
        size_bytes=1234,
        object_key="public/org_a/ds/aaa/data.pmtiles",
        visibility=Visibility.PUBLIC,
    )

    decoded = decode_event(PublicationSucceeded, encode_event(event))

    assert decoded.status == "SUCCEEDED"
    assert decoded.result is PublicationResult.CREATED


def test_failure_event_carries_a_machine_readable_code() -> None:
    event = PublicationFailed(
        tenant_id="org_a",
        dataset_id=uuid4(),
        job_id=uuid4(),
        error_code=ErrorCode.INVALID_PMTILES,
        error_message="bad magic",
        attempts=1,
    )

    decoded = decode_event(PublicationFailed, encode_event(event))

    assert decoded.error_code is ErrorCode.INVALID_PMTILES
    assert decoded.dead_lettered is False


def test_sha256_length_is_enforced() -> None:
    with pytest.raises(ValidationError):
        PublicationSucceeded(
            tenant_id="org_a",
            dataset_id=uuid4(),
            job_id=uuid4(),
            result=PublicationResult.CREATED,
            version_id=uuid4(),
            version_seq=1,
            sha256="short",
            size_bytes=1,
            object_key="k",
            visibility=Visibility.PUBLIC,
        )


def test_permanent_vs_transient_error_classification() -> None:
    assert ErrorCode.INVALID_PMTILES.is_permanent
    assert ErrorCode.SOURCE_NOT_FOUND.is_permanent
    assert ErrorCode.TENANT_MISMATCH.is_permanent
    assert not ErrorCode.STORAGE_ERROR.is_permanent
    assert not ErrorCode.DATABASE_ERROR.is_permanent
