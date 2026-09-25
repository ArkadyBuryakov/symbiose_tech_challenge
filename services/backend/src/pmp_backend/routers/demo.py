"""Browser upload endpoint for the demo page.

This stands in for the customer's private processing environment: it hands the
browser a short-lived presigned PUT into the staging bucket, after which the
demo page calls ``POST /publications`` exactly like any other client.

It is disabled unless ``DEMO_UPLOAD_ENABLED=true`` — in a real deployment
nothing outside the processing environment writes to staging.
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, status

from pmp_common.ids import new_uuid
from pmp_common.logging import get_logger
from pmp_common.problem import NotFound
from pmp_common.s3 import staging_key

from ..deps import Settings, Store
from ..identity import CurrentIdentity
from ..schemas import DemoUploadRequest, DemoUploadResponse

router = APIRouter(prefix="/demo", tags=["demo"])
log = get_logger(__name__)


@router.post(
    "/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=DemoUploadResponse,
    summary="Get a presigned PUT for a staged upload",
)
async def create_upload(
    body: DemoUploadRequest,
    identity: CurrentIdentity,
    settings: Settings,
    storage: Store,
) -> DemoUploadResponse:
    if not settings.demo_upload_enabled:
        # 404 rather than 403: a disabled feature should not be discoverable.
        raise NotFound("Demo uploads are not enabled on this deployment.")

    upload_id = new_uuid()
    key = staging_key(identity.require_tenant(), upload_id)

    url, headers = storage.presign_staging_put(
        key=key,
        # S3 checksum headers carry base64; the schema already enforced 64 hex chars.
        sha256_b64=base64.b64encode(bytes.fromhex(body.sha256)).decode(),
        content_length=body.content_length,
    )
    # The URL carries a signature; only the key is safe to log.
    log.info("demo.upload_presigned", upload_id=str(upload_id), source_key=key)

    return DemoUploadResponse(
        upload_id=upload_id,
        source_key=key,
        url=url,
        headers=headers,
        expires_in=settings.s3.presign_expiry_seconds,
    )
