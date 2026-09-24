"""S3 client factory and object-key helpers.

Locally this talks to MinIO over ``S3_ENDPOINT`` with static credentials; on
AWS both are unset, so boto3 falls back to the regional endpoint and the
default credential chain (EKS Pod Identity). No code changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from .config import S3Settings
from .enums import Visibility

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.client import S3Client
else:  # pragma: no cover
    S3Client = Any

__all__ = [
    "IMMUTABLE_CACHE_CONTROL",
    "PMTILES_CONTENT_TYPE",
    "make_s3_client",
    "publish_key",
    "staging_key",
    "staging_prefix",
]

PMTILES_CONTENT_TYPE = "application/vnd.pmtiles"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def make_s3_client(
    settings: S3Settings,
    *,
    signature_version: str = "s3v4",
    addressing_style: Literal["path", "virtual", "auto"] | None = None,
    max_attempts: int = 5,
) -> S3Client:
    """Build a boto3 S3 client from settings.

    ``signature_version`` is exposed because presigned URLs and normal calls can
    need different signers; everything here uses SigV4.
    """
    import boto3
    from botocore.config import Config

    style = addressing_style or ("path" if settings.force_path_style else "auto")
    config = Config(
        signature_version=signature_version,
        s3={"addressing_style": style},
        retries={"max_attempts": max_attempts, "mode": "standard"},
        connect_timeout=5,
        read_timeout=60,
        # MinIO accepts the newer default checksum behaviour, but being explicit
        # keeps presigned-PUT signing predictable across boto3 versions.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )

    kwargs: dict[str, Any] = {"config": config, "region_name": settings.region}
    if settings.endpoint:
        kwargs["endpoint_url"] = settings.endpoint
    if settings.access_key_id and settings.secret_access_key:
        kwargs["aws_access_key_id"] = settings.access_key_id
        kwargs["aws_secret_access_key"] = settings.secret_access_key
    client: S3Client = boto3.client("s3", **kwargs)
    return client


# --------------------------------------------------------------------------
# Key layout
# --------------------------------------------------------------------------
def staging_prefix(tenant_id: str) -> str:
    """Prefix a tenant is allowed to write to in the staging bucket."""
    return f"{tenant_id}/"


def staging_key(tenant_id: str, upload_id: str | UUID) -> str:
    """Write-once key for one staged upload."""
    return f"{tenant_id}/{upload_id}/data.pmtiles"


def publish_key(
    *,
    visibility: Visibility | str,
    tenant_id: str,
    dataset_id: str | UUID,
    sha256: str,
) -> str:
    """Immutable, content-addressed key in the publish bucket.

    Content addressing is what makes republishing idempotent: the same bytes
    always land on the same key, so a retried job re-copies to the same place
    and the CDN can cache it forever.
    """
    visibility = Visibility(visibility)
    return f"{visibility.value}/{tenant_id}/{dataset_id}/{sha256}/data.pmtiles"
