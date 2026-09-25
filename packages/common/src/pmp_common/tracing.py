"""OpenTelemetry tracing setup and Kafka context propagation.

Tracing is opt-in: if ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset (the default),
no exporter is installed and the SDK stays effectively inert. That keeps the
default local stack light while ``make up PROFILE=observability`` lights up a
full trace: edge -> gateway -> backend -> Kafka -> worker -> DB/S3.

Sampling is left to the SDK's own environment variables
(``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG``; default: always on).

Kafka has no automatic instrumentation here, so the W3C ``traceparent`` is
injected into and extracted from message headers explicitly.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind

from .logging import get_logger

__all__ = [
    "SpanKind",
    "configure_tracing",
    "current_trace_id",
    "header_value",
    "instrument_fastapi",
    "instrument_psycopg",
    "kafka_headers_from_carrier",
    "merge_headers",
    "span_from_kafka_headers",
    "traced",
]

KafkaHeaders = Sequence[tuple[str, bytes | None]] | None

_KAFKA_CONTEXT_KEYS = ("traceparent", "tracestate")
_log = get_logger(__name__)


def configure_tracing(
    *,
    service: str,
    version: str = "unknown",
    endpoint: str | None = None,
    environment: str = "local",
) -> None:
    """Install a tracer provider. A no-op exporter-wise when ``endpoint`` is None."""
    resource = Resource.create(
        {
            "service.name": service,
            "service.version": version,
            "deployment.environment.name": environment,
        }
    )
    provider = TracerProvider(resource=resource)

    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )

    trace.set_tracer_provider(provider)

    # httpx is used by the gateway proxy; instrument it eagerly so outgoing
    # calls continue the trace. Services that do not depend on httpx (the
    # worker) simply skip it.
    if importlib.util.find_spec("httpx") is not None:
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except Exception as exc:  # pragma: no cover - instrumentation is best-effort
            _log.warning("tracing.instrument_httpx_failed", error=str(exc))


def instrument_fastapi(app: Any) -> None:
    """Attach FastAPI server instrumentation, skipping probe endpoints."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    except Exception as exc:  # pragma: no cover
        _log.warning("tracing.instrument_fastapi_failed", error=str(exc))


def instrument_psycopg() -> None:
    """Attach psycopg instrumentation (used by backend and worker)."""
    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        PsycopgInstrumentor().instrument(skip_dep_check=True)
    except Exception as exc:  # pragma: no cover
        _log.warning("tracing.instrument_psycopg_failed", error=str(exc))


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


@contextmanager
def traced(
    name: str,
    /,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    context: Any = None,
    **attrs: Any,
) -> Iterator[Any]:
    """Span context manager; ``None`` attribute values are skipped."""
    tracer = trace.get_tracer("pmp")
    with tracer.start_as_current_span(name, context=context, kind=kind) as span:
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, value)
        yield span


# --------------------------------------------------------------------------
# Kafka propagation
# --------------------------------------------------------------------------
def kafka_headers_from_carrier() -> list[tuple[str, bytes]]:
    """Serialise the current trace context as Kafka message headers."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return [(k, v.encode()) for k, v in carrier.items()]


def merge_headers(extra: Mapping[str, str | None] | None = None) -> list[tuple[str, bytes]]:
    """Kafka headers carrying the current trace context plus correlation extras.

    Header names contain hyphens (``x-request-id``), so extras are passed as a
    mapping rather than keyword arguments.
    """
    out = dict(kafka_headers_from_carrier())
    out.update({k: v.encode() for k, v in (extra or {}).items() if v is not None})
    return list(out.items())


def header_value(headers: KafkaHeaders, key: str) -> str | None:
    """Read a single Kafka header value as text."""
    for name, value in headers or ():
        if name.lower() == key.lower() and value is not None:
            return value.decode()
    return None


def span_from_kafka_headers(name: str, headers: KafkaHeaders, **attrs: Any) -> Any:
    """Continue the producer's trace when consuming a Kafka message."""
    carrier = {k: v for k in _KAFKA_CONTEXT_KEYS if (v := header_value(headers, k)) is not None}
    return traced(name, kind=SpanKind.CONSUMER, context=extract(carrier), **attrs)
