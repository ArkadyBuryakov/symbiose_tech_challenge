"""Worker entry point.

Starts, in order: the health server, the outbox relay, the reconciler, and then
the consume loop on the main thread. SIGTERM stops the loop, which finishes or
abandons the job in flight and then drains the background threads.
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from pmp_common.db import make_sync_engine
from pmp_common.kafka import make_producer
from pmp_common.logging import configure_logging, get_logger
from pmp_common.tracing import configure_tracing, instrument_psycopg

from .health import HealthServer
from .outbox import OutboxRelay
from .reconciler import Reconciler
from .runner import Runner
from .settings import WorkerSettings, get_settings
from .storage import Storage

log = get_logger(__name__)


def main() -> int:
    settings: WorkerSettings = get_settings()

    configure_logging(
        service=settings.service_name,
        level=settings.observability.log_level,
        fmt=settings.observability.log_format,
        git_sha=settings.git_sha,
    )
    configure_tracing(
        service=settings.service_name,
        version=settings.git_sha,
        endpoint=settings.observability.otlp_endpoint,
        environment=settings.environment,
    )
    instrument_psycopg()

    if settings.chaos.any_enabled:
        log.error(
            "worker.chaos_enabled",
            detail="CHAOS_* switches are on; this process will fail deliberately",
            crash_after_copy=settings.chaos.crash_after_copy,
            crash_before_offset_commit=settings.chaos.crash_before_offset_commit,
            delay_ms=settings.chaos.delay_ms,
        )

    # A small pool: the loop uses one connection at a time, the relay and the
    # reconciler one each.
    engine = make_sync_engine(settings.db, application_name="pmp-worker", pool_size=4)
    storage = Storage(settings.s3)
    producer = make_producer(settings.kafka, client_id=f"worker-{settings.worker_id}")

    def readiness() -> tuple[bool, str]:
        """Ready means the worker could actually run a job right now."""
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return False, f"database unavailable: {exc}"
        if not storage.healthy():
            return False, "object storage unavailable"
        return True, "ok"

    health = HealthServer(
        host=settings.http_host,
        port=settings.http_port,
        version=settings.git_sha,
        readiness=readiness,
    )
    relay = OutboxRelay(
        engine,
        producer,
        batch_size=settings.outbox_batch,
        interval_seconds=settings.outbox_interval_seconds,
    )
    reconciler = Reconciler(
        engine,
        producer,
        interval_seconds=settings.reconciler_interval_seconds,
        stuck_pending_seconds=settings.stuck_pending_seconds,
        batch_size=settings.reconciler_batch,
    )
    runner = Runner(settings, engine, storage, producer)
    runner.install_signal_handlers()

    health.start()
    relay.start()
    reconciler.start()
    log.info("worker.started", worker_id=settings.worker_id, version=settings.git_sha)

    try:
        runner.run()
    except Exception:
        log.exception("worker.crashed")
        return 1
    finally:
        # Drain the relay one last time so results committed just before
        # shutdown still reach Kafka.
        reconciler.stop()
        try:
            relay.drain_once()
        except Exception as exc:  # pragma: no cover
            log.warning("worker.final_drain_failed", error=str(exc))
        relay.stop()
        health.stop()
        engine.dispose()
        log.info("worker.stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
