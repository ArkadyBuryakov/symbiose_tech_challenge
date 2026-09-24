"""Object-storage operations the backend performs.

The backend's S3 rights are deliberately tiny: presign a PUT into staging, and
read back a staged object's metadata. It never writes to the publish bucket —
that is the worker's job alone.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pmp_common.config import S3Settings
from pmp_common.logging import get_logger
from pmp_common.s3 import PMTILES_CONTENT_TYPE, make_s3_client

__all__ = ["STAGING_UPLOAD_PREFIX", "Storage"]

log = get_logger(__name__)

# nginx proxies this path to the S3 server, preserving the object key.
STAGING_UPLOAD_PREFIX = "/staging-upload"


class Storage:
    def __init__(self, settings: S3Settings, *, public_base_url: str) -> None:
        self._settings = settings
        self._client = make_s3_client(settings)
        self._public_base_url = public_base_url.rstrip("/")

    def presign_staging_put(
        self,
        *,
        key: str,
        sha256_b64: str | None,
        content_length: int | None,
        expires_in: int | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Presign a PUT into the staging bucket and rewrite it to go via the edge.

        SigV4 signs the ``Host`` header, so the URL must be signed against the
        endpoint the request will actually carry. It is signed for the internal
        S3 endpoint and only its *origin* is swapped for the public edge; nginx
        then proxies ``/staging-upload/<key>`` upstream with the original
        ``Host``, so the signature still verifies. This keeps the object store
        off the host network and sidesteps CORS entirely.

        ``x-amz-checksum-sha256`` is part of the signature, so the browser must
        send exactly the digest it declared — the upload cannot be swapped for
        different bytes after the URL is issued.
        """
        params: dict[str, Any] = {
            "Bucket": self._settings.staging_bucket,
            "Key": key,
            "ContentType": PMTILES_CONTENT_TYPE,
        }
        headers = {"content-type": PMTILES_CONTENT_TYPE}
        if sha256_b64:
            params["ChecksumSHA256"] = sha256_b64
            headers["x-amz-checksum-sha256"] = sha256_b64
        if content_length:
            params["ContentLength"] = content_length

        signed = self._client.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=expires_in or self._settings.presign_expiry_seconds,
            HttpMethod="PUT",
        )
        return self._rewrite_to_edge(signed), headers

    def _rewrite_to_edge(self, url: str) -> str:
        """Swap the internal S3 origin for the public edge, keeping path+query."""
        internal = urlsplit(url)
        edge = urlsplit(self._public_base_url)
        return urlunsplit(
            (
                edge.scheme,
                edge.netloc,
                f"{STAGING_UPLOAD_PREFIX}{internal.path}",
                internal.query,
                "",
            )
        )

    def head_staged_object(self, key: str) -> dict[str, Any] | None:
        """Return metadata for a staged object, or None if it is not there."""
        try:
            response = self._client.head_object(Bucket=self._settings.staging_bucket, Key=key)
        except Exception as exc:  # botocore raises ClientError subclasses
            if getattr(exc, "response", {}).get("Error", {}).get("Code") in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                return None
            raise
        return {
            "size_bytes": response["ContentLength"],
            "etag": response.get("ETag", "").strip('"'),
            "content_type": response.get("ContentType"),
        }

    def healthy(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self._settings.staging_bucket)
        except Exception:
            return False
        return True
