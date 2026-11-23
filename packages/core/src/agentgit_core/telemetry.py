"""OpenTelemetry hooks that cost nothing until someone configures an exporter.

Library code only depends on the OpenTelemetry *API*. Without an SDK installed
and configured (see ``agentgit_cli.telemetry``), ``get_tracer`` and ``get_meter``
return no-op objects, so instrumenting a hot path adds almost nothing and never
changes behaviour. Names follow ``agentgit.<area>.<what>``.

Attributes carry tool names, methods and outcomes. They never carry arguments or
results, which may hold secrets (recording redacts them for the same reason).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import metrics, trace
from opentelemetry.trace import Span

SCOPE = "agentgit"


def tracer() -> trace.Tracer:
    return trace.get_tracer(SCOPE)


def _histogram() -> metrics.Histogram:
    return metrics.get_meter(SCOPE).create_histogram(
        "agentgit.exchange.duration",
        unit="ms",
        description="Time from a request to its response, as seen by the recording proxy",
    )


def _counter(name: str, description: str) -> metrics.Counter:
    return metrics.get_meter(SCOPE).create_counter(name, description=description)


def record_exchange(
    method: str, tool: str | None, outcome: str, started_at_ms: int, duration_ms: int
) -> None:
    """Report one completed request/response pair seen by the recording proxy.

    The span is created after the fact with the exchange's own timestamps, so
    the trace shows when the call really happened, not when it was recorded.
    """
    attrs = {"mcp.method": method, "agentgit.outcome": outcome}
    if tool:
        attrs["mcp.tool"] = tool
    start_ns = started_at_ms * 1_000_000
    span = tracer().start_span("mcp.exchange", start_time=start_ns, attributes=attrs)
    span.end(end_time=start_ns + duration_ms * 1_000_000)
    _histogram().record(duration_ms, attrs)


def count_divergence(kind: str) -> None:
    _counter("agentgit.replay.divergences", "Replay requests the recording could not answer").add(
        1, {"kind": kind}
    )


@contextmanager
def span(name: str, **attributes: str | int | bool) -> Iterator[Span]:
    """A span around a block; records an exception and re-raises it."""
    with tracer().start_as_current_span(name, attributes=attributes) as s:
        yield s
