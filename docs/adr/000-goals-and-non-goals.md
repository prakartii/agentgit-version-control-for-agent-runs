# ADR-000: Goals and non-goals

Status: accepted

## Context

AI agents are hard to debug and test: runs are non-deterministic, tool
outputs change, and there is no equivalent of version control for a run.

## Decision

AgentGit treats an agent run like a git history. An MCP proxy records every
tool call and result into a content-addressed object store (SHA-256). Runs
form a DAG with parent pointers; refs name runs and forks. A git-style CLI
(`log`, `show`, `diff`, `fork`, `checkout`, `replay`, `status`) works on it.

### Goals

- Zero-code recording of agent runs through an MCP proxy
- Deterministic replay of recorded tool outputs with divergence detection
- Forking a run at step N with an edited tool result or prompt
- Semantic diff of two runs (tool sequence, arguments, cost, latency, outcome)
- Agent CI: replay saved scenarios on each pull request
- Fault and prompt-injection testing of agents

### Non-goals

- Making LLMs deterministic. Replaying recorded model outputs is exact;
  re-running live with a changed prompt is approximate.
- Being an agent framework. AgentGit observes agents, it does not build them.
- Supporting non-MCP tool protocols in the first version.
- Multi-tenant hosting or access control.

## Alternatives rejected

- SDK-based instrumentation: needs code changes per framework. A proxy works
  with any agent that speaks MCP.
- A plain log table: no deduplication, no DAG, no cheap forking.

## Consequences

- Agents that bypass MCP cannot be recorded.
- Storage is append-only and deduplicated by content hash.
