"""Transactional outbox relay, plus confirmed Kafka produce.

Result events are written to ``catalog.outbox`` in the same transaction as the
job's terminal state, then moved onto Kafka by this loop. ``FOR UPDATE SKIP
LOCKED`` lets several replicas relay concurrently. A row can still be produced
twice (crash between produce and ``sent_at``), so consumers deduplicate on
``event_id``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from confluent_kafka import KafkaError, KafkaException, Message, Producer
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from pmp_common.logging import get_logger
from pmp_common.metrics import OUTBOX_PENDING, OUTBOX_RELAYED
from pmp_common.tables import outbox

__all__ = ["KafkaMessage", "OutboxRelay", "produce_confirmed"]

log = get_logger(__name__)


class KafkaMessage(NamedTuple):
    topic: str
    key: bytes | None
    value: bytes | None
    headers: list[tuple[str, str | bytes | None]]


def produce_confirmed(
    producer: Producer, messages: Sequence[KafkaMessage], timeout: float = 10.0
) -> list[bool]:
    """Produce ``messages`` and report, per message, whether the broker acked it.

    ``flush()`` never raises and ``produce()`` only fails locally, so broker
    acknowledgement is taken from the delivery callbacks alone. One flush per
    call, not per message.
    """
    delivered = [False] * len(messages)

    def on_delivery(index: int) -> Callable[[KafkaError | None, Message], None]:
        def callback(err: KafkaError | None, _msg: Message) -> None:
            if err is None:
                delivered[index] = True
            else:
                log.warning("kafka.delivery_failed", topic=messages[index].topic, error=str(err))

        return callback

    for index, message in enumerate(messages):
        try:
            producer.produce(
                message.topic,
                key=message.key,
                value=message.value,
                headers=message.headers,
                on_delivery=on_delivery(index),
            )
        except (BufferError, KafkaException) as exc:
            log.warning("kafka.produce_failed", topic=message.topic, error=str(exc))
    producer.flush(timeout)
    return delivered


class OutboxRelay:
    """Background thread that drains the outbox onto Kafka."""

    def __init__(
        self, engine: Engine, producer: Producer, *, batch_size: int, interval_seconds: float
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
                # The rows are still there; the next pass picks them up.
                log.warning("outbox.relay_error", error=str(exc))
                relayed = 0
            # Back off only when there was nothing to do, so a backlog drains fast.
            self._stop.wait(0 if relayed == self._batch_size else self._interval)

    def drain_once(self) -> int:
        """Relay one batch. Returns how many rows the broker acknowledged."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                select(outbox)
                .where(outbox.c.sent_at.is_(None))
                .order_by(outbox.c.created_at)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            ).all()

            delivered = produce_confirmed(self._producer, [_to_message(r) for r in rows])
            sent = [row for row, ok in zip(rows, delivered, strict=True) if ok]
            if sent:
                conn.execute(
                    update(outbox)
                    .where(outbox.c.id.in_([row.id for row in sent]))
                    .values(sent_at=func.now())
                )
            for row in sent:
                OUTBOX_RELAYED.labels(row.topic).inc()

            OUTBOX_PENDING.set(
                conn.execute(
                    select(func.count()).select_from(outbox).where(outbox.c.sent_at.is_(None))
                ).scalar_one()
            )
        return len(sent)


def _to_message(row: Any) -> KafkaMessage:
    return KafkaMessage(
        topic=row.topic,
        key=str(row.key).encode(),
        value=json.dumps(row.payload, separators=(",", ":")).encode(),
        headers=[(k, str(v).encode()) for k, v in (row.headers or {}).items()],
    )
