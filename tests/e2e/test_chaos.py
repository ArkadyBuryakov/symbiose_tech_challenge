"""Scenarios 5 and 6: at-least-once delivery with idempotent effects."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from pmp_common.kafka import TOPIC_REQUESTED as TOPIC

from .conftest import REPO_ROOT, Archive, Session, compose, inside


def test_5_a_duplicate_kafka_message_creates_one_version(
    alice: Session, slug: str, archive_v1: Archive
) -> None:
    job = alice.publish_and_wait(slug, archive_v1)
    dataset_id = job["dataset_id"]
    assert len(alice.versions(dataset_id)) == 1

    messages = inside("kafka", f"rpk topic consume {TOPIC} -o :end -f '%v\\n' 2>/dev/null")
    original = next(line for line in reversed(messages.splitlines()) if job["id"] in line)
    assert json.loads(original)["job_id"] == job["id"]

    compose(
        "exec",
        "-T",
        "kafka",
        "rpk",
        "topic",
        "produce",
        TOPIC,
        "-k",
        dataset_id,
        input_bytes=(original + "\n").encode(),
    )
    time.sleep(6)

    assert len(alice.versions(dataset_id)) == 1
    assert alice.get(f"/api/v1/publications/{job['id']}").json()["attempts"] == 1


def running_worker_env() -> dict[str, str]:
    """Recreate the worker from the image and tracing config it runs with now.

    Recomputing the image tag from ``git rev-parse HEAD`` breaks as soon as a
    commit lands after ``make up``: ``--no-build`` then asks for an image that
    was never built. Recreating without the OTLP endpoint would also silently
    switch tracing off under the observability profile.
    """
    image = compose("ps", "worker", "--format", "{{.Image}}").strip().splitlines()[0]
    env = {"GIT_SHA": image.rsplit(":", 1)[1]}
    container = compose("ps", "-q", "worker").strip().splitlines()[0]
    inspected = subprocess.run(
        ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in inspected.splitlines():
        if line.startswith("OTEL_EXPORTER_OTLP_ENDPOINT="):
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = line.split("=", 1)[1]
    return env


@pytest.mark.skipif(os.environ.get("E2E_SKIP_SLOW") == "1", reason="restarts the worker; ~40s")
def test_6_a_crash_between_copy_and_commit_recovers_to_the_same_version(
    alice: Session, slug: str, archive_v1: Archive
) -> None:
    env = {**os.environ, **running_worker_env()}

    def restart_worker(**extra: str) -> None:
        subprocess.run(
            ["docker", "compose", "up", "-d", "--no-build", "--force-recreate", "worker"],
            cwd=REPO_ROOT,
            env={**env, **extra},
            check=True,
            capture_output=True,
            timeout=120,
        )

    try:
        restart_worker(
            CHAOS_CRASH_AFTER_COPY="1",
            WORKER_LEASE_SECONDS="15",
            WORKER_RECONCILER_INTERVAL_SECONDS="10",
        )
        time.sleep(3)

        accepted = alice.publish(slug, alice.stage(archive_v1))
        # Wait for the crashing worker to claim, copy and exit. A fixed sleep
        # flakes: joining the consumer group can take longer than the job.
        deadline = time.monotonic() + 60
        while True:
            job = alice.get(f"/api/v1/publications/{accepted['job_id']}").json()
            if job["status"] == "RUNNING" or time.monotonic() > deadline:
                break
            time.sleep(1)
        assert job["status"] == "RUNNING", job

        restart_worker(WORKER_LEASE_SECONDS="15", WORKER_RECONCILER_INTERVAL_SECONDS="10")
        job = alice.wait(accepted["job_id"], timeout=120)
    finally:
        restart_worker()

    assert job["status"] == "SUCCEEDED"
    assert job["result"] == "CREATED"
    assert job["attempts"] >= 2, "the job should have been claimed again after the crash"

    versions = alice.versions(accepted["dataset_id"])
    assert len(versions) == 1
    assert versions[0]["seq"] == 1
    assert versions[0]["sha256"] == archive_v1.sha256
