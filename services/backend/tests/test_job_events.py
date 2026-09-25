"""Fan-out of job notifications to per-tenant SSE queues."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from pmp_backend.job_events import JobEventHub


async def test_notifications_reach_only_the_owning_tenant() -> None:
    hub = JobEventHub()
    a1, a2, b = hub.subscribe("tenant-a"), hub.subscribe("tenant-a"), hub.subscribe("tenant-b")
    job = uuid4()

    hub.publish("tenant-a", job)

    assert a1.get_nowait() == job
    assert a2.get_nowait() == job
    assert b.empty()


async def test_unsubscribed_queue_gets_nothing() -> None:
    hub = JobEventHub()
    queue = hub.subscribe("tenant-a")
    hub.unsubscribe("tenant-a", queue)

    hub.publish("tenant-a", uuid4())

    assert queue.empty()


async def test_full_queue_drops_instead_of_blocking() -> None:
    hub = JobEventHub()
    queue = hub.subscribe("tenant-a")
    for _ in range(queue.maxsize + 5):
        hub.publish("tenant-a", uuid4())

    assert queue.full()
    await asyncio.sleep(0)  # nothing is left pending on the loop
