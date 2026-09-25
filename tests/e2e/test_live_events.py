"""Live job updates: the SSE stream is fed by Kafka and scoped to the tenant."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx

from .conftest import API, Archive, Session


class _Listener:
    """Reads ``event: job`` messages from the stream on a background thread."""

    def __init__(self, session: Session) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.status: int | None = None
        self.connected = threading.Event()
        self.done = threading.Event()
        self._session = session
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self.connected.wait(10), "stream did not open"

    def _run(self) -> None:
        timeout = httpx.Timeout(10, read=60)
        with self._session.http.stream("GET", f"{API}/publications/events", timeout=timeout) as r:
            self.status = r.status_code
            self.content_type = r.headers.get("content-type", "")
            self.connected.set()
            if r.status_code != 200:
                return
            event = None
            for line in r.iter_lines():
                if self.done.is_set():
                    return
                if line.startswith("event: "):
                    event = line.removeprefix("event: ")
                elif line.startswith("data: ") and event == "job":
                    self.jobs.append(json.loads(line.removeprefix("data: ")))

    def statuses(self, job_id: str) -> list[str]:
        return [job["status"] for job in self.jobs if job["id"] == job_id]


def test_14_job_status_changes_stream_to_the_owning_tenant_only(
    alice: Session, bob: Session, slug: str, archive_v1: Archive
) -> None:
    mine, theirs = _Listener(alice), _Listener(bob)
    try:
        assert mine.status == 200
        assert mine.content_type.startswith("text/event-stream")

        job = alice.publish(slug, alice.stage(archive_v1))
        final = alice.wait(job["job_id"])
        assert final["status"] == "SUCCEEDED", final

        # The result event goes through the worker's outbox relay, so allow it
        # a moment after the job row is already terminal.
        for _ in range(40):
            if "SUCCEEDED" in mine.statuses(job["job_id"]):
                break
            time.sleep(0.25)
        assert "SUCCEEDED" in mine.statuses(job["job_id"]), mine.jobs
        assert theirs.statuses(job["job_id"]) == []
    finally:
        mine.done.set()
        theirs.done.set()


def test_14b_the_stream_requires_a_signed_in_caller(anon: Session) -> None:
    response = anon.get(f"{API}/publications/events")
    assert response.status_code == 401
