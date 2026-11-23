import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentgit_core import telemetry


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Route spans to memory. The global provider can only be set once per process."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "tracer", lambda: provider.get_tracer("test"))
    return exporter


def test_an_exchange_becomes_a_span_with_its_own_timestamps(spans: InMemorySpanExporter) -> None:
    telemetry.record_exchange("tools/call", "read_file", "ok", started_at_ms=1_000, duration_ms=250)
    (span,) = spans.get_finished_spans()
    assert span.name == "mcp.exchange"
    assert span.start_time == 1_000 * 1_000_000
    assert span.end_time is not None and span.end_time - span.start_time == 250 * 1_000_000
    assert span.attributes == {
        "mcp.method": "tools/call",
        "agentgit.outcome": "ok",
        "mcp.tool": "read_file",
    }


def test_span_attributes_never_include_payloads(spans: InMemorySpanExporter) -> None:
    telemetry.record_exchange("tools/call", "t", "ok", 0, 1)
    (span,) = spans.get_finished_spans()
    assert set(span.attributes or {}) <= {"mcp.method", "mcp.tool", "agentgit.outcome"}


def test_a_failing_block_is_recorded_and_reraised(spans: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError), telemetry.span("agent.tool_call", tool="x"):
        raise RuntimeError("boom")
    (span,) = spans.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert any(e.name == "exception" for e in span.events)


def test_instrumentation_is_harmless_without_an_sdk() -> None:
    # No provider configured by the test: the calls must simply do nothing.
    telemetry.record_exchange("tools/list", None, "ok", 0, 1)
    telemetry.count_divergence("unexpected_request")
    with telemetry.span("noop"):
        pass
