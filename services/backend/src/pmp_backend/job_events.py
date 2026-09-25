"""Live job-status fan-out for ``GET /publications/events`` (SSE).

Each backend process runs one Kafka consumer on ``publication.requested`` and
``publication.results`` in its own throwaway consumer group, starting at the
latest offset and never committing: it is a live tail, not a work queue, so
every replica sees every event. Each event is turned into a ``(tenant_id,
job_id)`` notification and handed to the asyncio queues of that tenant's open
streams. The stream handler then reads the job row itself, so what the browser
receives is always the authoritative, tenant-scoped row, never event payload.

A slow browser cannot stall the consumer: queues are bounded and a full queue
drops the notification (the client re-lists jobs on reconnect anyway).
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from uuid import UUID

from confluent_kafka import Consumer

from pmp_common.config import KafkaSettings
from pmp_common.kafka import TOPIC_REQUESTED, TOPIC_RESULTS, make_consumer
from pmp_common.logging import get_logger

__all__ = ["JobEventHub"]

log = get_logger(__name__)

_QUEUE_SIZE = 100


class JobEventHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[UUID]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- asyncio side ------------------------------------------------------
    def subscribe(self, tenant_id: str) -> asyncio.Queue[UUID]:
        queue: asyncio.Queue[UUID] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers.setdefault(tenant_id, set()).add(queue)
        return queue

    def unsubscribe(self, tenant_id: str, queue: asyncio.Queue[UUID]) -> None:
        queues = self._subscribers.get(tenant_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del self._subscribers[tenant_id]

    def publish(self, tenant_id: str, job_id: UUID) -> None:
        """Deliver to every open stream of ``tenant_id``. Loop thread only."""
        for queue in self._subscribers.get(tenant_id, ()):
            try:
                queue.put_nowait(job_id)
            except asyncio.QueueFull:
                log.warning("job_events.dropped", tenant_id=tenant_id, job_id=str(job_id))

    # --- Kafka side --------------------------------------------------------
    def start(self, settings: KafkaSettings, *, client_id: str) -> None:
        self._loop = asyncio.get_running_loop()
        consumer = make_consumer(
            settings,
            client_id=client_id,
            group_id=f"backend-job-events-{uuid.uuid4()}",
            overrides={"auto.offset.reset": "latest"},
        )
        consumer.subscribe([TOPIC_REQUESTED, TOPIC_RESULTS])
        self._thread = threading.Thread(
            target=self._consume, args=(consumer,), name="job-events", daemon=True
        )
        self._thread.start()

    def _consume(self, consumer: Consumer) -> None:
        loop = self._loop
        assert loop is not None
        try:
            while not self._stop.is_set():
                message = consumer.poll(0.5)
                if message is None or message.error():
                    continue
                try:
                    event = json.loads(message.value() or b"")
                    tenant_id, job_id = str(event["tenant_id"]), UUID(event["job_id"])
                except (ValueError, KeyError, TypeError):
                    continue
                loop.call_soon_threadsafe(self.publish, tenant_id, job_id)
        finally:
            consumer.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
