# ADR-007: Forks share steps; diff works on step identity; continuation is live

Status: accepted

## Context

Phase 5 needs to branch a recorded run at step N with a changed tool result,
compare two runs, and let an agent continue past the branch point. The object
model (ADR-002, ADR-003) already makes steps content-only so that runs can share
a prefix; this ADR decides how fork, diff and continuation use that.

## Decision

1. **A fork is a `Run` with `parents=(source,)` and `fork_step=N`.** Its steps are
   `source.steps[:N]` (the same step objects, not copies) plus, if a result is
   edited, one edited copy of step N. Steps after N are dropped: they were
   produced in a world where the original result was true, so keeping them would
   present an untrue history. Editing changes only the `result` blob (and the
   outcome, e.g. `isError: true` becomes `tool_error`); the request is kept so
   the fork still matches the same agent request during replay. Only `tools/call`
   results can be edited. Forking is deterministic: the same edit yields the same
   run id.
2. **What cannot be edited here: prompts.** The model side of an agent never
   passes through MCP, so it is not in a run's steps. A changed prompt is a
   different agent configuration (and a different LLM cache key), not a run edit.
3. **Diff works on step ids first.** Equal ids mean equal content, so the common
   prefix is found without reading any payload. The remainder is aligned with
   `difflib.SequenceMatcher` over step ids (an LCS-style alignment, so one
   inserted step does not mark everything after it as changed). Aligned
   `replace` pairs are compared field by field, and JSON payloads path by path
   (`result.content[0].text`), so the output says what changed, not just that
   something did. The CLI exits 1 when runs differ, which lets CI use it.
4. **Continuation is `replay --live -- <server command>`.** In live mode a request
   with no recorded match is forwarded to a real server instead of being reported
   as divergence. It is counted separately (`live_requests` in the report) and the
   summary says that part was not replayed. Wrapping it in `agentgit record`
   records the whole session as a new run whose prefix is the fork's steps, so
   `agentgit diff original continued` shows exactly where the histories split.
5. **The upstream is started lazily and never sees the replayed prefix.** This
   works for the stateless protocol revision, where every request carries its own
   `_meta`; a spike confirmed the demo server answers a bare `tools/call`. A
   legacy server that requires `initialize` first would reject these requests.
   That limit is documented, not hidden.

## Consequences

- Forks are cheap (a few objects) and the shared prefix is verifiable by id.
- Live continuation re-executes real tools with a live model: it is
  **approximate and can have real side effects**. It is an explicit opt-in flag.
- Because the fork keeps the source's `redaction` setting, replay of a fork
  normalises requests the same way the source was recorded.
- `checkout` is a read-only export for inspection and for preparing an edit; it
  does not change any state, unlike `git checkout`.
