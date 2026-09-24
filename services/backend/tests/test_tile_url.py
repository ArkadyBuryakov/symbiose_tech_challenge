"""The tile URL is the contract between the catalogue and the edge."""

from __future__ import annotations

from uuid import UUID

from pmp_backend.routers.datasets import tile_url
from pmp_backend.storage import STAGING_UPLOAD_PREFIX, Storage
from pmp_common.config import S3Settings

DATASET = UUID("11111111-2222-3333-4444-555555555555")
SHA = "b" * 64


def test_public_tile_url_matches_the_edge_route() -> None:
    url = tile_url(visibility="public", tenant_id="org_a", dataset_id=DATASET, sha256=SHA)

    assert url == f"/tiles/public/org_a/{DATASET}/{SHA}/data.pmtiles"


def test_private_tile_url_lands_under_the_authenticated_edge_route() -> None:
    url = tile_url(visibility="private", tenant_id="org_a", dataset_id=DATASET, sha256=SHA)

    assert url.startswith("/tiles/private/org_a/")


def test_tile_url_is_relative_so_it_works_behind_any_origin() -> None:
    """Returning a path rather than an absolute URL means the same response is
    correct behind localhost and behind a CloudFront domain."""
    assert tile_url(visibility="public", tenant_id="t", dataset_id=DATASET, sha256=SHA).startswith(
        "/"
    )


def test_url_changes_when_the_content_changes() -> None:
    a = tile_url(visibility="public", tenant_id="t", dataset_id=DATASET, sha256="a" * 64)
    b = tile_url(visibility="public", tenant_id="t", dataset_id=DATASET, sha256="c" * 64)

    assert a != b


def test_presigned_put_is_rewritten_onto_the_edge_origin() -> None:
    """SigV4 signs the Host, so only the origin may be swapped: the path, the
    query string and therefore the signature must survive intact."""
    storage = Storage(
        S3Settings(
            endpoint="http://s3:9000",
            force_path_style=True,
            access_key_id="key",
            secret_access_key="secret",
        ),
        public_base_url="http://localhost:8080",
    )

    url, headers = storage.presign_staging_put(
        key="org_a/upload-1/data.pmtiles", sha256_b64=None, content_length=None
    )

    assert url.startswith(f"http://localhost:8080{STAGING_UPLOAD_PREFIX}/staging/org_a/upload-1/")
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Credential=" in url
    assert headers["content-type"] == "application/vnd.pmtiles"


def test_declared_checksum_is_bound_into_the_signature() -> None:
    """The browser must send exactly the digest it declared, so the bytes
    cannot be swapped after the URL has been issued."""
    storage = Storage(
        S3Settings(
            endpoint="http://s3:9000",
            force_path_style=True,
            access_key_id="key",
            secret_access_key="secret",
        ),
        public_base_url="http://localhost:8080",
    )

    url, headers = storage.presign_staging_put(
        key="org_a/u/data.pmtiles", sha256_b64="3q2+7w==", content_length=10
    )

    assert headers["x-amz-checksum-sha256"] == "3q2+7w=="
    assert "x-amz-checksum-sha256" in url.lower()
