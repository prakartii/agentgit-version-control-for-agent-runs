"""The agent loop, written by hand (no framework).

    while the model asks for tools:
        run each requested tool, send the results back

That is the whole idea. The details that make it safe and testable:

* **Validated tool calls.** The model proposes ``(tool name, arguments)``. Before
  anything runs, the name must be on the agent's allow-list and the arguments
  must parse into the agent's Pydantic model for that tool. A bad call is not
  executed; the model gets an error message and can correct itself.
* **Sequential tool execution.** Tools run one after another in the order the
  model asked. That keeps the recording order deterministic, which replay needs.
* **Bounded.** Step limit, wall-clock limit, per-tool timeout, and a cap on how
  much tool output is fed back.
* **Failures are data.** A tool that raises or times out becomes an error result
  the model can read; it does not crash the loop.
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from mcp.types import TextContent
from pydantic import BaseModel, ValidationError

from agentgit_agents.llm import (
    LLMClient,
    LLMError,
    LLMRequest,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
)

log = logging.getLogger("agentgit.agents")

TRUNCATION_NOTE = "\n[output truncated by the agent harness]"


class ToolResult(BaseModel):
    text: str
    is_error: bool = False


class ToolClient(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


@dataclass(frozen=True)
class AgentSpec:
    """What makes one agent different from another: a prompt and an allow-list."""

    name: str
    system_prompt: str
    # Tool name -> Pydantic model its arguments must satisfy. Anything else is refused.
    tool_models: Mapping[str, type[BaseModel]]


@dataclass(frozen=True)
class AgentConfig:
    model: str
    max_steps: int = 12
    max_tokens: int = 16000
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    tool_timeout_s: float = 60.0
    wall_timeout_s: float = 600.0
    max_tool_chars: int = 20_000


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any]
    outcome: Literal["ok", "tool_error", "invalid_arguments", "unknown_tool", "failed"]
    detail: str = ""


AgentStatus = Literal[
    "completed", "max_steps", "timeout", "llm_error", "truncated", "refused", "stopped"
]


class AgentResult(BaseModel):
    status: AgentStatus
    answer: str = ""
    steps: int = 0  # model calls made
    tool_calls: list[ToolCallRecord] = []
    transcript: list[Message] = []
    error: str = ""


@dataclass
class _State:
    messages: list[Message]
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    steps: int = 0


async def run_agent(
    spec: AgentSpec, config: AgentConfig, llm: LLMClient, tools: ToolClient, task: str
) -> AgentResult:
    """Run ``spec`` on ``task`` until it finishes or hits a limit.

    Model and tool failures are reported in the result, not raised.
    """
    available = await tools.list_tools()
    exposed = [t for t in available if t.name in spec.tool_models]
    state = _State(messages=[Message(role="user", content=[TextBlock(text=task)])])
    try:
        async with asyncio.timeout(config.wall_timeout_s):
            return await _loop(spec, config, llm, tools, exposed, state)
    except TimeoutError:
        return _result(state, "timeout", error=f"exceeded {config.wall_timeout_s}s")


async def _loop(
    spec: AgentSpec,
    config: AgentConfig,
    llm: LLMClient,
    tools: ToolClient,
    exposed: list[ToolSpec],
    state: _State,
) -> AgentResult:
    for _ in range(config.max_steps):
        request = LLMRequest(
            model=config.model,
            system=spec.system_prompt,
            messages=state.messages,
            tools=exposed,
            max_tokens=config.max_tokens,
            effort=config.effort,
        )
        try:
            response = await llm.complete(request)
        except LLMError as exc:
            return _result(state, "llm_error", error=str(exc))
        state.steps += 1
        state.messages.append(Message(role="assistant", content=response.content))

        calls = response.tool_uses
        if response.stop_reason == "tool_use" and calls:
            results = []
            for call in calls:  # in order, one at a time
                result = await _run_tool(spec, config, tools, call.name, call.input, state)
                results.append(
                    ToolResultBlock(
                        tool_use_id=call.id, content=result.text, is_error=result.is_error
                    )
                )
            # All results of one turn go back together in a single user message.
            state.messages.append(Message(role="user", content=list(results)))
            continue

        return _result(state, _final_status(response.stop_reason), answer=response.text)

    return _result(state, "max_steps", error=f"no final answer after {config.max_steps} steps")


def _final_status(stop_reason: str) -> AgentStatus:
    if stop_reason in ("end_turn", "stop_sequence"):
        return "completed"
    if stop_reason == "max_tokens":
        return "truncated"
    if stop_reason == "refusal":
        return "refused"
    return "stopped"


async def _run_tool(
    spec: AgentSpec,
    config: AgentConfig,
    tools: ToolClient,
    name: str,
    raw_arguments: dict[str, Any],
    state: _State,
) -> ToolResult:
    model = spec.tool_models.get(name)
    if model is None:
        return _fail(state, name, raw_arguments, "unknown_tool", f"unknown tool: {name}")
    try:
        arguments = model.model_validate(raw_arguments).model_dump(exclude_unset=True)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()
        )
        return _fail(
            state, name, raw_arguments, "invalid_arguments", f"invalid arguments: {problems}"
        )

    try:
        result = await asyncio.wait_for(tools.call_tool(name, arguments), config.tool_timeout_s)
    except TimeoutError:
        return _fail(
            state, name, arguments, "failed", f"tool timed out after {config.tool_timeout_s}s"
        )
    except Exception as exc:  # a broken tool must not crash the agent
        log.warning("tool %s raised %s", name, exc)
        return _fail(state, name, arguments, "failed", f"tool failed: {type(exc).__name__}: {exc}")

    text = result.text
    if len(text) > config.max_tool_chars:
        text = text[: config.max_tool_chars] + TRUNCATION_NOTE
    state.tool_calls.append(
        ToolCallRecord(
            name=name, arguments=arguments, outcome="tool_error" if result.is_error else "ok"
        )
    )
    return ToolResult(text=text, is_error=result.is_error)


def _fail(
    state: _State,
    name: str,
    arguments: dict[str, Any],
    outcome: Literal["unknown_tool", "invalid_arguments", "failed"],
    message: str,
) -> ToolResult:
    state.tool_calls.append(
        ToolCallRecord(name=name, arguments=arguments, outcome=outcome, detail=message)
    )
    return ToolResult(text=message, is_error=True)


def _result(state: _State, status: AgentStatus, answer: str = "", error: str = "") -> AgentResult:
    return AgentResult(
        status=status,
        answer=answer,
        steps=state.steps,
        tool_calls=state.tool_calls,
        transcript=state.messages,
        error=error,
    )


# ---- MCP tool client ---------------------------------------------------------------


class McpToolClient:
    """Calls tools on an MCP server using the official SDK client.

    The agent does not know or care whether ``params`` launches the server
    directly or launches ``agentgit record -- <server>``. Putting the proxy in
    front is how a run gets recorded, with no change to the agent.
    """

    def __init__(self, params: Any) -> None:
        from mcp import Client

        self._client = Client(params)
        self._open = False

    async def __aenter__(self) -> "McpToolClient":
        await self._client.__aenter__()
        self._open = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._open:
            self._open = False
            await self._client.__aexit__(*exc)  # type: ignore[arg-type]

    async def list_tools(self) -> list[ToolSpec]:
        listing = await self._client.list_tools()
        return [
            ToolSpec(name=t.name, description=t.description or "", input_schema=t.input_schema)
            for t in listing.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = await self._client.call_tool(name, arguments)
        parts = [c.text for c in result.content if isinstance(c, TextContent)]
        text = "\n".join(parts)
        if not text and result.structured_content is not None:
            text = json.dumps(result.structured_content, sort_keys=True)
        return ToolResult(text=text, is_error=bool(result.is_error))
