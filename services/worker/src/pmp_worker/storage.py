"""Object-storage work: validate, hash, copy.

The worker never buffers an archive in memory or on disk. It reads the 127-byte
header with a ranged GET, streams the object through SHA-256 in chunks, and
then asks S3 to copy the bytes server-side — the data never transits the worker
at all for the copy step.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from pmp_common.config import S3Settings
from pmp_common.enums import ErrorCode
from pmp_common.logging import get_logger
from pmp_common.pmtiles import HEADER_SIZE, InvalidPMTiles, PMTilesHeader, parse_header
from pmp_common.s3 import IMMUTABLE_CACHE_CONTROL, PMTILES_CONTENT_TYPE, make_s3_client
from pmp_common.tracing import SpanKind, traced

from .multipart import MAX_SINGLE_COPY_BYTES, plan_copy_parts

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_s3.type_defs import CompletedPartTypeDef

__all__ = ["PermanentJobError", "SourceObject", "Storage", "TransientJobError"]

log = get_logger(__name__)

_MISSING_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})
_FORBIDDEN_CODES = frozenset({"403", "AccessDenied"})


class PermanentJobError(Exception):
    """The job can never succeed as requested; do not retry, do not dead-letter."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class TransientJobError(Exception):
    """A retryable failure (network, storage, throttling)."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SourceObject:
    key: str
    size_bytes: int
    etag: str
    header: PMTilesHeader


class Storage:
    def __init__(self, settings: S3Settings) -> None:
        self._settings = settings
        self._client = make_s3_client(settings)
        self.staging_bucket = settings.staging_bucket
        self.publish_bucket = settings.publish_bucket

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def inspect_source(self, key: str) -> SourceObject:
        """Validate a staged object cheaply, with one HEAD and one 127-byte GET.

        A malformed archive is a *permanent* failure: retrying cannot fix the
        bytes, and publishing it would break the map for everyone.
        """
        with traced("worker.inspect_source", kind=SpanKind.CLIENT, **{"s3.key": key}):
            head = self._head(self.staging_bucket, key)
            size = int(head["ContentLength"])
            if size == 0:
                raise PermanentJobError(ErrorCode.EMPTY_SOURCE, f"staged object {key} is empty")
            if size < HEADER_SIZE:
                raise PermanentJobError(
                    ErrorCode.INVALID_PMTILES,
                    f"staged object {key} is {size} bytes, too small to be a PMTiles archive",
                )

            raw = self._get_range(self.staging_bucket, key, 0, HEADER_SIZE - 1)
            try:
                header = parse_header(raw)
            except InvalidPMTiles as exc:
                raise PermanentJobError(
                    ErrorCode.INVALID_PMTILES, f"{key} is not a valid PMTiles v3 archive: {exc}"
                ) from exc

            return SourceObject(
                key=key, size_bytes=size, etag=head.get("ETag", "").strip('"'), header=header
            )

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------
    def sha256_of(self, key: str, *, chunk_bytes: int) -> str:
        """Stream the staged object through SHA-256. Constant memory, no disk."""
        with traced("worker.hash_source", kind=SpanKind.CLIENT, **{"s3.key": key}):
            digest = hashlib.sha256()
            try:
                body = self._client.get_object(Bucket=self.staging_bucket, Key=key)["Body"]
                try:
                    while chunk := body.read(chunk_bytes):
                        digest.update(chunk)
                finally:
                    body.close()
            except ClientError as exc:
                raise self._classify(exc, key) from exc
            return digest.hexdigest()

    # ------------------------------------------------------------------
    # Copy
    # ------------------------------------------------------------------
    def copy_to_publish(self, *, source_key: str, target_key: str, size_bytes: int) -> None:
        """Server-side copy into the publish bucket, skipping work already done.

        Idempotent by construction: the target key is content-addressed, so a
        redelivered message copies the same bytes to the same place. If an
        object of the right size is already there, the copy is skipped entirely
        — which is what makes recovery after a crash between copy and commit
        cheap.
        """
        with traced(
            "worker.copy_to_publish",
            kind=SpanKind.CLIENT,
            **{"s3.target_key": target_key, "s3.size_bytes": size_bytes},
        ):
            if self._already_copied(target_key, size_bytes):
                log.info("worker.copy_skipped", target_key=target_key, reason="already present")
                return

            if size_bytes > MAX_SINGLE_COPY_BYTES:
                self._multipart_copy(source_key, target_key, size_bytes)
            else:
                self._single_copy(source_key, target_key)

    def _already_copied(self, target_key: str, size_bytes: int) -> bool:
        try:
            head = self._client.head_object(Bucket=self.publish_bucket, Key=target_key)
        except ClientError as exc:
            if _error_code(exc) in _MISSING_CODES:
                return False
            raise TransientJobError(
                ErrorCode.STORAGE_ERROR, f"cannot inspect {target_key}: {exc}"
            ) from exc
        return int(head["ContentLength"]) == size_bytes

    def _object_metadata(self) -> dict[str, Any]:
        return {
            "ContentType": PMTILES_CONTENT_TYPE,
            "CacheControl": IMMUTABLE_CACHE_CONTROL,
            "MetadataDirective": "REPLACE",
        }

    def _single_copy(self, source_key: str, target_key: str) -> None:
        try:
            self._client.copy_object(
                Bucket=self.publish_bucket,
                Key=target_key,
                CopySource={"Bucket": self.staging_bucket, "Key": source_key},
                **self._object_metadata(),
            )
        except ClientError as exc:
            raise self._classify(exc, source_key) from exc

    def _multipart_copy(self, source_key: str, target_key: str, size_bytes: int) -> None:
        parts = plan_copy_parts(size_bytes, part_size=self._settings.multipart_part_bytes)
        log.info("worker.multipart_copy_start", target_key=target_key, parts=len(parts))

        metadata = self._object_metadata()
        metadata.pop("MetadataDirective", None)  # not valid on CreateMultipartUpload
        upload_id = self._client.create_multipart_upload(
            Bucket=self.publish_bucket, Key=target_key, **metadata
        )["UploadId"]

        try:
            completed: list[CompletedPartTypeDef] = []
            for part in parts:
                result = self._client.upload_part_copy(
                    Bucket=self.publish_bucket,
                    Key=target_key,
                    UploadId=upload_id,
                    PartNumber=part.part_number,
                    CopySource={"Bucket": self.staging_bucket, "Key": source_key},
                    CopySourceRange=part.copy_source_range,
                )
                completed.append(
                    {"ETag": result["CopyPartResult"]["ETag"], "PartNumber": part.part_number}
                )
            self._client.complete_multipart_upload(
                Bucket=self.publish_bucket,
                Key=target_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": completed},
            )
        except Exception as exc:
            # Leaving an incomplete upload behind costs storage forever, so it
            # is aborted even on the failure path.
            try:
                self._client.abort_multipart_upload(
                    Bucket=self.publish_bucket, Key=target_key, UploadId=upload_id
                )
            except ClientError as abort_exc:  # pragma: no cover
                log.warning("worker.multipart_abort_failed", error=str(abort_exc))
            if isinstance(exc, ClientError):
                raise self._classify(exc, source_key) from exc
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _head(self, bucket: str, key: str) -> dict[str, Any]:
        try:
            return dict(self._client.head_object(Bucket=bucket, Key=key))
        except ClientError as exc:
            raise self._classify(exc, key) from exc

    def _get_range(self, bucket: str, key: str, first: int, last: int) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=bucket, Key=key, Range=f"bytes={first}-{last}"
            )
            body = response["Body"]
            try:
                return bytes(body.read())
            finally:
                body.close()
        except ClientError as exc:
            raise self._classify(exc, key) from exc

    @staticmethod
    def _classify(exc: ClientError, key: str) -> Exception:
        """Turn a botocore error into a permanent or transient job failure.

        The distinction drives everything downstream: permanent failures mark
        the job FAILED immediately, transient ones are retried with backoff and
        eventually dead-lettered.
        """
        code = _error_code(exc)
        if code in _MISSING_CODES:
            return PermanentJobError(
                ErrorCode.SOURCE_NOT_FOUND, f"staged object {key} does not exist"
            )
        if code in _FORBIDDEN_CODES:
            return PermanentJobError(
                ErrorCode.SOURCE_FORBIDDEN, f"not permitted to read {key} ({code})"
            )
        return TransientJobError(ErrorCode.STORAGE_ERROR, f"storage error on {key}: {exc}")

    def healthy(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self.publish_bucket)
        except Exception:
            return False
        return True


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))
