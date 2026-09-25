"""Catalogue writes, synchronous.

* :func:`claim_job` — a conditional UPDATE that is also the distributed lock.
  It is why a redelivered message or two racing workers cannot double-publish.
* :func:`apply_publication` / :func:`fail_job` — one transaction each that
  finalises the job and writes the result event into the outbox. Both refuse
  (raise :class:`LeaseLost`, rolling back) unless this worker still holds the
  lease, so a worker that lost its job cannot overwrite another's result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, and_, func, insert, or_, select, update

from pmp_common.enums import ErrorCode, JobStatus, PublicationResult, VersionStatus, Visibility
from pmp_common.events import EventEnvelope, PublicationFailed, PublicationSucceeded
from pmp_common.ids import new_uuid
from pmp_common.kafka import TOPIC_RESULTS
from pmp_common.tables import dataset_versions, datasets, outbox, publication_jobs
from pmp_common.tracing import kafka_headers_from_carrier
from pmp_common.versioning import (
    DatasetState,
    VersionDecision,
    VersionKey,
    decide_version,
    spec_digest,
)

__all__ = [
    "ClaimedJob",
    "LeaseLost",
    "apply_publication",
    "claim_job",
    "fail_job",
    "release_lease",
    "renew_lease",
]


class LeaseLost(Exception):
    """This worker no longer owns the job; its terminal write was rolled back."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: UUID
    tenant_id: str
    dataset_id: UUID
    source_key: str
    spec: dict[str, Any] | None
    attempts: int
    visibility: Visibility
    owner: str


def claim_job(
    conn: Connection, *, job_id: UUID, owner: str, lease_seconds: int
) -> ClaimedJob | None:
    """Take ownership of a job, or return ``None`` if it is not ours to take.

    ``PENDING``, or ``RUNNING`` with an expired lease (the owner died), can be
    claimed. Anything else — terminal, or leased by a live worker — yields no
    row, which is how a duplicate delivery becomes a no-op. Times come from the
    database clock so replicas with skewed clocks agree.
    """
    row = conn.execute(
        update(publication_jobs)
        .where(
            publication_jobs.c.id == job_id,
            or_(
                publication_jobs.c.status == JobStatus.PENDING.value,
                and_(
                    publication_jobs.c.status == JobStatus.RUNNING.value,
                    publication_jobs.c.lease_expires_at < func.now(),
                ),
            ),
        )
        .values(
            status=JobStatus.RUNNING.value,
            lease_owner=owner,
            lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
            attempts=publication_jobs.c.attempts + 1,
        )
        .returning(publication_jobs)
    ).one_or_none()
    if row is None:
        return None
    visibility = conn.execute(
        select(datasets.c.visibility).where(datasets.c.id == row.dataset_id)
    ).scalar_one()
    return ClaimedJob(
        id=row.id,
        tenant_id=row.tenant_id,
        dataset_id=row.dataset_id,
        source_key=row.source_key,
        spec=row.spec,
        attempts=row.attempts,
        visibility=Visibility(visibility),
        owner=owner,
    )


def _owned(job_id: UUID, owner: str) -> Any:
    return and_(
        publication_jobs.c.id == job_id,
        publication_jobs.c.status == JobStatus.RUNNING.value,
        publication_jobs.c.lease_owner == owner,
    )


def renew_lease(conn: Connection, *, job_id: UUID, owner: str, lease_seconds: int) -> bool:
    """Extend a lease we still hold. Used by the heartbeat."""
    stmt = (
        update(publication_jobs)
        .where(_owned(job_id, owner))
        .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
    )
    return conn.execute(stmt).rowcount > 0


def release_lease(conn: Connection, *, job: ClaimedJob) -> None:
    """Put our RUNNING job back to PENDING before an in-process retry sleep.

    If this worker dies during the wait, the reconciler re-emits the job; if it
    does not, the worker re-claims it after the wait.
    """
    conn.execute(
        update(publication_jobs)
        .where(_owned(job.id, job.owner))
        .values(status=JobStatus.PENDING.value, lease_owner=None, lease_expires_at=None)
    )


def _finish_job(conn: Connection, job: ClaimedJob, **values: Any) -> None:
    """Move our job to a terminal state, or raise :class:`LeaseLost`."""
    result = conn.execute(
        update(publication_jobs)
        .where(_owned(job.id, job.owner))
        .values(lease_owner=None, lease_expires_at=None, **values)
    )
    if result.rowcount == 0:
        raise LeaseLost(str(job.id))


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
) -> VersionDecision:
    """Apply the versioning decision and finalise the job, in one transaction.

    ``FOR UPDATE`` on the dataset row serialises publications of the same
    dataset, so ``latest_seq`` is allocated without gaps or duplicates.
    """
    dataset = conn.execute(
        select(datasets).where(datasets.c.id == job.dataset_id).with_for_update()
    ).one()
    versions = conn.execute(
        select(
            dataset_versions.c.id,
            dataset_versions.c.seq,
            dataset_versions.c.sha256,
            dataset_versions.c.spec_sha256,
        ).where(
            dataset_versions.c.dataset_id == job.dataset_id,
            dataset_versions.c.status == VersionStatus.AVAILABLE.value,
        )
    ).all()
    by_key = {VersionKey(v.sha256, v.spec_sha256): v for v in versions}
    current_key = next((k for k, v in by_key.items() if v.id == dataset.current_version_id), None)

    # A version is the bytes *and* the spec: the same archive with a new spec
    # is a new version (sharing the same stored object).
    spec_sha256 = spec_digest(job.spec)
    key = VersionKey(sha256, spec_sha256)
    decision = decide_version(
        DatasetState(
            latest_seq=dataset.latest_seq,
            current_key=current_key,
            available={k: v.seq for k, v in by_key.items()},
        ),
        key,
    )

    if decision.create_version:
        version_id = new_uuid()
        conn.execute(
            insert(dataset_versions).values(
                id=version_id,
                dataset_id=job.dataset_id,
                seq=decision.seq,
                sha256=sha256,
                spec_sha256=spec_sha256,
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
        )
        pointer: dict[str, Any] = {"current_version_id": version_id, "latest_seq": decision.seq}
    else:
        version_id = by_key[key].id
        pointer = {"current_version_id": version_id}

    if decision.result is not PublicationResult.DEDUPLICATED:
        conn.execute(update(datasets).where(datasets.c.id == job.dataset_id).values(**pointer))

    _finish_job(
        conn,
        job,
        status=JobStatus.SUCCEEDED.value,
        result=decision.result.value,
        result_version_id=version_id,
        error_code=None,
        error_message=None,
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
            spec_sha256=spec_sha256,
            size_bytes=size_bytes,
            object_key=object_key,
            visibility=Visibility(dataset.visibility),
        ),
    )
    return decision


def fail_job(
    conn: Connection,
    *,
    job: ClaimedJob,
    error_code: ErrorCode,
    error_message: str,
    dead_lettered: bool,
) -> None:
    """Mark our job FAILED and enqueue the failure event, in one transaction."""
    _finish_job(
        conn,
        job,
        status=JobStatus.FAILED.value,
        error_code=error_code.value,
        error_message=error_message[:2000],
    )
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


def _enqueue(conn: Connection, event: EventEnvelope) -> None:
    """Write a result event into the transactional outbox (relayed by OutboxRelay)."""
    conn.execute(
        insert(outbox).values(
            id=new_uuid(),
            topic=TOPIC_RESULTS,
            key=str(event.dataset_id),
            payload=event.model_dump(mode="json"),
            headers={k: v.decode() for k, v in kafka_headers_from_carrier()},
        )
    )
