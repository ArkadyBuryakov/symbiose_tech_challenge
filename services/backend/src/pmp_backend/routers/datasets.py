"""Dataset and version endpoints.

These are the read paths the map client uses. They are ``optional``-auth: an
anonymous caller sees public datasets, a signed-in caller additionally sees
their own tenant's private datasets, and a platform administrator sees
everything.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, Response

from pmp_common.enums import VersionStatus
from pmp_common.problem import Conflict, NotFound
from pmp_common.s3 import publish_key

from .. import repository as repo
from ..deps import Conn, Settings
from ..identity import OptionalIdentity, TenantAdmin
from ..schemas import (
    CurrentVersionResponse,
    DatasetDetail,
    DatasetSummary,
    Page,
    RollbackRequest,
    VersionSummary,
)

router = APIRouter(prefix="/datasets", tags=["datasets"])

# `/current` is the one response that must not be cached hard: it is the
# indirection that lets the immutable tile archives be cached forever.
CURRENT_CACHE_CONTROL = "public, max-age=30, stale-while-revalidate=30"


def _scope(identity: OptionalIdentity) -> tuple[str | None, bool]:
    if identity is None:
        return None, False
    return identity.tenant_id, identity.is_platform_admin


def tile_url(*, visibility: str, tenant_id: str, dataset_id: UUID, sha256: str) -> str:
    """Edge path of an immutable tile archive.

    A path, not an absolute URL: the browser resolves it against the origin it
    is already on, so the same response works behind localhost and behind a
    CloudFront domain without the backend knowing which.
    """
    key = publish_key(
        visibility=visibility, tenant_id=tenant_id, dataset_id=dataset_id, sha256=sha256
    )
    return f"/tiles/{key}"


@router.get("", response_model=Page[DatasetSummary], summary="List visible datasets")
async def list_datasets(
    identity: OptionalIdentity,
    conn: Conn,
    settings: Settings,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DatasetSummary]:
    tenant_id, is_admin = _scope(identity)
    limit = min(limit, settings.max_page_size)
    rows = await repo.list_datasets(
        conn, tenant_id=tenant_id, is_admin=is_admin, limit=limit, offset=offset
    )
    total = await repo.count_datasets(conn, tenant_id=tenant_id, is_admin=is_admin)
    return Page(
        items=[DatasetSummary.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{dataset_id}", response_model=DatasetDetail, summary="Get one dataset")
async def get_dataset(
    dataset_id: Annotated[UUID, Path()],
    identity: OptionalIdentity,
    conn: Conn,
) -> DatasetDetail:
    tenant_id, is_admin = _scope(identity)
    dataset = await repo.get_dataset(conn, dataset_id, tenant_id=tenant_id, is_admin=is_admin)
    if dataset is None:
        raise NotFound(f"No dataset {dataset_id} visible to this caller.")

    current = await repo.get_current_version(conn, dataset)
    detail = DatasetDetail.model_validate(dataset)
    if current is not None:
        detail.current_version = VersionSummary.model_validate(
            {**current._mapping, "is_current": True}
        )
    return detail


@router.get(
    "/{dataset_id}/versions",
    response_model=list[VersionSummary],
    summary="List a dataset's versions, newest first",
)
async def list_versions(
    dataset_id: Annotated[UUID, Path()],
    identity: OptionalIdentity,
    conn: Conn,
) -> list[VersionSummary]:
    tenant_id, is_admin = _scope(identity)
    dataset = await repo.get_dataset(conn, dataset_id, tenant_id=tenant_id, is_admin=is_admin)
    if dataset is None:
        raise NotFound(f"No dataset {dataset_id} visible to this caller.")

    rows = await repo.list_versions(conn, dataset_id, current_version_id=dataset.current_version_id)
    return [VersionSummary.model_validate(row) for row in rows]


@router.get(
    "/{dataset_id}/current",
    response_model=CurrentVersionResponse,
    summary="Everything the map client needs to render this dataset",
)
async def get_current(
    dataset_id: Annotated[UUID, Path()],
    identity: OptionalIdentity,
    conn: Conn,
    response: Response,
) -> CurrentVersionResponse:
    tenant_id, is_admin = _scope(identity)
    dataset = await repo.get_dataset(conn, dataset_id, tenant_id=tenant_id, is_admin=is_admin)
    if dataset is None:
        raise NotFound(f"No dataset {dataset_id} visible to this caller.")

    current = await repo.get_current_version(conn, dataset)
    if current is None:
        raise NotFound(
            f"Dataset {dataset_id} has no published version yet.",
            code="no-current-version",
        )

    response.headers["Cache-Control"] = CURRENT_CACHE_CONTROL
    return CurrentVersionResponse(
        dataset_id=dataset.id,
        seq=current.seq,
        sha256=current.sha256,
        spec_sha256=current.spec_sha256,
        size_bytes=current.size_bytes,
        url=tile_url(
            visibility=dataset.visibility.value,
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            sha256=current.sha256,
        ),
        visibility=dataset.visibility,
        spec=current.spec,
        pmtiles_header=current.pmtiles_header,
        created_at=current.created_at,
    )


@router.put(
    "/{dataset_id}/current",
    response_model=CurrentVersionResponse,
    summary="Roll the dataset back to an earlier version",
)
async def set_current(
    dataset_id: Annotated[UUID, Path()],
    body: RollbackRequest,
    identity: TenantAdmin,
    conn: Conn,
    response: Response,
) -> CurrentVersionResponse:
    """Move the dataset pointer to an existing AVAILABLE version.

    No bytes are copied and no version row changes: rollback is purely a
    pointer move, which is why it is instant and reversible.
    """
    tenant_id = identity.tenant_id if not identity.is_platform_admin else None
    dataset = await repo.get_dataset(
        conn, dataset_id, tenant_id=tenant_id, is_admin=identity.is_platform_admin
    )
    if dataset is None:
        raise NotFound(f"No dataset {dataset_id} visible to this caller.")
    if not identity.is_platform_admin and dataset.tenant_id != identity.tenant_id:
        # Visible because it is public, but not owned by this tenant.
        raise NotFound(f"No dataset {dataset_id} owned by this tenant.")

    version = await repo.get_version_by_seq(conn, dataset_id, body.seq)
    if version is None:
        raise NotFound(f"Dataset {dataset_id} has no version {body.seq}.")
    if version.status != VersionStatus.AVAILABLE.value:
        raise Conflict(
            f"Version {body.seq} is {version.status} and cannot be made current.",
            code="version-not-available",
        )

    await repo.set_current_version(conn, dataset_id=dataset_id, version_id=version.id)
    await conn.commit()

    response.headers["Cache-Control"] = "no-store"
    return CurrentVersionResponse(
        dataset_id=dataset.id,
        seq=version.seq,
        sha256=version.sha256,
        spec_sha256=version.spec_sha256,
        size_bytes=version.size_bytes,
        url=tile_url(
            visibility=dataset.visibility.value,
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            sha256=version.sha256,
        ),
        visibility=dataset.visibility,
        spec=version.spec,
        pmtiles_header=version.pmtiles_header,
        created_at=version.created_at,
    )
