# ADR-005: Hand-written agent loop and a thin, cacheable LLM interface

Status: accepted

## Context

AgentGit needs agents to record, replay and fork. The agents must work through
the recording proxy unmodified, be deterministic enough to test, and show the
mechanics of an agent clearly. The proxy only sees tool traffic, so exact
replay also needs the model's outputs to be captured.

## Decision

1. **One shared loop, thin agents.** `run_agent` is a ~100 line loop (call the
   model; if it asks for tools, run them and send results back; repeat). An
   agent (`coding`, `support`) is only a system prompt plus an allow-list of
   tool names, each with a strict Pydantic argument model. No framework.
2. **Validate every tool call before it runs.** The tool name must be on the
   allow-list and the arguments must parse into the model (`extra="forbid"`).
   A bad call is not sent to the server; the model receives an error result and
   can retry. The proxy therefore never records calls the agent refused.
3. **Sequential tool execution**, in the order the model asked, with all
   results of a turn returned in one user message. Concurrent execution would
   make the recorded order depend on timing, which replay cannot reproduce.
4. **Failures are data.** Tool exceptions, timeouts and oversized output become
   error results; model failures, step limits and wall-clock limits become a
   status on the result. The loop only raises for programming errors.
5. **A provider-neutral interface**: `LLMClient.complete(LLMRequest) ->
   LLMResponse`, using plain Pydantic messages. `AnthropicLLM` uses the
   official `anthropic` SDK. Blocks the loop cannot interpret (Claude's
   `thinking` blocks) are kept verbatim as `OpaqueBlock` and sent back
   unchanged, as the API requires. The request type has no sampling parameters
   (rejected by current models) and the provider never forces a tool choice.
6. **Response caching as a wrapper** (`CachingLLM`), keyed by the SHA-256 of the
   canonical JSON of the whole request, with modes `off`, `readwrite`, `record`
   and `replay`. `replay` never calls a model and treats a missing entry as an
   error (`CacheMiss`), never a silent live call. Entries store the request next
   to the response so a human can read them.
7. **Cost logging without invented prices.** Every call is logged with token
   usage. Dollar cost is computed only from prices the caller supplies;
   otherwise it is reported as unknown. Cache hits are logged as costing 0.
8. **Agents reach tools over MCP with the SDK client.** Putting
   `agentgit record --` in front of the server command records the run; the
   agent code is identical either way.

## Alternatives rejected

- **An agent framework**: hides the loop this project exists to instrument.
- **Raw HTTP to the Claude API**: the official SDK handles auth, retries and
  typing, and is the documented way for Python.
- **Concurrent tool calls**: faster, but non-deterministic recordings.
- **Hard-coding a price table**: prices change and a stale number is worse than
  an honest "unknown".
- **Storing model calls inside the run now**: the cache is a separate, simple
  file store for this phase; linking model calls into runs belongs to replay
  (Phase 4), where the matching rules are decided.

## Consequences

- Strict replay becomes possible: tool results come from the recording, model
  outputs from the cache in `replay` mode. Until Phase 4 connects them, the two
  records are separate.
- The Claude provider is covered by unit tests against SDK-shaped objects, not
  by live API calls: no API key was used in development, so its behaviour
  against the real service is unverified.
- If the model changes its tool-call ids between runs, cache keys for later
  turns change too. In `replay` mode ids come from the cache, so this does not
  matter; in `readwrite` mode it is a cache miss, which is the safe outcome.
