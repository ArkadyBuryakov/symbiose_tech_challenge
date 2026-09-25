"""Catalogue queries.

All SQL lives here so the routers stay about HTTP and authorization. Statements
are written with SQLAlchemy Core rather than the ORM: every query in this
service is deliberate (tenant predicates, row locks, conditional updates) and
an identity map would only obscure them.

Authorization note: dataset reads take an explicit ``tenant_id`` (``None`` for
an anonymous caller) and an explicit ``is_admin`` flag, and always apply the
resulting visibility predicate. There is no "unscoped by accident" path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, Select, and_, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from pmp_common.enums import JobStatus, Visibility
from pmp_common.ids import new_uuid
from pmp_common.tables import dataset_versions, datasets, publication_jobs

__all__ = [
    "create_job",
    "find_job_by_idempotency_key",
    "get_current_version",
    "get_dataset",
    "get_job",
    "get_version_by_seq",
    "list_datasets",
    "list_jobs",
    "list_versions",
    "mark_job_pending_for_retry",
    "set_current_version",
    "upsert_dataset",
]


def _dataset_select() -> Select[Any]:
    """Dataset columns plus the sequence number of the current version."""
    current = dataset_versions.alias("current")
    return select(datasets, current.c.seq.label("current_seq")).select_from(
        datasets.outerjoin(current, current.c.id == datasets.c.current_version_id)
    )


def _visibility_predicate(tenant_id: str | None, *, is_admin: bool) -> Any:
    """Which datasets this caller may see.

    * platform admin — everything.
    * tenant member  — everything owned by their tenant, plus public datasets.
    * anonymous      — public datasets only.
    """
    if is_admin:
        return true()
    public_only = datasets.c.visibility == Visibility.PUBLIC.value
    if tenant_id is None:
        return public_only
    return or_(datasets.c.tenant_id == tenant_id, public_only)


# --------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------
async def upsert_dataset(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    slug: str,
    name: str,
    visibility: Visibility,
) -> Row[Any]:
    """Return the tenant's dataset with this slug, creating it if it is new.

    ``ON CONFLICT DO NOTHING`` followed by a read makes this safe under
    concurrent first-publish requests for the same slug: exactly one insert
    wins and both callers see the same row. Name and visibility are *not*
    overwritten on conflict — a publication request must not silently flip an
    existing dataset from private to public.
    """
    await conn.execute(
        pg_insert(datasets)
        .values(
            id=new_uuid(),
            tenant_id=tenant_id,
            slug=slug,
            name=name,
            visibility=visibility.value,
            latest_seq=0,
        )
        .on_conflict_do_nothing(index_elements=[datasets.c.tenant_id, datasets.c.slug])
    )
    stmt = _dataset_select().where(datasets.c.tenant_id == tenant_id, datasets.c.slug == slug)
    return (await conn.execute(stmt)).one()


async def get_dataset(
    conn: AsyncConnection,
    dataset_id: UUID,
    *,
    tenant_id: str | None,
    is_admin: bool = False,
) -> Row[Any] | None:
    stmt = _dataset_select().where(
        datasets.c.id == dataset_id, _visibility_predicate(tenant_id, is_admin=is_admin)
    )
    return (await conn.execute(stmt)).one_or_none()


async def list_datasets(
    conn: AsyncConnection,
    *,
    tenant_id: str | None,
    is_admin: bool = False,
    owner: str | None = None,
    limit: int,
    offset: int,
) -> list[Row[Any]]:
    """Visible datasets, newest first; ``owner`` narrows to one tenant's datasets."""
    stmt = (
        _dataset_select()
        .where(_visibility_predicate(tenant_id, is_admin=is_admin))
        .order_by(datasets.c.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if owner is not None:
        stmt = stmt.where(datasets.c.tenant_id == owner)
    return list((await conn.execute(stmt)).all())


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------
async def list_versions(
    conn: AsyncConnection, dataset_id: UUID, *, current_version_id: UUID | None
) -> list[dict[str, Any]]:
    v = dataset_versions.c
    stmt = (
        # Everything but the (potentially large) spec and header documents.
        select(
            v.id,
            v.seq,
            v.sha256,
            v.spec_sha256,
            v.size_bytes,
            v.status,
            v.job_id,
            v.worker_build,
            v.created_at,
        )
        .where(v.dataset_id == dataset_id)
        .order_by(dataset_versions.c.seq.desc())
    )
    return [
        {**row._mapping, "is_current": row.id == current_version_id}
        for row in (await conn.execute(stmt)).all()
    ]


async def get_current_version(conn: AsyncConnection, dataset: Row[Any]) -> Row[Any] | None:
    if dataset.current_version_id is None:
        return None
    stmt = select(dataset_versions).where(dataset_versions.c.id == dataset.current_version_id)
    return (await conn.execute(stmt)).one_or_none()


async def get_version_by_seq(conn: AsyncConnection, dataset_id: UUID, seq: int) -> Row[Any] | None:
    stmt = select(dataset_versions).where(
        dataset_versions.c.dataset_id == dataset_id, dataset_versions.c.seq == seq
    )
    return (await conn.execute(stmt)).one_or_none()


async def set_current_version(conn: AsyncConnection, *, dataset_id: UUID, version_id: UUID) -> None:
    """Explicit rollback: move the pointer. No data is copied or deleted."""
    await conn.execute(
        update(datasets)
        .where(datasets.c.id == dataset_id)
        .values(current_version_id=version_id, updated_at=func.now())
    )


# --------------------------------------------------------------------------
# Publication jobs
# --------------------------------------------------------------------------
def _job_select() -> Select[Any]:
    """Job columns plus the sequence number of the version the job produced."""
    result = dataset_versions.alias("result")
    return select(publication_jobs, result.c.seq.label("result_version_seq")).select_from(
        publication_jobs.outerjoin(result, result.c.id == publication_jobs.c.result_version_id)
    )


async def find_job_by_idempotency_key(
    conn: AsyncConnection, *, tenant_id: str, idempotency_key: str
) -> Row[Any] | None:
    stmt = select(publication_jobs).where(
        publication_jobs.c.tenant_id == tenant_id,
        publication_jobs.c.idempotency_key == idempotency_key,
    )
    return (await conn.execute(stmt)).one_or_none()


async def create_job(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    dataset_id: UUID,
    idempotency_key: str,
    source_key: str,
    spec: dict[str, Any] | None,
    requested_by: str,
    trace_id: str | None,
) -> Row[Any]:
    """Insert a PENDING job, or return the existing one for this key.

    The insert and the ``publication.requested`` produce are deliberately *not*
    atomic. The job is committed first; if the produce then fails, the job stays
    PENDING and the worker's reconciler re-emits it. The opposite order would
    risk a message referencing a job that does not exist.
    """
    stmt = (
        pg_insert(publication_jobs)
        .values(
            id=new_uuid(),
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            idempotency_key=idempotency_key,
            source_key=source_key,
            spec=spec,
            status=JobStatus.PENDING.value,
            attempts=0,
            requested_by=requested_by,
            trace_id=trace_id,
        )
        .on_conflict_do_nothing(
            index_elements=[publication_jobs.c.tenant_id, publication_jobs.c.idempotency_key]
        )
        .returning(publication_jobs)
    )
    row = (await conn.execute(stmt)).one_or_none()
    if row is not None:
        return row
    # Lost a race with a concurrent request using the same key.
    existing = await find_job_by_idempotency_key(
        conn, tenant_id=tenant_id, idempotency_key=idempotency_key
    )
    assert existing is not None  # guaranteed by the unique index
    return existing


async def get_job(conn: AsyncConnection, job_id: UUID, *, tenant_id: str) -> Row[Any] | None:
    stmt = _job_select().where(
        publication_jobs.c.id == job_id, publication_jobs.c.tenant_id == tenant_id
    )
    return (await conn.execute(stmt)).one_or_none()


async def list_jobs(
    conn: AsyncConnection,
    *,
    tenant_id: str | None,
    dataset_id: UUID | None = None,
    status: JobStatus | None = None,
    limit: int,
    offset: int,
) -> list[Row[Any]]:
    """Jobs, newest first. ``tenant_id=None`` is the cross-tenant admin listing."""
    stmt = _job_select().order_by(publication_jobs.c.created_at.desc()).limit(limit).offset(offset)
    if tenant_id is not None:
        stmt = stmt.where(publication_jobs.c.tenant_id == tenant_id)
    if dataset_id is not None:
        stmt = stmt.where(publication_jobs.c.dataset_id == dataset_id)
    if status is not None:
        stmt = stmt.where(publication_jobs.c.status == status.value)
    return list((await conn.execute(stmt)).all())


async def mark_job_pending_for_retry(
    conn: AsyncConnection, *, job_id: UUID, tenant_id: str
) -> Row[Any] | None:
    """Reset a FAILED job to PENDING with a fresh attempt budget.

    The ``status = 'FAILED'`` predicate is the concurrency control: two
    simultaneous retries of the same job produce one reset and one 409.
    ``attempts`` is reset so a dead-lettered job gets the full retry budget
    again instead of being dead-lettered on its first transient error.
    """
    stmt = (
        update(publication_jobs)
        .where(
            and_(
                publication_jobs.c.id == job_id,
                publication_jobs.c.tenant_id == tenant_id,
                publication_jobs.c.status == JobStatus.FAILED.value,
            )
        )
        .values(
            status=JobStatus.PENDING.value,
            attempts=0,
            error_code=None,
            error_message=None,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=datetime.now(UTC),
        )
        .returning(publication_jobs)
    )
    return (await conn.execute(stmt)).one_or_none()
