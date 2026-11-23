# ADR-011: OpenTelemetry hooks that are off by default, and honest benchmarks

Status: accepted

## Decision

- **Libraries depend on the OpenTelemetry API only.** Without an SDK the calls
  are no-ops, so instrumenting the proxy's hot path changes nothing for users who
  do not opt in. The SDK and OTLP exporter live in the CLI (`agentgit_cli.telemetry`).
- **Opt-in by environment.** `OTEL_EXPORTER_OTLP_ENDPOINT` sends to a collector;
  `AGENTGIT_TELEMETRY=console` prints to **stderr**, because stdout carries
  protocol messages in `record` and `replay`.
- **What is emitted:** one span and one duration histogram per recorded exchange
  (created after the fact with the exchange's own timestamps), a counter of replay
  divergences by kind, a span per replayed request, and spans around the agent
  loop's model and tool calls.
- **No payloads in telemetry.** Attributes carry method, tool name and outcome,
  never arguments or results, which may contain secrets. A test enforces this.
- **Benchmarks are a script, not CI.** `benchmarks/bench.py` prints measured
  numbers; one run is committed with its machine. CI does not gate on timings,
  because shared runners are too noisy for that.

## Consequences

- Traces from the proxy show real call times, but the span is emitted when the
  exchange completes, so it has no child spans and cannot be a parent of the
  server's own spans (no trace-context propagation yet).
- The figures describe one Windows machine and one run each. They are a baseline
  for comparison on the same machine, not a performance claim.
