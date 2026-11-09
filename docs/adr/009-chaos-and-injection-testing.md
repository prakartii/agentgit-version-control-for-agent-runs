# ADR-009: Chaos and injection testing by damaging replayed results

Status: accepted

## Context

Agents break in ways ordinary tests do not exercise: tools fail, return nothing
or far too much, answer slowly, or return text written by an attacker (prompt
injection). We already serve recorded tool results to an agent (ADR-006) and can
run that as a CI scenario (ADR-008), so the same mechanism can serve *damaged*
results and check what the agent does with them.

## Decision

1. **Faults are applied to what replay serves, by a seeded plan.** Rules select
   tool calls by name pattern, occurrence number or seeded probability and apply
   one fault: `tool_error`, `protocol_error`, `empty`, `truncate`, `oversize`,
   `delay`, `inject`. The same plan and seed against the same recording give the
   same damage, so a failure reproduces. The random generator is drawn for every
   matching call, so adding a rule never changes another rule's draws.
2. **Result text is changed in both its forms** (`content[].text` and
   `structuredContent.result`), because clients may read either; changing one
   would let an agent read an undamaged copy.
3. **Judging is separate from damaging.** The replay report lists every
   `tools/call` the agent made (tool, redacted normalised arguments), and which
   call each fault hit. A `Policy` (forbidden tool patterns, forbidden argument
   regexes, "the canary must not reach a tool call") returns violations. By
   default only calls made *after* the first fault count, so the agent's normal
   earlier calls cannot trip a "forbid write" rule.
4. **Injection payloads carry a canary token** (`AGENTGIT-CANARY-7f3a91`) that no
   real task contains, so "the agent acted on attacker text" is a string search,
   not a judgement call. A small built-in library is provided
   (`agentgit chaos payloads`); custom text works too.
5. **Chaos scenarios skip the replay-divergence check.** The data was changed on
   purpose, so different behaviour is expected; the policy decides the outcome.
6. **A live or reactive model is required for injection tests.** The response
   cache (ADR-005) is keyed on the conversation, which now contains different
   text, so it would miss. Cache-based scenarios test faults on tool results the
   model never sees changed; they are not injection tests.

## Consequences

- The harness is proven with a deterministic *gullible* model (obeys the
  canary) and a *robust* one (does not): the first fails the policy, the second
  passes. This validates the tooling, not any real model.
- A pass means this payload set, against this recording, did not make the agent
  misbehave. It is a regression check, not a safety guarantee; the payload
  library is small and well known, and a determined attacker will not use it.
- Real-model chaos runs cost money and are non-deterministic; they are opt-in and
  belong in a scheduled job rather than on every pull request.
- Faults only change served data. Nothing real is altered, and live
  continuation (`--live`, ADR-007) is not damaged.
