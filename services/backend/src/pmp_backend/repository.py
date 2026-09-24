"""Catalogue queries.

All SQL lives here so the routers stay about HTTP and authorization. Statements
are written with SQLAlchemy Core rather than the ORM: every query in this
service is deliberate (tenant predicates, row locks, conditional updates) and
an identity map would only obscure them.

Authorization note: these functions take an explicit ``tenant_id`` (or ``None``
for a platform admin) and *always* apply it as a predicate. There is no
"unscoped by accident" path — a caller that forgets to pass a tenant gets the
admin behaviour only by asking for it by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, Select, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from pmp_common.enums import JobStatus, Visibility
from pmp_common.ids import new_uuid
from pmp_common.tables import dataset_versions, datasets, publication_jobs

__all__ = [
    "DatasetRow",
    "count_datasets",
    "count_jobs",
    "create_job",
    "find_job_by_idempotency_key",
    "get_current_version",
    "get_dataset",
    "get_dataset_by_slug",
    "get_job",
    "get_version_by_seq",
    "list_datasets",
    "list_jobs",
    "list_versions",
    "mark_job_pending_for_retry",
    "set_current_version",
    "upsert_dataset",
    "version_seq_for",
]


@dataclass(frozen=True, slots=True)
class DatasetRow:
    id: UUID
    tenant_id: str
    slug: str
    name: str
    visibility: Visibility
    latest_seq: int
    current_version_id: UUID | None
    current_seq: int | None
    created_at: datetime
    updated_at: datetime


def _dataset_select() -> Select[Any]:
    """Dataset columns plus the sequence number of the current version."""
    current = dataset_versions.alias("current")
    return select(
        datasets.c.id,
        datasets.c.tenant_id,
        datasets.c.slug,
        datasets.c.name,
        datasets.c.visibility,
        datasets.c.latest_seq,
        datasets.c.current_version_id,
        current.c.seq.label("current_seq"),
        datasets.c.created_at,
        datasets.c.updated_at,
    ).select_from(datasets.outerjoin(current, current.c.id == datasets.c.current_version_id))


def _to_dataset(row: Row[Any]) -> DatasetRow:
    return DatasetRow(
        id=row.id,
        tenant_id=row.tenant_id,
        slug=row.slug,
        name=row.name,
        visibility=Visibility(row.visibility),
        latest_seq=row.latest_seq,
        current_version_id=row.current_version_id,
        current_seq=row.current_seq,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _visibility_predicate(tenant_id: str | None, *, is_admin: bool) -> Any:
    """Which datasets this caller may see.

    * platform admin — everything.
    * tenant member  — everything owned by their tenant, plus public datasets.
    * anonymous      — public datasets only.
    """
    if is_admin:
        return None
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
) -> DatasetRow:
    """Return the tenant's dataset with this slug, creating it if it is new.

    ``ON CONFLICT DO NOTHING`` followed by a read makes this safe under
    concurrent first-publish requests for the same slug: exactly one insert
    wins and both callers see the same row. Name and visibility are *not*
    overwritten on conflict — a publication request must not silently flip an
    existing dataset from private to public.
    """
    stmt = (
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
    await conn.execute(stmt)

    row = await get_dataset_by_slug(conn, tenant_id=tenant_id, slug=slug)
    if row is None:  # pragma: no cover - only reachable if the row vanished mid-transaction
        raise RuntimeError(f"dataset {tenant_id}/{slug} disappeared during upsert")
    return row


async def get_dataset_by_slug(
    conn: AsyncConnection, *, tenant_id: str, slug: str
) -> DatasetRow | None:
    stmt = _dataset_select().where(and_(datasets.c.tenant_id == tenant_id, datasets.c.slug == slug))
    row = (await conn.execute(stmt)).one_or_none()
    return _to_dataset(row) if row else None


async def get_dataset(
    conn: AsyncConnection,
    dataset_id: UUID,
    *,
    tenant_id: str | None,
    is_admin: bool = False,
) -> DatasetRow | None:
    stmt = _dataset_select().where(datasets.c.id == dataset_id)
    predicate = _visibility_predicate(tenant_id, is_admin=is_admin)
    if predicate is not None:
        stmt = stmt.where(predicate)
    row = (await conn.execute(stmt)).one_or_none()
    return _to_dataset(row) if row else None


async def list_datasets(
    conn: AsyncConnection,
    *,
    tenant_id: str | None,
    is_admin: bool = False,
    limit: int,
    offset: int,
) -> list[DatasetRow]:
    stmt = _dataset_select().order_by(datasets.c.created_at.desc()).limit(limit).offset(offset)
    predicate = _visibility_predicate(tenant_id, is_admin=is_admin)
    if predicate is not None:
        stmt = stmt.where(predicate)
    rows = (await conn.execute(stmt)).all()
    return [_to_dataset(row) for row in rows]


async def count_datasets(
    conn: AsyncConnection, *, tenant_id: str | None, is_admin: bool = False
) -> int:
    stmt = select(func.count()).select_from(datasets)
    predicate = _visibility_predicate(tenant_id, is_admin=is_admin)
    if predicate is not None:
        stmt = stmt.where(predicate)
    return int((await conn.execute(stmt)).scalar_one())


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------
async def list_versions(
    conn: AsyncConnection, dataset_id: UUID, *, current_version_id: UUID | None
) -> list[dict[str, Any]]:
    stmt = (
        select(
            dataset_versions.c.id,
            dataset_versions.c.seq,
            dataset_versions.c.sha256,
            dataset_versions.c.size_bytes,
            dataset_versions.c.status,
            dataset_versions.c.job_id,
            dataset_versions.c.worker_build,
            dataset_versions.c.created_at,
        )
        .where(dataset_versions.c.dataset_id == dataset_id)
        .order_by(dataset_versions.c.seq.desc())
    )
    return [
        {**row._mapping, "is_current": row.id == current_version_id}
        for row in (await conn.execute(stmt)).all()
    ]


async def get_current_version(conn: AsyncConnection, dataset: DatasetRow) -> Row[Any] | None:
    if dataset.current_version_id is None:
        return None
    stmt = select(dataset_versions).where(dataset_versions.c.id == dataset.current_version_id)
    return (await conn.execute(stmt)).one_or_none()


async def get_version_by_seq(conn: AsyncConnection, dataset_id: UUID, seq: int) -> Row[Any] | None:
    stmt = select(dataset_versions).where(
        and_(dataset_versions.c.dataset_id == dataset_id, dataset_versions.c.seq == seq)
    )
    return (await conn.execute(stmt)).one_or_none()


async def set_current_version(conn: AsyncConnection, *, dataset_id: UUID, version_id: UUID) -> None:
    """Explicit rollback: move the pointer. No data is copied or deleted."""
    await conn.execute(
        update(datasets).where(datasets.c.id == dataset_id).values(current_version_id=version_id)
    )


# --------------------------------------------------------------------------
# Publication jobs
# --------------------------------------------------------------------------
async def find_job_by_idempotency_key(
    conn: AsyncConnection, *, tenant_id: str, idempotency_key: str
) -> Row[Any] | None:
    stmt = select(publication_jobs).where(
        and_(
            publication_jobs.c.tenant_id == tenant_id,
            publication_jobs.c.idempotency_key == idempotency_key,
        )
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

    existing = await find_job_by_idempotency_key(
        conn, tenant_id=tenant_id, idempotency_key=idempotency_key
    )
    if existing is None:  # pragma: no cover - unreachable while the unique index exists
        raise RuntimeError("job insert conflicted but no existing row was found")
    return existing


async def get_job(conn: AsyncConnection, job_id: UUID, *, tenant_id: str | None) -> Row[Any] | None:
    stmt = select(publication_jobs).where(publication_jobs.c.id == job_id)
    if tenant_id is not None:
        stmt = stmt.where(publication_jobs.c.tenant_id == tenant_id)
    return (await conn.execute(stmt)).one_or_none()


def _job_filter(tenant_id: str | None, dataset_id: UUID | None, status: JobStatus | None) -> Any:
    clauses = []
    if tenant_id is not None:
        clauses.append(publication_jobs.c.tenant_id == tenant_id)
    if dataset_id is not None:
        clauses.append(publication_jobs.c.dataset_id == dataset_id)
    if status is not None:
        clauses.append(publication_jobs.c.status == status.value)
    return and_(*clauses) if clauses else None


async def list_jobs(
    conn: AsyncConnection,
    *,
    tenant_id: str | None,
    dataset_id: UUID | None = None,
    status: JobStatus | None = None,
    limit: int,
    offset: int,
) -> list[Row[Any]]:
    stmt = (
        select(publication_jobs)
        .order_by(publication_jobs.c.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    where = _job_filter(tenant_id, dataset_id, status)
    if where is not None:
        stmt = stmt.where(where)
    return list((await conn.execute(stmt)).all())


async def count_jobs(
    conn: AsyncConnection,
    *,
    tenant_id: str | None,
    dataset_id: UUID | None = None,
    status: JobStatus | None = None,
) -> int:
    stmt = select(func.count()).select_from(publication_jobs)
    where = _job_filter(tenant_id, dataset_id, status)
    if where is not None:
        stmt = stmt.where(where)
    return int((await conn.execute(stmt)).scalar_one())


async def mark_job_pending_for_retry(
    conn: AsyncConnection, *, job_id: UUID, tenant_id: str
) -> Row[Any] | None:
    """Reset a FAILED job to PENDING so it can be re-emitted.

    The ``status = 'FAILED'`` predicate is the concurrency control: two
    simultaneous retries of the same job produce one reset and one 409.
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
            error_code=None,
            error_message=None,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=datetime.now(UTC),
        )
        .returning(publication_jobs)
    )
    return (await conn.execute(stmt)).one_or_none()


async def version_seq_for(conn: AsyncConnection, version_id: UUID | None) -> int | None:
    if version_id is None:
        return None
    stmt = select(dataset_versions.c.seq).where(dataset_versions.c.id == version_id)
    return (await conn.execute(stmt)).scalar_one_or_none()
