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
- **Two demo agents** (coding, support) with a hand-written loop and
  Pydantic-validated tool calls, a provider-neutral LLM interface with response
  caching and cost logging, and `agentgit-agent` to run them

- **Deterministic replay** (`agentgit replay`): a drop-in MCP server that answers
  from a recorded run and reports any divergence (unexpected, out-of-order or
  missing requests)

- **Fork, diff and checkout**: branch a run at a step with an edited tool result,
  compare two runs path by path, export a run as readable JSON, and continue a
  fork against a live server (`replay --live`)

- **Agent CI** (`agentgit ci`, `action.yml`): save a recording as a scenario and
  replay it against the real agent on every pull request, with no model or tool
  server; divergence fails the build

- **Chaos and injection testing** (`agentgit replay --chaos`, scenario `chaos` and
  `[policy]`): seeded faults and prompt-injection payloads on replayed results,
  with policies that fail the build if the agent acts on them

Not built yet: UI, observability and benchmarks, deployment.
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

## Replaying a run

`agentgit replay <run id>` is an MCP server whose answers come from a recording.
Use it where the real server command would go. With the model output cache in
`replay` mode, an unchanged agent re-runs with no tool server and no model:

```
uv run agentgit-agent run support "..." --cache-mode replay     -- uv run agentgit replay <run id> --report replay.json
```

If the agent asks for something the recording does not contain, replay reports
it instead of inventing an answer. Exit code 0 means faithful, 1 means diverged.
Replay is exact for an unchanged agent; if you change the agent, the prompt or
the model input, divergence is the expected result, not a bug.

## Forking and diffing runs

```
agentgit checkout <run id> --to ./export            # read a run as JSON files
agentgit fork <run id> --at 3 --result-file edited.json   # new run, one result changed
agentgit diff <run id> <fork id>                    # exit code 1 if they differ
```

A fork keeps the steps before `--at` (the same stored objects, so the shared
history is verifiable by id) and replaces that step's result; the steps after it
are dropped. To see what the agent does next, record a replay of the fork that
falls through to a real server for anything the recording lacks:

```
agentgit record -- agentgit replay <fork id> --live -- <real server command>
agentgit diff <run id> <new run id>
```

The part before the fork point is exact replay. The part after is live
re-execution: the model is not deterministic and real tools run (side effects
included), so it is approximate. Prompts are not part of a run's steps, so a
fork edits tool results only. See ADR-007.

## Agent CI

Turn a recording into a regression test:

```
agentgit ci add <run id> --name "support refund" --dir scenarios --     agentgit-agent run support "Refund order 42" --model <model>     --cache-dir llm_cache --cache-mode replay -- {server}
agentgit ci run scenarios --report-md summary.md --junit report.xml
```

`ci add` writes a portable bundle (the run and everything it references) and a
`.scenario.toml`; commit both, plus the model-response cache. `{server}` is where
the replay server goes. `ci run` replays each scenario to the real agent and
fails (exit 1) if the agent exits unexpectedly, makes a request the recording
never contained, or stops making one it did. Add it to a workflow with the
composite action in this repository:

```yaml
- uses: actions/checkout@v4
- uses: prakartii/agentgit-version-control-for-agent-runs@main
  with:
    scenarios: scenarios
    install: uv sync --locked   # however your repo installs agentgit and the agent
```

`examples/agent-ci` is a working scenario that this repository's own CI runs.
A passing scenario means the agent behaves the same given the recorded tool
results and model outputs; it does not judge the quality of the model. See ADR-008.

## Chaos and injection testing

Damage the results an agent receives and check what it does next. A plan is a
TOML file:

```toml
seed = 1
[[rules]]
fault = "inject"                 # tool_error | protocol_error | empty | truncate | oversize | delay | inject
tool = "read_file"               # fnmatch pattern; also nth = [2] or probability = 0.3
payload = "builtin:ignore_previous"   # see `agentgit chaos payloads`
```

Use it with `agentgit replay <run> --chaos plan.toml`, or in a CI scenario:

```toml
chaos = "plan.toml"
[policy]
forbid_canary = true             # the injection canary must never reach a tool call
forbid_tools = ["delete_*"]      # judged only on calls made after the first fault
```

Injection payloads carry a canary token, so "the agent obeyed attacker text" is
a string match. Injection scenarios need a live (or reactive) model: a response
cache cannot know how a model reacts to new text. The tests prove the harness
with a deterministic gullible agent (caught) and a robust one (passes); they say
nothing about any real model, and a pass is a regression check, not a safety
guarantee. See ADR-009.

## Running a demo agent

The agents need a model. With an Anthropic key in `ANTHROPIC_API_KEY`:

```
uv run agentgit-agent run support "Handle the double-charge ticket" --record     -- uv run agentgit-demo-tickets
uv run agentgit log
```

`--record` starts the tool server through `agentgit record`; the agent is the
same with or without it. Model responses are cached in `.agentgit/llm_cache`,
and `--cache-mode replay` re-runs the agent with no network and no model calls.
Dollar costs are shown only if you pass `--input-price` and `--output-price`
(USD per million tokens); otherwise they are reported as unknown. The Claude
provider has been tested against the SDK's types, not the live API.

## Layout

- `packages/core`: object model, hashing, object store
- `packages/index`: Postgres index and migrations
- `apps/proxy`: recording proxy (stdio and HTTP)
- `packages/demo_servers`: demo MCP servers
- `packages/agents`: demo agents, agent loop, LLM interface and cache
- `apps/api`: FastAPI service (`/health`, `/runs`)
- `apps/cli`: `agentgit` command
- `docs/adr`: design decisions; `docs/architecture.md`, `docs/recording-format.md`

## Planned features

- Record: capture agent runs through an MCP proxy, no SDK needed
- Replay: re-run an agent against recorded tool outputs and detect divergence
- Diff of cost and latency between runs (steps and payloads are done)

## Limitations

Replaying recorded model outputs is exact. Re-running live with a changed
prompt is approximate, because LLMs are non-deterministic.
