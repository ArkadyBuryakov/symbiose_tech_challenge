"""Prometheus metrics shared across services.

Metric names follow the Prometheus conventions (``_total`` for counters,
``_seconds`` for durations). Every service exposes ``/metrics``; the gateway
deliberately does *not* route ``/metrics`` from the public edge.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "DLQ_MESSAGES",
    "HTTP_REQUESTS",
    "HTTP_REQUEST_DURATION",
    "OUTBOX_PENDING",
    "OUTBOX_RELAYED",
    "PUBLICATION_JOBS",
    "PUBLICATION_JOB_DURATION",
    "PUBLICATION_REQUESTS",
    "RECONCILER_REQUEUED",
    "WORKER_INFLIGHT_JOBS",
    "render_metrics",
]

# --- HTTP (all FastAPI services) -------------------------------------------
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled by this service.",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# --- publication domain ----------------------------------------------------
PUBLICATION_REQUESTS = Counter(
    "publication_requests_total",
    "Publication requests accepted by the backend API.",
    ["result"],  # accepted | idempotent_replay | rejected
)
PUBLICATION_JOBS = Counter(
    "publication_jobs_total",
    "Publication jobs that reached a terminal state.",
    ["status", "result", "error_code"],
)
PUBLICATION_JOB_DURATION = Histogram(
    "publication_job_duration_seconds",
    "Wall-clock duration of a publication job inside the worker, by outcome.",
    ["outcome"],  # CREATED | DEDUPLICATED | POINTER_MOVED | FAILED | skipped
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)
WORKER_INFLIGHT_JOBS = Gauge(
    "worker_inflight_jobs",
    "Publication jobs currently being processed by this worker process.",
)
OUTBOX_PENDING = Gauge(
    "outbox_pending",
    "Rows in the transactional outbox that have not been produced yet.",
)
OUTBOX_RELAYED = Counter(
    "outbox_relayed_total",
    "Outbox rows successfully produced to Kafka.",
    ["topic"],
)
DLQ_MESSAGES = Counter(
    "dlq_messages_total",
    "Messages routed to the dead-letter topic.",
    ["topic", "error_code"],
)
RECONCILER_REQUEUED = Counter(
    "reconciler_requeued_total",
    "Publication requests re-emitted by the reconciler.",
    ["reason"],  # stuck_pending | expired_lease
)


def render_metrics() -> bytes:
    """Return the Prometheus exposition payload."""
    return generate_latest()
