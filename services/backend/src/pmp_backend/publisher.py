"""Producing ``publication.requested``.

The backend is the only service that produces this topic directly (the worker's
reconciler re-emits it). It deliberately produces *after* committing the job
row, never inside the transaction:

* commit-then-produce can lose the message (fixed by the reconciler, which
  re-emits PENDING jobs that are older than a threshold);
* produce-then-commit can publish a message that references a job that does not
  exist, which the worker cannot do anything useful with.

Losing a message is recoverable; a dangling reference is not, so the order is
commit first.
"""

from __future__ import annotations

from uuid import UUID

from pmp_common.enums import Visibility
from pmp_common.events import PublicationRequested, encode_event
from pmp_common.kafka import AsyncProducer, DeliveryError
from pmp_common.logging import get_logger
from pmp_common.tracing import merge_headers

__all__ = ["PublicationPublisher"]

log = get_logger(__name__)


class PublicationPublisher:
    """Thin domain wrapper over the Kafka producer."""

    def __init__(self, producer: AsyncProducer, *, topic: str) -> None:
        self._producer = producer
        self._topic = topic

    async def publication_requested(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
        dataset_slug: str,
        job_id: UUID,
        source_key: str,
        visibility: Visibility,
        requested_by: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> bool:
        """Emit the request. Returns False if delivery failed (job stays PENDING)."""
        event = PublicationRequested(
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            job_id=job_id,
            dataset_slug=dataset_slug,
            source_key=source_key,
            visibility=visibility,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
        )
        try:
            await self._producer.produce(
                self._topic,
                # Partitioning by dataset keeps all work for one dataset in
                # order and on one consumer, so the per-dataset row lock in the
                # worker is uncontended.
                key=str(dataset_id),
                value=encode_event(event),
                headers=merge_headers({"x-request-id": request_id}),
            )
        except DeliveryError as exc:
            log.warning(
                "publication.produce_failed",
                job_id=str(job_id),
                dataset_id=str(dataset_id),
                error=str(exc),
                recovery="reconciler will re-emit this PENDING job",
            )
            return False
        log.info("publication.requested", job_id=str(job_id), dataset_id=str(dataset_id))
        return True
