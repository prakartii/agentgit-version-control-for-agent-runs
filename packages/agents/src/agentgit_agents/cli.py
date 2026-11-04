"""``agentgit-agent``: run a demo agent against an MCP server.

    agentgit-agent run support "Close the duplicate-charge ticket" --record \\
        -- uv run agentgit-demo-tickets

With ``--record`` the server is launched through ``agentgit record`` so the run
is captured; the agent code is identical either way.
"""

import asyncio
import os
import sys
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from mcp import StdioServerParameters

from agentgit_agents.agents import AGENTS
from agentgit_agents.llm import (
    DEFAULT_MODEL,
    AnthropicLLM,
    CacheMode,
    CachingLLM,
    CostLog,
    FileResponseCache,
    LLMClient,
    ModelPrice,
    ScriptedLLM,
)
from agentgit_agents.loop import AgentConfig, AgentResult, McpToolClient, run_agent

app = typer.Typer(help="Run the AgentGit demo agents.", no_args_is_help=True)


class AgentName(StrEnum):
    coding = "coding"
    support = "support"


class Provider(StrEnum):
    anthropic = "anthropic"
    scripted = "scripted"


@app.callback()
def main() -> None:
    """Root group (keeps `run` a subcommand)."""


def _say(message: str) -> None:
    typer.echo(message, err=True)


@app.command()
def run(
    agent: Annotated[AgentName, typer.Argument(help="Which agent to run.")],
    task: Annotated[str, typer.Argument(help="What the agent should do.")],
    command: Annotated[list[str], typer.Argument(help="MCP server command, after `--`.")],
    provider: Annotated[
        Provider, typer.Option(help="Where model responses come from.")
    ] = Provider.anthropic,
    model: Annotated[str, typer.Option(help="Model id (anthropic provider).")] = DEFAULT_MODEL,
    script: Annotated[
        Path | None, typer.Option(help="JSON file of responses (scripted provider).")
    ] = None,
    cache_dir: Annotated[Path, typer.Option(help="Where model responses are cached.")] = Path(
        ".agentgit/llm_cache"
    ),
    cache_mode: Annotated[
        str, typer.Option(help="off | readwrite | record | replay")
    ] = "readwrite",
    max_steps: Annotated[int, typer.Option(help="Maximum model calls.")] = 12,
    record: Annotated[
        bool, typer.Option(help="Launch the server through `agentgit record`.")
    ] = False,
    session: Annotated[str | None, typer.Option(help="Session id stored on the recording.")] = None,
    input_price: Annotated[
        float | None, typer.Option(help="USD per 1M input tokens (for cost estimates).")
    ] = None,
    output_price: Annotated[float | None, typer.Option(help="USD per 1M output tokens.")] = None,
) -> None:
    """Run an agent on a task using the MCP server given after `--`."""
    if cache_mode not in ("off", "readwrite", "record", "replay"):
        _say("--cache-mode must be one of: off, readwrite, record, replay")
        raise typer.Exit(2)
    mode: CacheMode = cache_mode  # type: ignore[assignment]

    pricing = {}
    if input_price is not None and output_price is not None:
        pricing[model] = ModelPrice(input=input_price, output=output_price)
    cost_log = CostLog(pricing=pricing)
    cache = FileResponseCache(cache_dir)

    if provider is Provider.scripted:
        if script is None:
            _say("--script is required with --provider scripted")
            raise typer.Exit(2)
        llm: LLMClient = CachingLLM(ScriptedLLM.from_file(script), cache, "off", cost_log)
    else:
        inner = None if mode == "replay" else AnthropicLLM()
        llm = CachingLLM(inner, cache, mode, cost_log)

    server = list(command)
    if record:
        sid = session or uuid.uuid4().hex[:8]
        server = [sys.executable, "-m", "agentgit_cli", "record", "--session", sid, "--", *server]

    result = asyncio.run(_run(agent.value, task, server, model, max_steps, llm))

    typer.echo(result.answer or f"(no answer: {result.status} {result.error})")
    _say(
        f"\nstatus: {result.status}   model calls: {result.steps}"
        f"   tool calls: {len(result.tool_calls)}"
    )
    _say(f"model calls served live: {cost_log.live_calls}, from cache: {cost_log.cached_calls}")
    spent = cost_log.tokens_spent
    _say(f"tokens billed: {spent.input_tokens} in / {spent.output_tokens} out")
    cost = cost_log.estimated_cost_usd
    _say(
        f"estimated cost: ${cost:.4f}"
        if cost is not None
        else "estimated cost: unknown (give --input-price and --output-price)"
    )
    if result.status != "completed":
        raise typer.Exit(1)


async def _run(
    agent: str, task: str, server: list[str], model: str, max_steps: int, llm: LLMClient
) -> AgentResult:
    params = StdioServerParameters(command=server[0], args=server[1:], env=dict(os.environ))
    async with McpToolClient(params) as tools:
        return await run_agent(
            AGENTS[agent], AgentConfig(model=model, max_steps=max_steps), llm, tools, task
        )
