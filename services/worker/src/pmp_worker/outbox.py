"""Transactional outbox relay.

Result events are written to ``catalog.outbox`` in the same transaction as the
job's terminal state, then moved onto Kafka by this loop. That is what makes
"the job says SUCCEEDED but nobody was told" impossible.

``FOR UPDATE SKIP LOCKED`` lets several worker replicas relay concurrently
without coordinating: each grabs a disjoint batch. A row can still be produced
twice (crash between produce and ``sent_at``), so consumers deduplicate on
``event_id`` — at-least-once, like everything else here.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any

from confluent_kafka import KafkaException, Producer
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from pmp_common.logging import get_logger, log_context
from pmp_common.metrics import OUTBOX_PENDING, OUTBOX_RELAYED
from pmp_common.tables import outbox

__all__ = ["OutboxRelay"]

log = get_logger(__name__)


class OutboxRelay:
    """Background thread that drains the outbox onto Kafka."""

    def __init__(
        self,
        engine: Engine,
        producer: Producer,
        *,
        batch_size: int,
        interval_seconds: float,
    ) -> None:
        self._engine = engine
        self._producer = producer
        self._batch_size = batch_size
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="outbox-relay", daemon=True)
        self._thread.start()
        log.info("outbox.relay_started", batch_size=self._batch_size, interval=self._interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                relayed = self.drain_once()
            except SQLAlchemyError as exc:
                # The database being briefly unavailable is normal; the rows are
                # still there and will be picked up on the next pass.
                log.warning("outbox.relay_error", error=str(exc))
                relayed = 0
            # Back off only when there was nothing to do, so a backlog drains
            # as fast as the broker allows.
            self._stop.wait(0 if relayed == self._batch_size else self._interval)

    def drain_once(self) -> int:
        """Relay one batch. Returns how many rows were produced."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                select(outbox)
                .where(outbox.c.sent_at.is_(None))
                .order_by(outbox.c.created_at)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            ).all()

            if not rows:
                self._record_backlog(conn)
                return 0

            sent_ids: list[Any] = []
            for row in rows:
                if self._produce(row):
                    sent_ids.append(row.id)

            if sent_ids:
                conn.execute(
                    update(outbox)
                    .where(outbox.c.id.in_(sent_ids))
                    .values(sent_at=datetime.now(UTC))
                )
            conn.execute(
                update(outbox)
                .where(outbox.c.id.in_([r.id for r in rows]))
                .values(attempts=outbox.c.attempts + 1)
            )
            self._record_backlog(conn)
        return len(sent_ids)

    def _produce(self, row: Any) -> bool:
        with log_context(job_id=row.payload.get("job_id"), dataset_id=row.key):
            try:
                self._producer.produce(
                    row.topic,
                    key=str(row.key).encode(),
                    value=json.dumps(row.payload, separators=(",", ":")).encode(),
                    headers=[(k, str(v).encode()) for k, v in (row.headers or {}).items()],
                )
                # Block until this batch is acknowledged: `sent_at` must not be
                # set for a message the broker never took.
                self._producer.flush(10.0)
            except (BufferError, KafkaException) as exc:
                log.warning("outbox.produce_failed", topic=row.topic, error=str(exc))
                return False
            OUTBOX_RELAYED.labels(row.topic).inc()
            return True

    @staticmethod
    def _record_backlog(conn: Any) -> None:
        pending = conn.execute(
            select(func.count()).select_from(outbox).where(outbox.c.sent_at.is_(None))
        ).scalar_one()
        OUTBOX_PENDING.set(pending)
