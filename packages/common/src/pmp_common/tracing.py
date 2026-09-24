"""OpenTelemetry tracing setup and Kafka context propagation.

Tracing is opt-in: if ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset (the default),
no exporter is installed and the SDK stays effectively inert. That keeps the
default local stack light while ``make up PROFILE=observability`` lights up a
full trace: edge -> gateway -> backend -> Kafka -> worker -> DB/S3.

Kafka has no automatic instrumentation here, so the W3C ``traceparent`` is
injected into and extracted from message headers explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind

from .logging import get_logger

__all__ = [
    "SpanKind",
    "carrier_from_kafka_headers",
    "configure_tracing",
    "current_trace_id",
    "get_tracer",
    "instrument_fastapi",
    "kafka_headers_from_carrier",
    "span_from_kafka_headers",
    "traced",
]

_KAFKA_CONTEXT_KEYS = ("traceparent", "tracestate")
_log = get_logger(__name__)


def configure_tracing(
    *,
    service: str,
    version: str = "unknown",
    endpoint: str | None = None,
    sample_ratio: float = 1.0,
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
    sampler = ALWAYS_ON if sample_ratio >= 1.0 else ParentBased(TraceIdRatioBased(sample_ratio))
    provider = TracerProvider(resource=resource, sampler=sampler)

    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )

    trace.set_tracer_provider(provider)

    # httpx is used by the gateway proxy and the backend; instrument it eagerly
    # so outgoing calls continue the trace.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover - instrumentation is best-effort
        _log.warning("tracing.instrument_httpx_failed", error=str(exc))


def instrument_fastapi(app: Any, *, excluded_urls: str = "healthz,readyz,metrics") -> None:
    """Attach FastAPI server instrumentation, skipping probe endpoints."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded_urls)
    except Exception as exc:  # pragma: no cover
        _log.warning("tracing.instrument_fastapi_failed", error=str(exc))


def instrument_psycopg() -> None:
    """Attach psycopg instrumentation (used by backend and worker)."""
    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        PsycopgInstrumentor().instrument(skip_dep_check=True)
    except Exception as exc:  # pragma: no cover
        _log.warning("tracing.instrument_psycopg_failed", error=str(exc))


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


@contextmanager
def traced(name: str, /, *, kind: SpanKind = SpanKind.INTERNAL, **attrs: Any) -> Iterator[Any]:
    """Convenience span context manager that records exceptions."""
    tracer = trace.get_tracer("pmp")
    with tracer.start_as_current_span(name, kind=kind) as span:
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


def carrier_from_kafka_headers(
    headers: Sequence[tuple[str, bytes | None]] | None,
) -> dict[str, str]:
    """Extract the W3C trace headers from Kafka headers into a text-map carrier."""
    carrier: dict[str, str] = {}
    for key, value in headers or ():
        if key.lower() in _KAFKA_CONTEXT_KEYS and value is not None:
            carrier[key.lower()] = value.decode()
    return carrier


@contextmanager
def span_from_kafka_headers(
    name: str,
    headers: Sequence[tuple[str, bytes | None]] | None,
    **attrs: Any,
) -> Iterator[Any]:
    """Continue the producer's trace when consuming a Kafka message."""
    parent = extract(carrier_from_kafka_headers(headers))
    token = otel_context.attach(parent)
    tracer = trace.get_tracer("pmp")
    try:
        with tracer.start_as_current_span(name, kind=SpanKind.CONSUMER) as span:
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, value)
            yield span
    finally:
        otel_context.detach(token)


def header_value(
    headers: Sequence[tuple[str, bytes | None]] | Mapping[str, Any] | None,
    key: str,
) -> str | None:
    """Read a single Kafka header value as text."""
    if headers is None:
        return None
    items = headers.items() if isinstance(headers, Mapping) else headers
    for name, value in items:
        if name.lower() == key.lower() and value is not None:
            return value.decode() if isinstance(value, bytes | bytearray) else str(value)
    return None


def merge_headers(
    base: MutableMapping[str, bytes] | None = None, **extra: str | None
) -> list[tuple[str, bytes]]:
    """Build a Kafka header list from the current trace context plus extras."""
    out: dict[str, bytes] = dict(base or {})
    out.update(dict(kafka_headers_from_carrier()))
    out.update({k: v.encode() for k, v in extra.items() if v is not None})
    return list(out.items())


__all__ += ["header_value", "instrument_psycopg", "merge_headers"]
