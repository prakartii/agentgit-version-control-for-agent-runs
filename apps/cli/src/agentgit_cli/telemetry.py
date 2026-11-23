"""Turn on OpenTelemetry export for the CLI.

Off by default. Two ways to enable it:

* ``OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`` sends traces and metrics
  to an OTLP/HTTP collector (Jaeger, Grafana Tempo, the OpenTelemetry Collector).
* ``AGENTGIT_TELEMETRY=console`` prints spans to stderr. stdout is never used:
  ``record`` and ``replay`` carry protocol messages there.
"""

import os
import sys

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)

_configured = False


def setup(service_name: str = "agentgit") -> bool:
    """Configure exporters from the environment. Returns True if telemetry is on."""
    global _configured
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    console = os.environ.get("AGENTGIT_TELEMETRY") == "console"
    if _configured or not (endpoint or console):
        return _configured

    resource = Resource.create({"service.name": service_name})
    span_exporter: SpanExporter
    metric_exporter: MetricExporter
    if endpoint:
        # Imported here so the OTLP packages are only needed when used.
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter, metric_exporter = OTLPSpanExporter(), OTLPMetricExporter()
        processor: SimpleSpanProcessor | BatchSpanProcessor = BatchSpanProcessor(span_exporter)
    else:
        span_exporter = ConsoleSpanExporter(out=sys.stderr)
        metric_exporter = ConsoleMetricExporter(out=sys.stderr)
        processor = SimpleSpanProcessor(span_exporter)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(processor)
    trace.set_tracer_provider(tracer_provider)
    reader = PeriodicExportingMetricReader(metric_exporter)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    _configured = True
    return True


def shutdown() -> None:
    """Flush buffered spans and metrics; call before the process exits."""
    if not _configured:
        return
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
    meter_provider = metrics.get_meter_provider()
    if isinstance(meter_provider, MeterProvider):
        meter_provider.shutdown()
