"""Catalogue writes, synchronous.

This module holds the two statements the whole platform's correctness rests on:

* :func:`claim_job` — a conditional UPDATE that is also the distributed lock.
  It is the reason a redelivered Kafka message, a duplicate produce, or two
  workers racing on the same partition cannot double-publish.
* :func:`apply_publication` — one transaction that locks the dataset row,
  applies the versioning decision, finalises the job, and writes the result
  event into the outbox. Either all four happen or none do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pmp_common.enums import (
    ErrorCode,
    JobStatus,
    PublicationResult,
    VersionStatus,
    Visibility,
)
from pmp_common.events import EventEnvelope, PublicationFailed, PublicationSucceeded
from pmp_common.ids import new_uuid
from pmp_common.logging import get_logger
from pmp_common.tables import dataset_versions, datasets, outbox, publication_jobs
from pmp_common.versioning import DatasetState, VersionDecision, decide_version

__all__ = [
    "ClaimedJob",
    "PublicationOutcome",
    "apply_publication",
    "claim_job",
    "enqueue_failure",
    "fail_job",
    "load_dataset_state",
    "release_lease",
    "renew_lease",
]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: UUID
    tenant_id: str
    dataset_id: UUID
    source_key: str
    spec: dict[str, Any] | None
    attempts: int
    idempotency_key: str
    requested_by: str


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    result: PublicationResult
    version_id: UUID
    version_seq: int
    object_key: str
    sha256: str
    size_bytes: int
    visibility: Visibility


def claim_job(
    conn: Connection, *, job_id: UUID, owner: str, lease_seconds: int
) -> ClaimedJob | None:
    """Take ownership of a job, or return ``None`` if it is not ours to take.

    The ``WHERE`` clause is the entire concurrency story:

    * ``PENDING`` — nobody has started it.
    * ``RUNNING`` with an expired lease — the previous owner died; the lease is
      what makes it safe to take over rather than waiting forever.

    Anything else (already SUCCEEDED or FAILED, or RUNNING under a live lease)
    yields no row, and the caller commits the Kafka offset and moves on. That is
    how a duplicate delivery becomes a no-op.
    """
    now = datetime.now(UTC)
    stmt = (
        update(publication_jobs)
        .where(
            and_(
                publication_jobs.c.id == job_id,
                or_(
                    publication_jobs.c.status == JobStatus.PENDING.value,
                    and_(
                        publication_jobs.c.status == JobStatus.RUNNING.value,
                        publication_jobs.c.lease_expires_at < now,
                    ),
                ),
            )
        )
        .values(
            status=JobStatus.RUNNING.value,
            lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempts=publication_jobs.c.attempts + 1,
        )
        .returning(publication_jobs)
    )
    row = conn.execute(stmt).one_or_none()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        tenant_id=row.tenant_id,
        dataset_id=row.dataset_id,
        source_key=row.source_key,
        spec=row.spec,
        attempts=row.attempts,
        idempotency_key=row.idempotency_key,
        requested_by=row.requested_by,
    )


def renew_lease(conn: Connection, *, job_id: UUID, owner: str, lease_seconds: int) -> bool:
    """Extend a lease we still hold. Used by long-running copies."""
    stmt = (
        update(publication_jobs)
        .where(
            and_(
                publication_jobs.c.id == job_id,
                publication_jobs.c.lease_owner == owner,
                publication_jobs.c.status == JobStatus.RUNNING.value,
            )
        )
        .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds))
    )
    return conn.execute(stmt).rowcount > 0


def release_lease(conn: Connection, *, job_id: UUID) -> None:
    """Put a RUNNING job back to PENDING and drop its lease.

    Called before an in-process retry sleep so that, if this worker dies during
    the wait, the reconciler can re-emit the job immediately instead of waiting
    for the lease to expire.
    """
    conn.execute(
        update(publication_jobs)
        .where(
            and_(
                publication_jobs.c.id == job_id,
                publication_jobs.c.status == JobStatus.RUNNING.value,
            )
        )
        .values(status=JobStatus.PENDING.value, lease_owner=None, lease_expires_at=None)
    )


def load_dataset_state(conn: Connection, dataset_id: UUID) -> tuple[Row[Any], DatasetState]:
    """Lock the dataset row and read everything the versioning rules need.

    ``FOR UPDATE`` serialises concurrent publications of the same dataset. Two
    workers handling different datasets never block each other; two handling the
    same one queue up, which is exactly the required behaviour because
    ``latest_seq`` must be allocated without gaps or duplicates.
    """
    dataset = conn.execute(
        select(datasets).where(datasets.c.id == dataset_id).with_for_update()
    ).one_or_none()
    if dataset is None:
        raise LookupError(f"dataset {dataset_id} does not exist")

    available = conn.execute(
        select(dataset_versions.c.sha256, dataset_versions.c.seq).where(
            and_(
                dataset_versions.c.dataset_id == dataset_id,
                dataset_versions.c.status == VersionStatus.AVAILABLE.value,
            )
        )
    ).all()

    current_seq: int | None = None
    current_sha: str | None = None
    if dataset.current_version_id is not None:
        current = conn.execute(
            select(dataset_versions.c.seq, dataset_versions.c.sha256).where(
                dataset_versions.c.id == dataset.current_version_id
            )
        ).one_or_none()
        if current is not None:
            current_seq, current_sha = current.seq, current.sha256

    state = DatasetState(
        latest_seq=dataset.latest_seq,
        current_seq=current_seq,
        current_sha256=current_sha,
        available_shas={row.sha256: row.seq for row in available},
    )
    return dataset, state


def apply_publication(
    conn: Connection,
    *,
    job: ClaimedJob,
    sha256: str,
    size_bytes: int,
    object_key: str,
    source_etag: str,
    pmtiles_header: dict[str, Any],
    worker_build: str,
) -> PublicationOutcome:
    """Apply the versioning decision and finalise the job, atomically.

    Everything in here is one transaction: the version row, the dataset pointer,
    the job's terminal state and the outbox row. The Kafka offset is committed
    only after this returns, so a crash anywhere before the commit simply
    replays the message.
    """
    dataset, state = load_dataset_state(conn, job.dataset_id)
    decision = decide_version(state, sha256)

    version_id = _apply_decision(
        conn,
        decision=decision,
        dataset=dataset,
        job=job,
        sha256=sha256,
        size_bytes=size_bytes,
        object_key=object_key,
        source_etag=source_etag,
        pmtiles_header=pmtiles_header,
        worker_build=worker_build,
    )

    conn.execute(
        update(publication_jobs)
        .where(publication_jobs.c.id == job.id)
        .values(
            status=JobStatus.SUCCEEDED.value,
            result=decision.result.value,
            result_version_id=version_id,
            error_code=None,
            error_message=None,
            lease_owner=None,
            lease_expires_at=None,
        )
    )

    visibility = Visibility(dataset.visibility)
    outcome = PublicationOutcome(
        result=decision.result,
        version_id=version_id,
        version_seq=decision.seq,
        object_key=object_key,
        sha256=sha256,
        size_bytes=size_bytes,
        visibility=visibility,
    )
    _enqueue(
        conn,
        PublicationSucceeded(
            tenant_id=job.tenant_id,
            dataset_id=job.dataset_id,
            job_id=job.id,
            result=decision.result,
            version_id=version_id,
            version_seq=decision.seq,
            sha256=sha256,
            size_bytes=size_bytes,
            object_key=object_key,
            visibility=visibility,
        ),
    )
    return outcome


def _apply_decision(
    conn: Connection,
    *,
    decision: VersionDecision,
    dataset: Row[Any],
    job: ClaimedJob,
    sha256: str,
    size_bytes: int,
    object_key: str,
    source_etag: str,
    pmtiles_header: dict[str, Any],
    worker_build: str,
) -> UUID:
    if not decision.create_version:
        # DEDUPLICATED or POINTER_MOVED: the version row already exists.
        existing = conn.execute(
            select(dataset_versions.c.id).where(
                and_(
                    dataset_versions.c.dataset_id == job.dataset_id,
                    dataset_versions.c.seq == decision.seq,
                )
            )
        ).scalar_one()
        if decision.move_pointer:
            conn.execute(
                update(datasets)
                .where(datasets.c.id == job.dataset_id)
                .values(current_version_id=existing)
            )
        return UUID(str(existing))

    version_id = new_uuid()
    # ON CONFLICT DO NOTHING on the live-sha partial unique index: if a
    # concurrent transaction inserted the same content first, fall through to
    # reading its row rather than failing the job.
    inserted = conn.execute(
        pg_insert(dataset_versions)
        .values(
            id=version_id,
            dataset_id=job.dataset_id,
            seq=decision.seq,
            sha256=sha256,
            size_bytes=size_bytes,
            object_key=object_key,
            source_key=job.source_key,
            source_etag=source_etag,
            spec=job.spec,
            pmtiles_header=pmtiles_header,
            job_id=job.id,
            status=VersionStatus.AVAILABLE.value,
            worker_build=worker_build,
        )
        .on_conflict_do_nothing()
        .returning(dataset_versions.c.id)
    ).scalar_one_or_none()

    if inserted is None:  # pragma: no cover - the FOR UPDATE lock makes this rare
        inserted = conn.execute(
            select(dataset_versions.c.id).where(
                and_(
                    dataset_versions.c.dataset_id == job.dataset_id,
                    dataset_versions.c.sha256 == sha256,
                    dataset_versions.c.status != VersionStatus.RETIRED.value,
                )
            )
        ).scalar_one()
        log.warning("worker.version_insert_raced", dataset_id=str(job.dataset_id), sha256=sha256)

    conn.execute(
        update(datasets)
        .where(datasets.c.id == job.dataset_id)
        .values(current_version_id=inserted, latest_seq=decision.seq)
    )
    return UUID(str(inserted))


def fail_job(
    conn: Connection,
    *,
    job: ClaimedJob,
    error_code: ErrorCode,
    error_message: str,
    dead_lettered: bool,
) -> None:
    """Mark a job FAILED and enqueue the failure event, in one transaction."""
    conn.execute(
        update(publication_jobs)
        .where(publication_jobs.c.id == job.id)
        .values(
            status=JobStatus.FAILED.value,
            error_code=error_code.value,
            error_message=error_message[:2000],
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    enqueue_failure(
        conn,
        job=job,
        error_code=error_code,
        error_message=error_message,
        dead_lettered=dead_lettered,
    )


def enqueue_failure(
    conn: Connection,
    *,
    job: ClaimedJob,
    error_code: ErrorCode,
    error_message: str,
    dead_lettered: bool,
) -> None:
    _enqueue(
        conn,
        PublicationFailed(
            tenant_id=job.tenant_id,
            dataset_id=job.dataset_id,
            job_id=job.id,
            error_code=error_code,
            error_message=error_message[:2000],
            attempts=job.attempts,
            dead_lettered=dead_lettered,
        ),
    )


def _enqueue(conn: Connection, event: EventEnvelope, *, topic: str | None = None) -> None:
    """Write an event into the transactional outbox.

    The event is committed with the state change it describes, so a consumer can
    never see a result for a job whose row says otherwise. A separate relay
    thread moves outbox rows onto Kafka.
    """
    from pmp_common.tracing import kafka_headers_from_carrier

    conn.execute(
        insert(outbox).values(
            id=new_uuid(),
            topic=topic or "publication.results",
            key=str(event.dataset_id),
            payload=event.model_dump(mode="json"),
            headers={k: v.decode() for k, v in kafka_headers_from_carrier()},
        )
    )
