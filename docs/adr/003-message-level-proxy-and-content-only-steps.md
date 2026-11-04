# ADR-003: Message-level recording proxy and content-only steps

Status: accepted

## Context

The recording proxy sits between an agent and an MCP server. The MCP
specification (revision 2026-07-28) is stateless: every request carries its
protocol version and capabilities in `_meta`, there is no `initialize`
handshake, Streamable HTTP is POST-only, and servers never send requests
(they return `resultType: "input_required"`). Clients and servers built for
earlier revisions still exist and use `initialize`, `Mcp-Session-Id`, GET
streams and server-initiated requests.

A spike against `mcp` 2.3.0 (stdio, client talking to an `MCPServer`
through a logging tap) confirmed the wire format: the client opens with
`server/discover`, then `tools/list` and `tools/call`; results carry
`resultType` and `_meta["io.modelcontextprotocol/serverInfo"]`.

## Decision

### 1. The proxy works on messages, not on SDK objects

The proxy forwards the original bytes unchanged and parses only a copy for
recording. It does not import the MCP SDK. Unknown methods, unknown fields,
future `_meta` keys and both protocol eras pass through untouched.

### 2. Recording is separate from forwarding

- Redaction applies only to the recorded copy, never to forwarded bytes.
- Storage failures are logged and counted; they never interrupt the
  agent's session (fail-open).
- A run states whether it is complete. Requests that never received a
  response are recorded with outcome `no_response`, and a run that ended
  abnormally carries `status: aborted`.

### 3. Steps are content-only

A `Step` holds only what was asked and answered: `method`, `tool_name`,
`arguments` (blob), `result` (blob), `outcome` and optional `events` (blob).
It no longer holds `index` or `duration_ms`. Position lives in
`Run.steps`; wall-clock start and duration live in `Run.timings`, aligned
with `Run.steps` by position.

Why: a fork at step N must reuse the parent's step ids for steps 0..N-1
(structural sharing, like git trees), and diff must compare steps by id
equality. Timing and position would make every step unique and defeat both.

The hashed payloads are the request `params` and the response `result` or
`error`, canonicalized, with the JSON-RPC `id` and the volatile `_meta`
keys removed (see `docs/recording-format.md`).

### 4. One proxy per upstream; runs share a session id

Each proxy process records one run for its upstream server, tagged with
`server` and an optional shared `session_id`. Stitching runs of the same
session into one logical agent run is done at replay time, using the
recorded wall-clock timings.

## Alternatives rejected

- SDK-level proxy (an SDK client plus an SDK server): re-serializes
  messages, drops fields the SDK does not model, needs two code paths for
  the two protocol eras and breaks when the SDK or spec moves.
- One gateway merging many upstreams into one endpoint: gives a single run
  but must understand and rewrite MCP, which breaks transparency.
- Keeping `index` and `duration_ms` in `Step`: simpler, but destroys
  prefix sharing between forks.

## Consequences

- This changes the hash of every step, so it needs a migration (`0002`).
  No real data exists yet.
- Replaying the MCP side alone does not make an agent deterministic: model
  calls do not pass through the proxy. Strict replay also needs recorded
  model outputs (planned with the LLM interface), otherwise replay is
  tool-side only and approximate.
- Redaction changes recorded content, so a replay of a redacted run
  serves placeholders. Runs record that redaction was applied.
