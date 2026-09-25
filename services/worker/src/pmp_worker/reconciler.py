"""Reconciler: the safety net for work that fell through a crack.

Two situations leave a job that nobody is going to pick up:

1. The backend committed a ``PENDING`` job but the Kafka produce failed (or the
   process died between the two). No message exists.
2. A worker claimed a job, then died. The job is ``RUNNING`` with a lease that
   has since expired, and the original message's offset may already be
   committed.

Both are fixed the same way: re-emit ``publication.requested``. Because the
worker claims with a conditional UPDATE and the publish key is content
addressed, a spurious re-emission is harmless — it either finds nothing to
claim or redoes work that lands in exactly the same place.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from confluent_kafka import Producer
from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from pmp_common.enums import JobStatus, Visibility
from pmp_common.events import PublicationRequested, encode_event
from pmp_common.kafka import TOPIC_REQUESTED
from pmp_common.logging import get_logger
from pmp_common.metrics import RECONCILER_REQUEUED
from pmp_common.tables import datasets, publication_jobs

from .outbox import KafkaMessage, produce_confirmed

__all__ = ["Reconciler"]

log = get_logger(__name__)


class Reconciler:
    def __init__(
        self,
        engine: Engine,
        producer: Producer,
        *,
        interval_seconds: float,
        stuck_pending_seconds: int,
        batch_size: int,
    ) -> None:
        self._engine = engine
        self._producer = producer
        self._interval = interval_seconds
        self._stuck_after = timedelta(seconds=stuck_pending_seconds)
        self._batch_size = batch_size
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="reconciler", daemon=True)
        self._thread.start()
        log.info("reconciler.started", interval=self._interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Wait one interval first so several replicas do not all scan at once.
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except SQLAlchemyError as exc:
                log.warning("reconciler.error", error=str(exc))

    def run_once(self) -> int:
        """Re-emit every recoverable job. Returns how many the broker acknowledged."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    publication_jobs.c.id,
                    publication_jobs.c.tenant_id,
                    publication_jobs.c.dataset_id,
                    publication_jobs.c.source_key,
                    publication_jobs.c.attempts,
                    publication_jobs.c.status,
                    datasets.c.visibility,
                )
                .select_from(
                    publication_jobs.join(datasets, datasets.c.id == publication_jobs.c.dataset_id)
                )
                .where(
                    or_(
                        and_(
                            publication_jobs.c.status == JobStatus.PENDING.value,
                            publication_jobs.c.updated_at < func.now() - self._stuck_after,
                        ),
                        and_(
                            publication_jobs.c.status == JobStatus.RUNNING.value,
                            publication_jobs.c.lease_expires_at < func.now(),
                        ),
                    )
                )
                .order_by(publication_jobs.c.updated_at)
                .limit(self._batch_size)
            ).all()

        messages = [
            KafkaMessage(
                topic=TOPIC_REQUESTED,
                key=str(row.dataset_id).encode(),
                value=encode_event(
                    PublicationRequested(
                        tenant_id=row.tenant_id,
                        dataset_id=row.dataset_id,
                        job_id=row.id,
                        source_key=row.source_key,
                        visibility=Visibility(row.visibility),
                    )
                ),
                headers=[("x-reconciled", b"1")],
            )
            for row in rows
        ]
        delivered = produce_confirmed(self._producer, messages) if messages else []

        for row, ok in zip(rows, delivered, strict=True):
            if not ok:
                continue
            reason = "stuck_pending" if row.status == JobStatus.PENDING.value else "expired_lease"
            RECONCILER_REQUEUED.labels(reason).inc()
            log.warning(
                "reconciler.requeued",
                job_id=str(row.id),
                dataset_id=str(row.dataset_id),
                reason=reason,
                attempts=row.attempts,
            )
        return sum(delivered)
