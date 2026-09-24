"""The publication worker's consume loop.

Delivery contract: **at-least-once, with idempotent effects.**

``enable.auto.commit`` is off. An offset is committed only once the job has
reached a terminal state in Postgres or has been handed to the dead-letter
topic. Everything a job does is therefore safe to repeat:

* the claim is a conditional UPDATE — a second delivery finds nothing to claim;
* the publish key is the content hash — a re-copy writes identical bytes to the
  identical key;
* the catalogue change and the result event are one transaction.

The loop is deliberately synchronous and single-job: one message in flight per
process, scale by adding replicas. That keeps the failure model small enough to
reason about, which matters much more here than throughput.
"""

from __future__ import annotations

import os
import random
import signal
import threading
import time
from collections.abc import Mapping
from types import FrameType

from confluent_kafka import Consumer, KafkaError, KafkaException, Message, Producer
from confluent_kafka._types import HeadersType
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from pmp_common.enums import ErrorCode
from pmp_common.events import PublicationRequested, decode_event, encode_event
from pmp_common.kafka import make_consumer, make_producer
from pmp_common.logging import get_logger, log_context
from pmp_common.metrics import (
    DLQ_MESSAGES,
    PUBLICATION_JOB_DURATION,
    PUBLICATION_JOBS,
    WORKER_INFLIGHT_JOBS,
)
from pmp_common.s3 import publish_key
from pmp_common.tracing import header_value, span_from_kafka_headers

from . import catalog
from .lease import LeaseHeartbeat
from .settings import WorkerSettings
from .storage import PermanentJobError, Storage, TransientJobError

__all__ = ["Runner"]

log = get_logger(__name__)


def _normalise_headers(headers: HeadersType | None) -> list[tuple[str, bytes | None]]:
    """Normalise Kafka headers to ``(str, bytes | None)`` pairs.

    confluent-kafka can hand back either a list of pairs or a mapping, and
    values may arrive as ``str``; the rest of the platform treats them as bytes.
    """
    if headers is None:
        return []
    items = headers.items() if isinstance(headers, Mapping) else headers
    return [(key, value.encode() if isinstance(value, str) else value) for key, value in items]


class Runner:
    def __init__(
        self,
        settings: WorkerSettings,
        engine: Engine,
        storage: Storage,
        *,
        producer: Producer | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._storage = storage
        self._producer = producer or make_producer(
            settings.kafka, client_id=f"worker-{settings.worker_id}"
        )
        self._consumer: Consumer | None = None
        self._stop = threading.Event()
        self._outcome = "skipped"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        """Stop polling on SIGTERM, and bound how long the in-flight job may take.

        The job in flight is allowed to finish, because finishing is cheaper
        than redoing it. But not indefinitely: after
        ``WORKER_SHUTDOWN_GRACE_SECONDS`` the process exits hard. Abandoning a
        job there is safe — its offset is not committed and its lease simply
        stops being renewed, so another worker picks it up after the lease
        expires, and the content-addressed copy makes the redo idempotent.
        """
        grace = self._settings.shutdown_grace_seconds

        def abandon() -> None:
            log.error(
                "worker.shutdown_grace_exceeded",
                grace_seconds=grace,
                detail="abandoning the in-flight job; its lease will expire and it will be retried",
            )
            os._exit(1)

        def handle(signum: int, _frame: FrameType | None) -> None:
            if self._stop.is_set():
                return
            log.info("worker.signal", signal=signal.Signals(signum).name, grace_seconds=grace)
            self._stop.set()
            deadline = threading.Timer(grace, abandon)
            deadline.daemon = True
            deadline.start()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        consumer = make_consumer(
            self._settings.kafka,
            client_id=f"worker-{self._settings.worker_id}",
            group_id=self._settings.kafka.consumer_group,
        )
        self._consumer = consumer
        topic = self._settings.kafka.topic_publication_requested
        consumer.subscribe([topic])
        log.info("worker.consuming", topic=topic, group=self._settings.kafka.consumer_group)

        try:
            while not self._stop.is_set():
                message = consumer.poll(self._settings.poll_timeout_seconds)
                if message is None:
                    continue
                error = message.error()
                if error is not None:
                    self._handle_consumer_error(error)
                    continue
                self._handle_message(consumer, message)
        finally:
            # Leave the group cleanly so the partitions are reassigned at once
            # instead of after the session timeout.
            log.info("worker.closing_consumer")
            consumer.close()
            self._producer.flush(10.0)

    @staticmethod
    def _handle_consumer_error(error: KafkaError) -> None:
        if error.code() == KafkaError._PARTITION_EOF:
            return
        # Kafka errors surfaced on poll() are informational; librdkafka
        # reconnects on its own. Fatal errors end the process so the
        # orchestrator restarts it.
        log.warning("worker.consumer_error", error=str(error), fatal=error.fatal())
        if error.fatal():
            raise KafkaException(error)

    # ------------------------------------------------------------------
    # One message
    # ------------------------------------------------------------------
    def _handle_message(self, consumer: Consumer, message: Message) -> None:
        raw = message.value()
        headers = _normalise_headers(message.headers())
        request_id = header_value(headers, "x-request-id")

        try:
            if raw is None:
                raise ValueError("message has no payload")
            event = decode_event(PublicationRequested, raw)
        except (ValueError, TypeError) as exc:
            # Unparseable input can never become parseable: dead-letter it
            # immediately rather than blocking the partition forever.
            log.error("worker.undecodable_message", error=str(exc), offset=message.offset())
            self._dead_letter(message, ErrorCode.INTERNAL_ERROR, str(exc))
            consumer.commit(message=message, asynchronous=False)
            return

        with (
            span_from_kafka_headers(
                "worker.publication",
                headers,
                **{
                    "pmp.job_id": str(event.job_id),
                    "pmp.dataset_id": str(event.dataset_id),
                    "pmp.tenant_id": event.tenant_id,
                },
            ),
            log_context(
                job_id=str(event.job_id),
                dataset_id=str(event.dataset_id),
                tenant_id=event.tenant_id,
                request_id=request_id,
            ),
        ):
            self._process(event, message)

        # Offset committed only after the job reached a terminal state or was
        # dead-lettered. A crash before this line replays the message.
        if self._settings.chaos.crash_before_offset_commit:
            log.error("chaos.crash_before_offset_commit", offset=message.offset())
            os._exit(93)
        consumer.commit(message=message, asynchronous=False)

    def _process(self, event: PublicationRequested, message: Message) -> None:
        started = time.perf_counter()
        self._outcome = "skipped"
        WORKER_INFLIGHT_JOBS.inc()
        try:
            # 1. Claim -------------------------------------------------------
            with self._engine.begin() as conn:
                job = catalog.claim_job(
                    conn,
                    job_id=event.job_id,
                    owner=self._settings.worker_id,
                    lease_seconds=self._settings.lease_seconds,
                )
            if job is None:
                # Terminal already, or leased by another worker. Either way this
                # delivery has nothing to do — which is the duplicate-message case.
                log.info("worker.job_not_claimable", reason="terminal or leased elsewhere")
                return

            if self._settings.chaos.delay_ms:
                log.warning("chaos.delay", ms=self._settings.chaos.delay_ms)
                time.sleep(self._settings.chaos.delay_ms / 1000)

            self._run_job(job, event)
        finally:
            WORKER_INFLIGHT_JOBS.dec()
            PUBLICATION_JOB_DURATION.labels(self._outcome).observe(time.perf_counter() - started)

    def _run_job(self, job: catalog.ClaimedJob, event: PublicationRequested) -> None:
        # The heartbeat keeps the lease short (fast recovery from a crash)
        # without risking a long-running job being taken away mid-copy.
        with LeaseHeartbeat(
            self._engine,
            job_id=job.id,
            owner=self._settings.worker_id,
            lease_seconds=self._settings.lease_seconds,
        ):
            self._execute(job, event)

    def _execute(self, job: catalog.ClaimedJob, event: PublicationRequested) -> None:
        try:
            # 2. Validate ---------------------------------------------------
            source = self._storage.inspect_source(job.source_key)

            # 3. Hash -------------------------------------------------------
            sha256 = self._storage.sha256_of(
                job.source_key, chunk_bytes=self._settings.hash_chunk_bytes
            )

            # 4. Copy (server-side, idempotent on the content-addressed key) --
            target_key = publish_key(
                visibility=event.visibility,
                tenant_id=job.tenant_id,
                dataset_id=job.dataset_id,
                sha256=sha256,
            )
            self._storage.copy_to_publish(
                source_key=job.source_key, target_key=target_key, size_bytes=source.size_bytes
            )

            if self._settings.chaos.crash_after_copy:
                log.error("chaos.crash_after_copy", target_key=target_key)
                os._exit(92)

            # 5. Commit: version + pointer + job + outbox, atomically ---------
            with self._engine.begin() as conn:
                outcome = catalog.apply_publication(
                    conn,
                    job=job,
                    sha256=sha256,
                    size_bytes=source.size_bytes,
                    object_key=target_key,
                    source_etag=source.etag,
                    pmtiles_header=source.header.model_dump(mode="json"),
                    worker_build=self._settings.git_sha,
                )

        except PermanentJobError as exc:
            self._fail(job, exc.code, str(exc), dead_letter=False, message=None)
            return
        except (TransientJobError, SQLAlchemyError) as exc:
            self._handle_transient(job, event, exc)
            return
        except Exception as exc:
            log.exception("worker.unexpected_error", error=type(exc).__name__)
            self._handle_transient(job, event, exc)
            return

        self._outcome = outcome.result.value
        PUBLICATION_JOBS.labels("SUCCEEDED", outcome.result.value, "").inc()
        log.info(
            "worker.job_succeeded",
            result=outcome.result.value,
            version_seq=outcome.version_seq,
            sha256=outcome.sha256,
            size_bytes=outcome.size_bytes,
            object_key=outcome.object_key,
            attempts=job.attempts,
        )

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------
    def _handle_transient(
        self, job: catalog.ClaimedJob, event: PublicationRequested, exc: Exception
    ) -> None:
        """Retry with backoff; dead-letter once the attempt budget is spent.

        Retries happen *in process*, inside the current message's lease, rather
        than by re-queuing: that keeps the ordering guarantee of the partition
        and avoids a retry storm. The lease and the reconciler cover the case
        where this process dies mid-retry.
        """
        code = getattr(exc, "code", ErrorCode.INTERNAL_ERROR)
        if job.attempts >= self._settings.max_attempts:
            log.error(
                "worker.job_dead_lettered",
                attempts=job.attempts,
                max_attempts=self._settings.max_attempts,
                error=str(exc),
            )
            self._fail(
                job,
                ErrorCode.MAX_ATTEMPTS_EXCEEDED,
                f"giving up after {job.attempts} attempts: {exc}",
                dead_letter=True,
                message=event,
                dlq_code=code,
            )
            return

        delay = self._backoff(job.attempts)
        log.warning(
            "worker.job_retrying",
            attempts=job.attempts,
            delay_seconds=round(delay, 2),
            error=str(exc),
        )
        # Release the lease so the reconciler (or a redelivery) can pick the job
        # up promptly if this process dies during the wait.
        with self._engine.begin() as conn:
            catalog.release_lease(conn, job_id=job.id)

        if self._stop.wait(delay):
            return
        # Re-run the whole job: claim again, then repeat the pipeline.
        with self._engine.begin() as conn:
            reclaimed = catalog.claim_job(
                conn,
                job_id=job.id,
                owner=self._settings.worker_id,
                lease_seconds=self._settings.lease_seconds,
            )
        if reclaimed is None:
            log.info("worker.retry_claimed_elsewhere")
            return
        self._run_job(reclaimed, event)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        Jitter matters when a dependency comes back after an outage: without it
        every replica retries in lockstep and knocks it over again.
        """
        ceiling = min(
            self._settings.backoff_base_seconds * (2 ** (attempt - 1)),
            self._settings.backoff_max_seconds,
        )
        return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography

    def _fail(
        self,
        job: catalog.ClaimedJob,
        code: ErrorCode,
        detail: str,
        *,
        dead_letter: bool,
        message: PublicationRequested | None,
        dlq_code: ErrorCode | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            catalog.fail_job(
                conn,
                job=job,
                error_code=code,
                error_message=detail,
                dead_lettered=dead_letter,
            )
        self._outcome = "FAILED"
        PUBLICATION_JOBS.labels("FAILED", "", code.value).inc()
        log.error("worker.job_failed", error_code=code.value, detail=detail, dlq=dead_letter)

        if dead_letter and message is not None:
            self._dead_letter_event(message, dlq_code or code, detail)

    def _dead_letter_event(self, event: PublicationRequested, code: ErrorCode, detail: str) -> None:
        topic = self._settings.kafka.topic_publication_dlq
        try:
            self._producer.produce(
                topic,
                key=str(event.dataset_id).encode(),
                value=encode_event(event),
                headers=[
                    ("x-error-code", code.value.encode()),
                    ("x-error-detail", detail[:500].encode()),
                    ("x-original-topic", self._settings.kafka.topic_publication_requested.encode()),
                ],
            )
            self._producer.flush(10.0)
        except (BufferError, KafkaException) as exc:  # pragma: no cover
            log.error("worker.dlq_produce_failed", error=str(exc))
            return
        DLQ_MESSAGES.labels(topic, code.value).inc()

    def _dead_letter(self, message: Message, code: ErrorCode, detail: str) -> None:
        """Dead-letter a raw, undecodable message verbatim."""
        topic = self._settings.kafka.topic_publication_dlq
        try:
            self._producer.produce(
                topic,
                key=message.key(),
                value=message.value(),
                headers=[
                    ("x-error-code", code.value.encode()),
                    ("x-error-detail", detail[:500].encode()),
                ],
            )
            self._producer.flush(10.0)
        except (BufferError, KafkaException) as exc:  # pragma: no cover
            log.error("worker.dlq_produce_failed", error=str(exc))
            return
        DLQ_MESSAGES.labels(topic, code.value).inc()
