# AgentGit

Version control for AI agent runs: record, replay, fork and diff.

An MCP-native proxy sits between an AI agent and its tool servers and records
every tool call and result. Recordings are stored like git objects
(content-addressed, SHA-256), and a git-style CLI works with them.

## Status

Working today:

- Content-addressed object store, Postgres index, `agentgit log` / `show`
- **Recording proxy** for MCP over stdio and Streamable HTTP (`agentgit record`)
  that forwards traffic unchanged and records every exchange, with secret
  redaction on the recorded copy and explicit completeness flags
- Two demo MCP servers, and integration tests that drive the official SDK
  client through the proxy

Not built yet: replay, fork, diff, Agent CI, chaos testing, UI.
See [docs/architecture.md](docs/architecture.md) for the design and the
known limitations.

## Quickstart

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker.

```
uv sync
uv run pytest
uv run agentgit version
uv run uvicorn agentgit_api.main:app   # GET /health
docker compose up -d                   # Postgres on :5433
DATABASE_URL=postgresql+psycopg://agentgit:agentgit@localhost:5433/agentgit   uv run --directory packages/index alembic upgrade head
```

## Recording a session

Replace an agent's MCP server command with `agentgit record -- <the command>`.
In an MCP client config that looks like:

```json
{
  "mcpServers": {
    "files": {
      "command": "agentgit",
      "args": ["record", "--session", "my-run", "--", "uv", "run", "agentgit-demo-files"]
    }
  }
}
```

For a server that is already running over HTTP, point the agent at the proxy
instead (Ctrl+C stops it and saves the run):

```
uv run agentgit record --upstream http://localhost:9000/mcp --port 8765
```

Then inspect it:

```
uv run agentgit log
uv run agentgit show <run id prefix>
```

If a recording process is killed before it can save its run, run
`uv run agentgit recover` to rebuild it from the journal (see ADR-004).

Add `--no-index` to skip Postgres (runs are still stored in `.agentgit/objects`).
Nothing the proxy does changes the traffic the agent and server see; secrets
are redacted only in what is stored. Redaction is best-effort, so treat
`.agentgit/` as sensitive.

## Layout

- `packages/core`: object model, hashing, object store
- `packages/index`: Postgres index and migrations
- `apps/proxy`: recording proxy (stdio and HTTP)
- `packages/demo_servers`: demo MCP servers
- `apps/api`: FastAPI service (`/health`, `/runs`)
- `apps/cli`: `agentgit` command
- `docs/adr`: design decisions; `docs/architecture.md`, `docs/recording-format.md`

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
