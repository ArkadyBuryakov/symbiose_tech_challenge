"""Scenarios 1-4 and 7: the publication lifecycle, through the public edge."""

from __future__ import annotations

import os

import httpx

from pmp_common.kafka import TOPIC_DLQ

from .conftest import API, BASE_URL, Archive, Session, inside


def test_1_publish_creates_version_1_served_with_range_requests(
    alice: Session, slug: str, archive_v1: Archive, spec: dict | None
) -> None:
    job = alice.publish_and_wait(slug, archive_v1, spec=spec)

    assert job["status"] == "SUCCEEDED", job
    assert job["result"] == "CREATED"
    assert job["result_version_seq"] == 1

    current = alice.current(job["dataset_id"])
    assert current["seq"] == 1
    assert current["sha256"] == archive_v1.sha256
    assert current["pmtiles_header"]["tile_type"] == "mvt"
    assert current["spec"] == spec

    # The map reads the archive with range requests; the edge must honour them.
    tile = httpx.get(f"{BASE_URL}{current['url']}", headers={"Range": "bytes=0-126"})
    assert tile.status_code == 206
    assert tile.headers["content-range"].startswith("bytes 0-126/")
    assert tile.headers["accept-ranges"] == "bytes"
    assert tile.headers.get("etag")
    assert "immutable" in tile.headers["cache-control"]
    assert tile.content[:7] == b"PMTiles"

    # The indirection is cached briefly, the archive forever.
    head = alice.get(f"{API}/datasets/{job['dataset_id']}/current")
    assert "max-age=30" in head.headers["cache-control"]


def test_2_replaying_an_idempotency_key_returns_the_same_job(
    alice: Session, slug: str, archive_v1: Archive
) -> None:
    source = alice.stage(archive_v1)
    key = f"e2e-idempotent-{slug}"

    first = alice.publish(slug, source, key=key)
    second = alice.publish(slug, source, key=key)

    assert second["job_id"] == first["job_id"]
    assert second["idempotent_replay"] is True
    assert first["idempotent_replay"] is False


def test_3_identical_content_under_a_new_key_is_deduplicated(
    alice: Session, slug: str, archive_v1: Archive
) -> None:
    first = alice.publish_and_wait(slug, archive_v1)
    second = alice.publish_and_wait(slug, archive_v1)

    assert first["result"] == "CREATED"
    assert second["status"] == "SUCCEEDED"
    assert second["result"] == "DEDUPLICATED"
    assert second["result_version_seq"] == 1
    assert len(alice.versions(first["dataset_id"])) == 1


def test_3b_same_bytes_with_a_new_spec_is_a_new_version_sharing_the_object(
    alice: Session, slug: str, archive_v1: Archive, spec: dict | None
) -> None:
    """Regression: republishing identical bytes with an updated spec used to be
    DEDUPLICATED, silently discarding the new spec."""
    original = spec or {"style": {"color_field": "count"}}
    updated = {**original, "style": {**original.get("style", {}), "max": 999}}

    first = alice.publish_and_wait(slug, archive_v1, spec=original)
    second = alice.publish_and_wait(slug, archive_v1, spec=updated)
    dataset_id = first["dataset_id"]

    assert first["result"] == "CREATED"
    assert second["result"] == "CREATED"
    assert second["result_version_seq"] == 2

    current = alice.current(dataset_id)
    assert current["spec"]["style"]["max"] == 999
    v1, v2 = sorted(alice.versions(dataset_id), key=lambda v: v["seq"])
    # Same bytes: one stored object shared by both versions, no second copy.
    assert v1["sha256"] == v2["sha256"] == archive_v1.sha256
    assert v1["spec_sha256"] != v2["spec_sha256"]
    tile = httpx.get(f"{BASE_URL}{current['url']}", headers={"Range": "bytes=0-6"})
    assert tile.status_code == 206

    # Identical bytes *and* spec is still a no-op.
    again = alice.publish_and_wait(slug, archive_v1, spec=updated)
    assert again["result"] == "DEDUPLICATED"

    # Rolling back restores the old styling, not just the old pointer.
    assert alice.put(f"{API}/datasets/{dataset_id}/current", json={"seq": 1}).status_code == 200
    assert alice.current(dataset_id)["spec"] == original

    # And republishing the original pair is recognised as that version.
    alice.put(f"{API}/datasets/{dataset_id}/current", json={"seq": 2})
    back = alice.publish_and_wait(slug, archive_v1, spec=original)
    assert back["result"] == "POINTER_MOVED"
    assert back["result_version_seq"] == 1


def test_4_new_content_becomes_version_2_and_rollback_restores_version_1(
    alice: Session, slug: str, archive_v1: Archive, archive_v2: Archive
) -> None:
    first = alice.publish_and_wait(slug, archive_v1)
    second = alice.publish_and_wait(slug, archive_v2)
    dataset_id = first["dataset_id"]

    assert second["result"] == "CREATED"
    assert second["result_version_seq"] == 2
    assert alice.current(dataset_id)["sha256"] == archive_v2.sha256

    rollback = alice.put(f"{API}/datasets/{dataset_id}/current", json={"seq": 1})
    assert rollback.status_code == 200, rollback.text

    current = alice.current(dataset_id)
    assert current["seq"] == 1
    assert current["sha256"] == archive_v1.sha256
    # Rollback is a pointer move: both versions are still there.
    assert [v["seq"] for v in alice.versions(dataset_id)] == [2, 1]

    # Publishing v2's bytes again moves the pointer rather than copying.
    again = alice.publish_and_wait(slug, archive_v2)
    assert again["result"] == "POINTER_MOVED"
    assert alice.current(dataset_id)["seq"] == 2


def test_4b_members_cannot_roll_back(
    alice: Session, bob: Session, slug: str, archive_v1: Archive
) -> None:
    """Rollback is an owner/admin action: a member of the owning tenant gets 403,
    and a caller from another tenant is refused too."""
    own = bob.publish_and_wait(slug, archive_v1)  # bob is a member of tenant-b
    assert own["status"] == "SUCCEEDED", own
    member = bob.put(f"{API}/datasets/{own['dataset_id']}/current", json={"seq": 1})
    assert member.status_code == 403, member.text

    other = alice.publish_and_wait(slug, archive_v1)
    foreign = bob.put(f"{API}/datasets/{other['dataset_id']}/current", json={"seq": 1})
    assert foreign.status_code in (403, 404), foreign.text


def test_7_invalid_file_fails_permanently_and_retry_works_once_fixed(
    alice: Session, slug: str, archive_v1: Archive
) -> None:
    source = alice.stage(b"this is not a pmtiles archive" * 10)
    accepted = alice.publish(slug, source)
    job = alice.wait(accepted["job_id"])

    assert job["status"] == "FAILED"
    assert job["error_code"] == "INVALID_PMTILES"
    assert job["attempts"] == 1, "a permanent failure must not be retried"

    dlq = inside(
        "kafka",
        f"rpk topic consume {TOPIC_DLQ} -o :end -f '%v\\n' 2>/dev/null || true",
    )
    assert accepted["job_id"] not in dlq, "permanent failures must not be dead-lettered"

    # The producer fixes its output in place (it owns the staging prefix).
    bucket = os.environ.get("S3_STAGING_BUCKET", "staging")
    compose_cp_into_staging(archive_v1, bucket, source)

    retried = alice.post(f"{API}/publications/{accepted['job_id']}/retry")
    assert retried.status_code == 202, retried.text

    job = alice.wait(accepted["job_id"])
    assert job["status"] == "SUCCEEDED"
    assert job["result"] == "CREATED"
    assert job["error_code"] is None


def test_7b_only_failed_jobs_can_be_retried(alice: Session, slug: str, archive_v1: Archive) -> None:
    job = alice.publish_and_wait(slug, archive_v1)

    response = alice.post(f"{API}/publications/{job['id']}/retry")

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")


def compose_cp_into_staging(archive: Archive, bucket: str, key: str) -> None:
    """Overwrite a staged object from inside the network, as the producer would."""
    from .conftest import compose

    compose("cp", str(archive.path), "s3:/tmp/fixed.pmtiles")
    inside(
        "s3",
        'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" '
        f">/dev/null && mc cp -q /tmp/fixed.pmtiles local/{bucket}/{key} >/dev/null",
    )
