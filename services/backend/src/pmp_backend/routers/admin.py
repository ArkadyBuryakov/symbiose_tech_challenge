"""Cross-tenant administration.

The gateway already refuses these routes to anyone without the platform admin
role. The backend checks again anyway: the gateway's route table and the
service's own authorization are two independent controls, and a mistake in one
should not silently expose every tenant's data.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from pmp_common.enums import JobStatus

from .. import repository as repo
from ..deps import Conn
from ..identity import PlatformAdmin
from ..schemas import DatasetSummary, JobResponse, Page

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/datasets",
    response_model=Page[DatasetSummary],
    summary="List datasets across every tenant",
)
async def admin_list_datasets(
    identity: PlatformAdmin,
    conn: Conn,
    tenant_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DatasetSummary]:
    rows = await repo.list_datasets(
        conn, tenant_id=None, is_admin=True, owner=tenant_id, limit=limit, offset=offset
    )
    return Page(
        items=[DatasetSummary.model_validate(row) for row in rows], limit=limit, offset=offset
    )


@router.get(
    "/publications",
    response_model=Page[JobResponse],
    summary="List publication jobs across every tenant",
)
async def admin_list_publications(
    identity: PlatformAdmin,
    conn: Conn,
    tenant_id: Annotated[str | None, Query()] = None,
    dataset_id: Annotated[UUID | None, Query()] = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[JobResponse]:
    rows = await repo.list_jobs(
        conn,
        tenant_id=tenant_id,
        dataset_id=dataset_id,
        status=job_status,
        limit=limit,
        offset=offset,
    )
    return Page(items=[JobResponse.model_validate(row) for row in rows], limit=limit, offset=offset)
