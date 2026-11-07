# Architecture

AgentGit records what happens between an AI agent and its MCP tool servers, and
stores it like git stores history. This page describes what exists today
(through the recording proxy). Replay, fork and diff build on this format and
are not implemented yet.

## Data flow

```
agent (any framework)
   │  MCP messages (JSON-RPC)
   ▼
┌───────────────── agentgit record ──────────────────┐
│ transport adapter ── forward unchanged ───────────►│──► real MCP server
│   (stdio or HTTP)                                  │     (child process or URL)
│        │ copy                                      │
│        ▼                                           │
│   Correlator   pairs requests with responses       │
│        ▼                                           │
│   RecordingSession  (async queue, fail-open)       │
│        │  Redactor ─ filter _meta ─ canonical JSON │
│        ▼                                           │
│   Repository ──► object store (files)              │
│        ├──────► IndexService ──► Postgres          │
│        └──────► Journal (crash recovery)           │
└────────────────────────────────────────────────────┘
```

## Packages

| Package | Role |
|---|---|
| `packages/core` | Object model (blob, step, run), hashing, object store, repository with integrity checks |
| `packages/index` | Postgres index of runs, steps and refs; Alembic migrations |
| `apps/proxy` | The recording proxy: framing, correlator, redaction, sink, stdio and HTTP adapters |
| `apps/cli` | `agentgit` command: `record`, `recover`, `log`, `show` |
| `apps/api` | Read-only HTTP API over the index |
| `packages/demo_servers` | Two tiny MCP servers for demos and tests |
| `packages/replay` | Replay engine and stdio replay server (`agentgit replay`) |
| `packages/agents` | Hand-written agent loop, the coding and support agents, the LLM interface (Claude provider, response cache, cost log) |

## Object model

Content-addressed, SHA-256 over `type length\0payload` (ADR-001).

- **Blob**: canonical JSON of a request's params, a response's result or error,
  or a list of events.
- **Step**: the content of one exchange (`method`, `tool_name`, `arguments`,
  `result`, `outcome`, `events`, `origin`). It has no position, timing or wire
  ids, so identical calls have identical step ids in every run (ADR-003).
- **Run**: ordered step ids, per-step timings, parents (for forks), metadata and
  explicit completeness flags: `status`, `redacted`, `dropped_steps`.
- **Ref**: a mutable name pointing at a run; stored in Postgres only.

The object store is the source of truth; Postgres is a rebuildable index
(ADR-002). Details of what is recorded: [recording-format.md](recording-format.md).

## Design decisions in the proxy

1. **Forward bytes, observe a copy** (ADR-003). The proxy never re-serialises
   messages, so unknown fields, `_meta` keys and both protocol eras pass
   through untouched. It does not import the MCP SDK.
2. **Tap order matters.** In stdio, a client message is observed right after
   it is handed to the server's pipe with no `await` in between, so a response
   can never be seen before its request. In HTTP the request is observed before
   it is forwarded.
3. **Redaction only on the recording path.** The server receives real
   secrets; the stored blobs hold placeholders.
4. **Fail-open recording.** `submit` never raises or blocks. A bounded queue
   drops (and counts) exchanges rather than slowing the agent; storage errors
   are counted in `dropped_steps`; indexing has a time limit and its failure
   leaves the stored run intact.
5. **Crash safety without a big write.** Steps are journaled one line each, so
   a killed proxy leaves a recoverable record (ADR-004).
6. **Nothing is implied.** Unanswered requests are steps with outcome
   `no_response`; abnormal sessions are `aborted`; gaps and redaction are
   flags on the run, and `log` / `show` print them.
7. **One proxy per upstream server.** Several servers in one agent session
   produce several runs sharing a `--session` id; stitching them is a replay
   concern.

## Agents and the LLM interface (ADR-005)

The agent loop (`packages/agents/loop.py`) validates each proposed tool call
against a Pydantic model, runs tools sequentially, and reports every failure as
data. Model calls go through `LLMClient`; `CachingLLM` adds response caching
(`readwrite`, `record`, `replay`) and cost logging. Because the same scripted
or cached model outputs and the same tool results give the same step ids, an
agent run repeated twice produces an identical sequence of recorded steps (a
test pins this down).

## Replay (ADR-006)

`agentgit replay <run>` is an MCP server that answers from a recording. A live
request is normalised exactly like the recorder normalised it (shared
`normalize_body`), hashed, and served by the first unconsumed recorded step with
the same method and params blob id; repeated identical calls are served in
recorded order. There is no fuzzy matching and no fallback to a live server.
Divergence is reported in three kinds: `unexpected_request` (the agent gets a
JSON-RPC error, never an invented result), `out_of_order` and `missing_request`.
Recording a replayed session yields exactly the original step ids, which a test
checks end to end.

## Replay and fork: what is exact and what is not

The proxy sees only the tool side of an agent. Calls from the agent to its LLM
do not pass through MCP. That gives three different things, and the docs and
run metadata keep them apart:

| Mode | Tool results | Model outputs | Exactness |
|---|---|---|---|
| Strict replay | from the recording | from a recorded model-call cache | exact for the same agent code; any different request is reported as divergence |
| Tool-side replay | from the recording | live | approximate; the model may choose differently, divergence is reported |
| Fork / live re-execution (edited tool result or prompt at step N) | steps before N replayed, then live | live | approximate; runs are stamped as such |

Strict replay and tool-side replay now exist (ADR-005, ADR-006): tool results
come from a run, model outputs from the response cache in `replay` mode, or from
a live model for tool-side replay. Fork is not built yet.

## Known limitations

- **A hard-killed proxy is recoverable, with caveats** (ADR-004). Each session
  keeps a write-ahead journal; after `SIGKILL` or `TerminateProcess`, run
  `agentgit recover` to turn it into an `aborted` run. Requests that were still
  in flight when the process died are not in it, and an operating-system crash
  or power loss can still lose recent steps (lines are not fsynced).
- **HTTP mode saves on graceful shutdown** (Ctrl+C / SIGTERM). One proxy process
  is one run, however many client sessions connect. On Windows, signals sent to
  a process started through a virtual-environment launcher may not reach the
  interpreter.
- **The recording is a canonical content record, not a wire capture**: key order
  is not preserved, duplicate keys collapse, numbers are re-spelled by value,
  and reserved `_meta` keys are dropped (see recording-format.md). Forwarded
  bytes are unaffected.
- **Not recorded**: HTTP headers; stderr; notifications that belong to no
  request (including `subscriptions/listen` streams and list-changed
  notifications, which are only counted); legacy GET/DELETE session traffic is
  forwarded but not recorded.
- **Compression is disabled upstream.** The HTTP proxy sends
  `Accept-Encoding: identity` so replies can be observed without decoding. It
  is the only request header the proxy changes.
- **Messages over 16 MiB (stdio) or 64 MiB (HTTP JSON replies) are forwarded
  but not recorded**, and the run metadata says how many.
- **Redaction is best-effort pattern matching.** Treat the object store as
  sensitive. A redacted run replays placeholders, so replay must apply the same
  redactor to live requests before matching.
- **Legacy protocol revisions** (`initialize` handshake, sessions, server
  requests) pass through and are recorded generically, but are only tested with
  a scripted fake, not with a real legacy SDK.
- **Replay limits** (ADR-006): stdio only; recorded latencies are not reproduced;
  requests that were never recorded (for example `subscriptions/listen`) are
  reported as unexpected; the model-output cache and the run are linked by the
  user, not stored together.
- **Overhead has not been measured.** No performance claims are made until the
  benchmark phase.
