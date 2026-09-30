"""Kafka producer/consumer construction and an asyncio-friendly producer.

Why a hand-rolled async wrapper
-------------------------------
``confluent_kafka`` is a C client with a blocking ``poll()`` loop.
``confluent_kafka.aio.AIOProducer`` exists in 2.15, but it batches produce
calls behind a buffer timeout, which makes single-message latency (our case:
one produce per accepted publication request) depend on tuning we would rather
not own. :class:`AsyncProducer` below is ~50 lines: the sync ``Producer`` plus a
dedicated poll thread that resolves an ``asyncio.Future`` from the delivery
callback via ``loop.call_soon_threadsafe``. Recorded in ``docs/DECISIONS.md``.

AWS MSK
-------
``KAFKA_SASL_MECHANISM=aws-msk-iam`` switches the client to SASL/OAUTHBEARER
with an ``oauth_cb`` backed by ``aws-msk-iam-sasl-signer-python``. That path is
a marked stub: it is not exercised by the local stack.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import Any

from confluent_kafka import Consumer, KafkaError, Message, Producer

from .config import KafkaSettings
from .logging import get_logger

__all__ = [
    "TOPIC_DLQ",
    "TOPIC_REQUESTED",
    "TOPIC_RESULTS",
    "AsyncProducer",
    "DeliveryError",
    "make_consumer",
    "make_producer",
]

log = get_logger(__name__)
KafkaHeaders = Sequence[tuple[str, bytes]]

# Topic names are part of the platform contract, not per-environment config.
TOPIC_REQUESTED = "publication.requested"
TOPIC_RESULTS = "publication.results"
TOPIC_DLQ = "publication.requested.dlq"


class DeliveryError(Exception):
    """A produced message was not acknowledged by the broker."""


def _security_config(settings: KafkaSettings) -> dict[str, Any]:
    if settings.sasl_mechanism == "none":
        return {"security.protocol": settings.security_protocol}

    # --- STUB: AWS MSK IAM authentication (not used locally) ---------------
    def _oauth_cb(_config: str) -> tuple[str, float]:
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider  # type: ignore[import-untyped]

        token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(settings.aws_region)
        return token, expiry_ms / 1000.0

    return {
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "OAUTHBEARER",
        "oauth_cb": _oauth_cb,
    }


def _producer_config(settings: KafkaSettings, *, client_id: str) -> dict[str, Any]:
    """Producer configuration: idempotent, fully acknowledged, ordered."""
    return {
        "bootstrap.servers": settings.bootstrap_servers,
        "client.id": client_id,
        "enable.idempotence": True,
        "acks": "all",
        "max.in.flight.requests.per.connection": 5,
        "retries": 10,
        "linger.ms": 5,
        "compression.type": "lz4",
        "request.timeout.ms": settings.request_timeout_ms,
        "delivery.timeout.ms": settings.delivery_timeout_ms,
        **_security_config(settings),
    }


def _consumer_config(
    settings: KafkaSettings, *, client_id: str, group_id: str | None = None
) -> dict[str, Any]:
    """Consumer configuration: manual offset commits, long poll interval.

    ``enable.auto.commit=False`` is the core of the at-least-once contract: the
    offset is committed only after the job has reached a terminal database state
    or has been handed to the DLQ.
    """
    return {
        "bootstrap.servers": settings.bootstrap_servers,
        "client.id": client_id,
        "group.id": group_id or settings.consumer_group,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "isolation.level": "read_committed",
        # Must exceed the longest possible job, or the broker evicts the worker
        # mid-job and another consumer picks the message up while it is running.
        "max.poll.interval.ms": settings.max_poll_interval_ms,
        "session.timeout.ms": settings.session_timeout_ms,
        "partition.assignment.strategy": "cooperative-sticky",
        **_security_config(settings),
    }


def make_producer(settings: KafkaSettings, *, client_id: str) -> Producer:
    return Producer(_producer_config(settings, client_id=client_id))


def make_consumer(
    settings: KafkaSettings,
    *,
    client_id: str,
    group_id: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Consumer:
    config = _consumer_config(settings, client_id=client_id, group_id=group_id)
    return Consumer({**config, **(overrides or {})})


class AsyncProducer:
    """asyncio wrapper around the blocking ``confluent_kafka.Producer``.

    A single background thread owns ``poll()``; delivery reports resolve the
    ``asyncio.Future`` returned by :meth:`produce` on the event loop thread.
    """

    def __init__(self, settings: KafkaSettings, *, client_id: str) -> None:
        self._producer = make_producer(settings, client_id=client_id)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._poll_forever, name="kafka-producer-poll", daemon=True
        )
        self._thread.start()

    def _poll_forever(self) -> None:
        while not self._stop.is_set():
            # Short timeout keeps shutdown responsive; poll() drives the
            # delivery callbacks registered below.
            self._producer.poll(0.2)

    async def produce(
        self,
        topic: str,
        *,
        key: str | bytes | None,
        value: bytes,
        headers: KafkaHeaders | None = None,
        timeout: float = 10.0,  # noqa: ASYNC109 - the delivery deadline is the API here
    ) -> None:
        """Produce one message and await its broker acknowledgement."""
        loop = self._loop
        if loop is None:  # pragma: no cover - programming error
            raise RuntimeError("AsyncProducer.start() must be called inside the event loop")
        future: asyncio.Future[None] = loop.create_future()

        def _resolve(err: KafkaError | None) -> None:
            # Runs on the loop thread, so the done() check cannot race with
            # wait_for() cancelling the future on timeout.
            if future.done():
                return
            if err is not None:
                future.set_exception(DeliveryError(str(err)))
            else:
                future.set_result(None)

        def _on_delivery(err: KafkaError | None, _msg: Message) -> None:
            loop.call_soon_threadsafe(_resolve, err)

        self._producer.produce(
            topic,
            value=value,
            key=key.encode() if isinstance(key, str) else key,
            headers=list(headers or ()),
            on_delivery=_on_delivery,
        )
        try:
            await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise DeliveryError(f"delivery report for {topic} timed out after {timeout}s") from exc

    async def flush(self, timeout: float = 10.0) -> int:  # noqa: ASYNC109 - librdkafka flush deadline
        """Flush outstanding messages; returns the number still queued."""
        return await asyncio.to_thread(self._producer.flush, timeout)

    def healthy(self) -> bool:
        """Cheap readiness probe: can we reach the cluster metadata?"""
        try:
            self._producer.list_topics(timeout=2.0)
        except Exception:
            return False
        return True

    async def close(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109 - shutdown budget
        remaining = await self.flush(timeout)
        if remaining:
            log.warning("kafka.producer.close.unflushed", messages=remaining)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
