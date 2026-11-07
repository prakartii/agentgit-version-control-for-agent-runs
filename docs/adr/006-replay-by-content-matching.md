# ADR-006: Replay by content-hash matching, with explicit divergence

Status: accepted

## Context

Replay must serve recorded tool results to an agent and say whether the agent
behaved the same way. MCP is stateless and clients may send requests in any
order, so replay cannot rely on connection state or on a fixed script position.

## Decision

1. **Replay is an MCP server.** `agentgit replay <run>` is a drop-in stdio
   server command that answers from a recorded run, exactly like
   `agentgit record -- <server>` is a drop-in for the real server. Agents need no
   changes; the real tool server is not started.
2. **Match by content.** A live request is normalised with the same function the
   recorder used (`normalize_body`: drop volatile `_meta`, redact, canonical
   JSON) and hashed. It is served by the first unconsumed recorded step with the
   same `(method, params blob id)`. Repeated identical calls are served in
   recorded order.
3. **Redaction is applied to live requests before matching**, with the same
   setting the run was recorded with (stored as `redaction` in run metadata).
   Otherwise a request carrying a real secret could never match its redacted
   recording.
4. **Divergence is reported, never hidden.** Three kinds:
   - `unexpected_request`: no unconsumed recorded step matches. The agent gets a
     JSON-RPC error (`-32603`), not a fabricated result.
   - `out_of_order`: a match was found, but earlier recorded steps are still
     unconsumed. The result is still served. `--allow-reorder` accepts this
     (agents that issue parallel calls).
   - `missing_request`: a recorded step was never requested.
   The run is faithful only if there are none. A report (JSON) and an exit code
   say so.
5. **Recorded outcomes are replayed as recorded.** `protocol_error` becomes a
   JSON-RPC error, `tool_error` is an ordinary result with `isError`, and a
   `no_response` step is answered with an error saying so. The recorded server
   info is re-attached to results.
6. **Warnings, not silence, for weak recordings**: an `aborted` run, a run with
   dropped steps, or a run recorded with redaction produce warnings in the
   report.

## What this does and does not give

Strict replay needs both halves: tool results from the recording (this ADR) and
model outputs from the response cache in `replay` mode (ADR-005). With both,
re-running an unchanged agent is exact. If the agent code, prompt or model input
changes, replay is no longer a re-run of the same thing: divergence is the
expected, useful result.

## Alternatives rejected

- **Position-based replay** (answer request N with recorded step N): breaks on
  any reordering and gives wrong answers silently.
- **Fuzzy matching of arguments**: would hide exactly the changes that
  regression testing exists to find.
- **Falling back to the live server on a miss**: replay would stop being
  hermetic and could cause side effects.

## Consequences

- Only stdio is supported for replay in this phase. Streamable HTTP replay would
  reuse the same engine behind a small HTTP front end.
- Requests that were not recorded (for example `subscriptions/listen`) cannot be
  replayed and are reported as unexpected.
- Replay is instant; recorded latencies are not reproduced.
- Tool output that depends on time or randomness is replayed as recorded, which
  is the point, but it means replay cannot detect tool-side regressions.
