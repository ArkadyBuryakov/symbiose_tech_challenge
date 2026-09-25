"""The publication worker's consume loop.

Delivery contract: **at-least-once, with idempotent effects.**

``enable.auto.commit`` is off. An offset is committed only once the job has
reached a terminal state in Postgres (ours or another worker's) or has been
handed to the dead-letter topic. Everything a job does is safe to repeat:

* the claim is a conditional UPDATE — a second delivery finds nothing to claim;
* the publish key is the content hash — a re-copy writes identical bytes to the
  identical key;
* the catalogue change and the result event are one transaction, guarded by
  the lease.

One message in flight per process; scale by adding replicas.
"""

from __future__ import annotations

import os
import random
import signal
import threading
import time
from types import FrameType
from typing import cast

from confluent_kafka import Consumer, KafkaError, KafkaException, Message, Producer
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from pmp_common.enums import ErrorCode
from pmp_common.events import PublicationRequested, decode_event, encode_event
from pmp_common.kafka import TOPIC_DLQ, TOPIC_REQUESTED, make_consumer
from pmp_common.logging import get_logger, log_context
from pmp_common.metrics import (
    DLQ_MESSAGES,
    PUBLICATION_JOB_DURATION,
    PUBLICATION_JOBS,
    WORKER_INFLIGHT_JOBS,
)
from pmp_common.s3 import publish_key, staging_prefix
from pmp_common.tracing import header_value, span_from_kafka_headers

from . import catalog
from .lease import LeaseHeartbeat
from .outbox import KafkaMessage, produce_confirmed
from .settings import WorkerSettings
from .storage import PermanentJobError, Storage, TransientJobError

__all__ = ["DeadLetterFailed", "Runner"]

log = get_logger(__name__)


class DeadLetterFailed(Exception):
    """The DLQ did not acknowledge a message; the offset must not be committed."""


class Runner:
    def __init__(
        self, settings: WorkerSettings, engine: Engine, storage: Storage, producer: Producer
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._storage = storage
        self._producer = producer
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        """Stop polling on SIGTERM, and bound how long the in-flight job may take.

        After ``WORKER_SHUTDOWN_GRACE_SECONDS`` the process exits hard. That is
        safe: the offset is not committed, the lease stops being renewed, and
        the content-addressed copy makes the redo idempotent.
        """
        grace = self._settings.shutdown_grace_seconds

        def abandon() -> None:
            log.error("worker.shutdown_grace_exceeded", grace_seconds=grace)
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

    def run(self) -> None:
        consumer = make_consumer(
            self._settings.kafka,
            client_id=f"worker-{self._settings.worker_id}",
            group_id=self._settings.kafka.consumer_group,
        )
        consumer.subscribe([TOPIC_REQUESTED])
        log.info(
            "worker.consuming", topic=TOPIC_REQUESTED, group=self._settings.kafka.consumer_group
        )

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
            # Leave the group cleanly so partitions are reassigned at once.
            log.info("worker.closing_consumer")
            consumer.close()
            self._producer.flush(10.0)

    @staticmethod
    def _handle_consumer_error(error: KafkaError) -> None:
        if error.code() == KafkaError._PARTITION_EOF:
            return
        # librdkafka reconnects on its own; fatal errors end the process so the
        # orchestrator restarts it.
        log.warning("worker.consumer_error", error=str(error), fatal=error.fatal())
        if error.fatal():
            raise KafkaException(error)

    # ------------------------------------------------------------------
    # One message
    # ------------------------------------------------------------------
    def _handle_message(self, consumer: Consumer, message: Message) -> None:
        raw = message.value()
        # A consumed message's headers are always a list of (str, bytes) pairs
        # or None; the dict form in the type stubs applies to produce() only.
        headers = cast("list[tuple[str, bytes | None]] | None", message.headers())

        try:
            if raw is None:
                raise ValueError("message has no payload")
            event = decode_event(PublicationRequested, raw)
        except (ValueError, TypeError) as exc:
            # Unparseable input can never become parseable: dead-letter it
            # rather than blocking the partition forever.
            log.error("worker.undecodable_message", error=str(exc), offset=message.offset())
            self._dead_letter(message.key(), raw, ErrorCode.INTERNAL_ERROR, str(exc))
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
                request_id=header_value(headers, "x-request-id"),
            ),
        ):
            done = self._process(event)

        if not done:
            # Shutting down mid-retry: leave the offset so the message is
            # redelivered to the next consumer.
            return
        if self._settings.chaos.crash_before_offset_commit:
            log.error("chaos.crash_before_offset_commit", offset=message.offset())
            os._exit(93)
        consumer.commit(message=message, asynchronous=False)

    def _process(self, event: PublicationRequested) -> bool:
        """Drive one job to a terminal state, retrying transient failures in process.

        Returns ``False`` only when shutdown interrupted a retry wait.
        """
        started = time.perf_counter()
        outcome = "skipped"
        WORKER_INFLIGHT_JOBS.inc()
        try:
            while True:
                with self._engine.begin() as conn:
                    job = catalog.claim_job(
                        conn,
                        job_id=event.job_id,
                        owner=self._settings.worker_id,
                        lease_seconds=self._settings.lease_seconds,
                    )
                if job is None:
                    # Terminal already, or leased by another worker: the
                    # duplicate-message case.
                    log.info("worker.job_not_claimable")
                    return True

                if self._settings.chaos.delay_ms:
                    log.warning("chaos.delay", ms=self._settings.chaos.delay_ms)
                    time.sleep(self._settings.chaos.delay_ms / 1000)

                with LeaseHeartbeat(
                    self._engine,
                    job_id=job.id,
                    owner=job.owner,
                    lease_seconds=self._settings.lease_seconds,
                ):
                    try:
                        outcome = self._execute(job)
                        return True
                    except catalog.LeaseLost:
                        log.warning("worker.lease_lost_before_commit")
                        return True
                    except PermanentJobError as exc:
                        outcome = self._fail(job, exc.code, str(exc), dead_lettered=False)
                        return True
                    except Exception as exc:  # transient: S3, DB, network, or a bug
                        error = exc
                        code = getattr(exc, "code", ErrorCode.INTERNAL_ERROR)
                        if not isinstance(exc, TransientJobError | SQLAlchemyError):
                            log.exception("worker.unexpected_error", error=type(exc).__name__)
                        if job.attempts >= self._settings.max_attempts:
                            detail = f"giving up after {job.attempts} attempts: {exc}"
                            # DLQ first: if it fails, nothing is committed and
                            # the message is redelivered.
                            self._dead_letter(
                                str(event.dataset_id).encode(), encode_event(event), code, detail
                            )
                            outcome = self._fail(
                                job,
                                ErrorCode.MAX_ATTEMPTS_EXCEEDED,
                                detail,
                                dead_lettered=True,
                            )
                            return True

                # Transient failure with attempts left: back off, then re-claim.
                delay = self._backoff(job.attempts)
                log.warning(
                    "worker.job_retrying",
                    attempts=job.attempts,
                    delay_seconds=round(delay, 2),
                    error=str(error),
                )
                with self._engine.begin() as conn:
                    catalog.release_lease(conn, job=job)
                if self._stop.wait(delay):
                    return False
        finally:
            WORKER_INFLIGHT_JOBS.dec()
            PUBLICATION_JOB_DURATION.labels(outcome).observe(time.perf_counter() - started)

    def _execute(self, job: catalog.ClaimedJob) -> str:
        """Validate, hash, copy, commit. Returns the result label."""
        if not job.source_key.startswith(staging_prefix(job.tenant_id)):
            raise PermanentJobError(
                ErrorCode.TENANT_MISMATCH,
                f"source key {job.source_key} is outside tenant {job.tenant_id}'s staging prefix",
            )

        # 2. Validate ---------------------------------------------------------
        source = self._storage.inspect_source(job.source_key)

        # 3. Hash -------------------------------------------------------------
        sha256 = self._storage.sha256_of(
            job.source_key, chunk_bytes=self._settings.hash_chunk_bytes
        )

        # 4. Copy (server-side, idempotent on the content-addressed key) ------
        target_key = publish_key(
            visibility=job.visibility,
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

        # 5. Commit: version + pointer + job + outbox, atomically -------------
        with self._engine.begin() as conn:
            decision = catalog.apply_publication(
                conn,
                job=job,
                sha256=sha256,
                size_bytes=source.size_bytes,
                object_key=target_key,
                source_etag=source.etag,
                pmtiles_header=source.header.model_dump(mode="json"),
                worker_build=self._settings.git_sha,
            )

        PUBLICATION_JOBS.labels("SUCCEEDED", decision.result.value, "").inc()
        log.info(
            "worker.job_succeeded",
            result=decision.result.value,
            version_seq=decision.seq,
            sha256=sha256,
            size_bytes=source.size_bytes,
            object_key=target_key,
            attempts=job.attempts,
        )
        return decision.result.value

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter (no lockstep retries after an outage)."""
        ceiling = min(
            self._settings.backoff_base_seconds * (2 ** (attempt - 1)),
            self._settings.backoff_max_seconds,
        )
        return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography

    def _fail(
        self, job: catalog.ClaimedJob, code: ErrorCode, detail: str, *, dead_lettered: bool
    ) -> str:
        try:
            with self._engine.begin() as conn:
                catalog.fail_job(
                    conn,
                    job=job,
                    error_code=code,
                    error_message=detail,
                    dead_lettered=dead_lettered,
                )
        except catalog.LeaseLost:
            log.warning("worker.lease_lost_before_fail", error_code=code.value)
            return "skipped"
        PUBLICATION_JOBS.labels("FAILED", "", code.value).inc()
        log.error("worker.job_failed", error_code=code.value, detail=detail, dlq=dead_lettered)
        return "FAILED"

    def _dead_letter(
        self, key: bytes | None, value: bytes | None, code: ErrorCode, detail: str
    ) -> None:
        """Hand a message to the DLQ, or raise :class:`DeadLetterFailed`."""
        message = KafkaMessage(
            topic=TOPIC_DLQ,
            key=key,
            value=value,
            headers=[
                ("x-error-code", code.value.encode()),
                ("x-error-detail", detail[:500].encode()),
                ("x-original-topic", TOPIC_REQUESTED.encode()),
            ],
        )
        if not produce_confirmed(self._producer, [message])[0]:
            raise DeadLetterFailed(f"DLQ did not acknowledge message ({code.value})")
        DLQ_MESSAGES.labels(TOPIC_DLQ, code.value).inc()
