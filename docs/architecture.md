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
│        └──────► IndexService ──► Postgres          │
└────────────────────────────────────────────────────┘
```

## Packages

| Package | Role |
|---|---|
| `packages/core` | Object model (blob, step, run), hashing, object store, repository with integrity checks |
| `packages/index` | Postgres index of runs, steps and refs; Alembic migrations |
| `apps/proxy` | The recording proxy: framing, correlator, redaction, sink, stdio and HTTP adapters |
| `apps/cli` | `agentgit` command: `record`, `log`, `show` |
| `apps/api` | Read-only HTTP API over the index |
| `packages/demo_servers` | Two tiny MCP servers for demos and tests |

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
5. **Nothing is implied.** Unanswered requests are steps with outcome
   `no_response`; abnormal sessions are `aborted`; gaps and redaction are
   flags on the run, and `log` / `show` print them.
6. **One proxy per upstream server.** Several servers in one agent session
   produce several runs sharing a `--session` id; stitching them is a replay
   concern.

## Replay and fork: what is exact and what is not

The proxy sees only the tool side of an agent. Calls from the agent to its LLM
do not pass through MCP. That gives three different things, and the docs and
run metadata keep them apart:

| Mode | Tool results | Model outputs | Exactness |
|---|---|---|---|
| Strict replay | from the recording | from a recorded model-call cache | exact for the same agent code; any different request is reported as divergence |
| Tool-side replay | from the recording | live | approximate; the model may choose differently, divergence is reported |
| Fork / live re-execution (edited tool result or prompt at step N) | steps before N replayed, then live | live | approximate; runs are stamped as such |

Only recording exists today. Strict replay additionally needs model calls to be
captured, which is planned with the LLM interface.

## Known limitations

- **A hard-killed proxy loses the run object.** Blobs and steps are written as
  exchanges complete, but the run that ties them together is written at the end.
  If the proxy is killed without a chance to shut down (`SIGKILL`,
  `TerminateProcess`), those steps exist in the store but no run references
  them. A journal or periodic checkpoint is the planned fix.
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
- **Overhead has not been measured.** No performance claims are made until the
  benchmark phase.
