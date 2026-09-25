"""Dataset and version endpoints.

These are the read paths the map client uses. They are ``optional``-auth: an
anonymous caller sees public datasets, a signed-in caller additionally sees
their own tenant's private datasets, and a platform administrator sees
everything.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, Response
from sqlalchemy import Row

from pmp_common.enums import VersionStatus, Visibility
from pmp_common.problem import Conflict, NotFound
from pmp_common.s3 import publish_key

from .. import repository as repo
from ..deps import Conn
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


def _scope(identity: OptionalIdentity) -> tuple[str | None, bool]:
    if identity is None:
        return None, False
    return identity.tenant_id, identity.is_platform_admin


async def _visible_dataset(conn: Conn, dataset_id: UUID, identity: OptionalIdentity) -> Row[Any]:
    tenant_id, is_admin = _scope(identity)
    dataset = await repo.get_dataset(conn, dataset_id, tenant_id=tenant_id, is_admin=is_admin)
    if dataset is None:
        raise NotFound(f"No dataset {dataset_id} visible to this caller.")
    return dataset


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


def _current_response(dataset: Row[Any], version: Row[Any]) -> CurrentVersionResponse:
    return CurrentVersionResponse(
        dataset_id=dataset.id,
        seq=version.seq,
        sha256=version.sha256,
        spec_sha256=version.spec_sha256,
        size_bytes=version.size_bytes,
        url=tile_url(
            visibility=dataset.visibility,
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            sha256=version.sha256,
        ),
        visibility=dataset.visibility,
        spec=version.spec,
        pmtiles_header=version.pmtiles_header,
        created_at=version.created_at,
    )


@router.get("", response_model=Page[DatasetSummary], summary="List visible datasets")
async def list_datasets(
    identity: OptionalIdentity,
    conn: Conn,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DatasetSummary]:
    tenant_id, is_admin = _scope(identity)
    rows = await repo.list_datasets(
        conn, tenant_id=tenant_id, is_admin=is_admin, limit=limit, offset=offset
    )
    return Page(
        items=[DatasetSummary.model_validate(row) for row in rows], limit=limit, offset=offset
    )


@router.get("/{dataset_id}", response_model=DatasetDetail, summary="Get one dataset")
async def get_dataset(
    dataset_id: Annotated[UUID, Path()],
    identity: OptionalIdentity,
    conn: Conn,
) -> DatasetDetail:
    dataset = await _visible_dataset(conn, dataset_id, identity)
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
    dataset = await _visible_dataset(conn, dataset_id, identity)
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
    dataset = await _visible_dataset(conn, dataset_id, identity)
    current = await repo.get_current_version(conn, dataset)
    if current is None:
        raise NotFound(
            f"Dataset {dataset_id} has no published version yet.",
            code="no-current-version",
        )

    # `/current` is the indirection that lets the immutable archives be cached
    # forever, so it is cached only briefly. A private dataset's answer depends
    # on who is asking and must never land in a shared cache.
    scope = "public" if dataset.visibility == Visibility.PUBLIC.value else "private"
    response.headers["Cache-Control"] = f"{scope}, max-age=30"
    return _current_response(dataset, current)


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

    Tenant ``owner``/``admin`` of the owning tenant only. No bytes are copied
    and no version row changes: rollback is purely a pointer move, which is
    why it is instant and reversible.
    """
    dataset = await repo.get_dataset(conn, dataset_id, tenant_id=identity.tenant_id)
    if dataset is None or dataset.tenant_id != identity.tenant_id:
        # Invisible, or visible only because it is public.
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
    return _current_response(dataset, version)
