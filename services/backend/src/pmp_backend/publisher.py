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

from typing import Any

from structlog.contextvars import get_contextvars

from pmp_common.enums import Visibility
from pmp_common.events import PublicationRequested, encode_event
from pmp_common.kafka import TOPIC_REQUESTED, AsyncProducer
from pmp_common.logging import get_logger
from pmp_common.tracing import merge_headers

__all__ = ["emit_publication_requested"]

log = get_logger(__name__)


async def emit_publication_requested(producer: AsyncProducer, *, job: Any, dataset: Any) -> None:
    """Emit the request for a committed job. Failures only log: the job stays
    PENDING and the reconciler re-emits it."""
    event = PublicationRequested(
        tenant_id=job.tenant_id,
        dataset_id=job.dataset_id,
        job_id=job.id,
        source_key=job.source_key,
        visibility=Visibility(dataset.visibility),
    )
    request_id = get_contextvars().get("request_id")
    try:
        await producer.produce(
            TOPIC_REQUESTED,
            # Partitioning by dataset keeps all work for one dataset in order
            # and on one consumer, so the per-dataset row lock in the worker is
            # uncontended.
            key=str(job.dataset_id),
            value=encode_event(event),
            headers=merge_headers({"x-request-id": request_id}),
        )
    except Exception as exc:  # DeliveryError, BufferError, KafkaException
        log.warning(
            "publication.produce_failed",
            job_id=str(job.id),
            error=str(exc),
            recovery="reconciler will re-emit this PENDING job",
        )
        return
    log.info("publication.requested", job_id=str(job.id), dataset_id=str(job.dataset_id))
