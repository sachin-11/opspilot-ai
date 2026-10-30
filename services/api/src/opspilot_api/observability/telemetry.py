"""Pipeline metrics (Prometheus) and tracing (OpenTelemetry) without raw payloads or secrets.

Span attributes are restricted to an allow-list of identifiers and counters; values are truncated,
so logs, payloads, signatures, and credentials can never reach a trace backend.
"""

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

ACCEPTED = Counter("opspilot_signals_accepted_total", "Signals durably accepted", ["provider"])
REJECTED = Counter("opspilot_signals_rejected_total", "Signals rejected at ingress", ["reason"])
DUPLICATES = Counter("opspilot_signals_duplicate_total", "Duplicate signals suppressed", ["stage"])
NORMALIZATION_FAILURES = Counter(
    "opspilot_normalization_failures_total", "Normalization failures", ["code"]
)
PROCESSING_FAILURES = Counter(
    "opspilot_processing_failures_total", "Processing failures", ["stage", "retryable"]
)
DEAD_LETTERS = Counter("opspilot_dead_letters_total", "Messages dead-lettered", ["code"])
PROCESSED = Counter("opspilot_signals_processed_total", "Signals fully processed")
INCIDENTS_CREATED = Counter("opspilot_incidents_created_total", "Incidents created from signals")
EVENTS_CORRELATED = Counter(
    "opspilot_events_correlated_total", "Signals correlated into existing incidents"
)
SIGNATURE_FAILURES = Counter(
    "opspilot_webhook_signature_failures_total", "Webhook signature failures", ["reason"]
)
BACKPRESSURE_ACTIVATIONS = Counter(
    "opspilot_backpressure_activations_total", "Backpressure activations", ["reason"]
)
END_TO_END_LATENCY = Histogram(
    "opspilot_signal_end_to_end_seconds",
    "Seconds from webhook receipt to processed",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)
CONSUMER_LAG = Gauge(
    "opspilot_consumer_lag_messages", "Uncommitted messages per consumer group", ["group"]
)
PROVIDER_QUERY_LATENCY = Histogram(
    "opspilot_provider_query_seconds",
    "Observability provider query latency",
    ["provider", "query_type"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

SAFE_SPAN_ATTRIBUTES = {
    "signal.id",
    "workspace.id",
    "source.id",
    "service.id",
    "provider",
    "topic",
    "partition",
    "offset",
    "stage",
    "result",
    "attempt",
    "query.type",
    "result.count",
    "failure.code",
    "incident.id",
}
_provider: TracerProvider | None = None


def configure_tracing(
    service_name: str = "opspilot-api", exporter: SpanExporter | None = None
) -> None:
    """Install a tracer provider once. Console export only when explicitly requested."""
    global _provider
    if _provider is not None and exporter is None:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif os.environ.get("OTEL_TRACES_EXPORTER") == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    _provider = provider


def _tracer() -> trace.Tracer:
    if _provider is None:
        configure_tracing()
    provider = _provider if _provider is not None else TracerProvider()
    return provider.get_tracer("opspilot.observability")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    with _tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            attribute = key.replace("__", ".")
            if attribute in SAFE_SPAN_ATTRIBUTES and value is not None:
                current.set_attribute(attribute, str(value)[:100])
        try:
            yield current
        except Exception as exc:
            current.set_attribute("failure.code", type(exc).__name__)
            raise


@contextmanager
def timed(histogram: Histogram) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        histogram.observe(time.perf_counter() - start)
