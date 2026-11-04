# AgentGit

Version control for AI agent runs: record, replay, fork and diff.

An MCP-native proxy sits between an AI agent and its tool servers and records
every tool call and result. Recordings are stored like git objects
(content-addressed, SHA-256), and a git-style CLI works with them.

## Status

Phase 0 (foundations) in progress. Nothing is usable yet.

## Planned features

- Record: capture agent runs through an MCP proxy, no SDK needed
- Replay: re-run an agent against recorded tool outputs and detect divergence
- Fork: change a tool result or prompt at step N and continue as a new branch
- Diff: compare two runs (tool calls, arguments, cost, latency, outcome)
- Agent CI: replay saved scenarios on each pull request
- Chaos testing: inject faults and prompt-injection payloads into tool results

## Limitations

Replaying recorded model outputs is exact. Re-running live with a changed
prompt is approximate, because LLMs are non-deterministic.
