import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentgit_agents.llm import (
    LLMError,
    LLMRequest,
    LLMResponse,
    ScriptedLLM,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    text_response,
    tool_response,
)
from agentgit_agents.loop import AgentConfig, AgentResult, AgentSpec, ToolResult, run_agent


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)


class AddArgs(BaseModel):
    a: int
    b: int


SPEC = AgentSpec(name="t", system_prompt="sys", tool_models={"read": ReadArgs, "add": AddArgs})
CONFIG = AgentConfig(model="m", max_steps=5, tool_timeout_s=1.0, wall_timeout_s=5.0)


class FakeTools:
    """A tool server that records what it was asked to do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.behaviour: dict[str, Any] = {}

    async def list_tools(self) -> list[ToolSpec]:
        schema = {"type": "object"}
        # "secret" exists on the server but is not on the agent's allow-list.
        return [ToolSpec(name=n, input_schema=schema) for n in ("read", "add", "secret")]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, arguments))
        action = self.behaviour.get(name)
        if callable(action):
            return await action(arguments)  # type: ignore[no-any-return]
        return ToolResult(text=f"{name}:{arguments}")


def go(
    llm: Any, tools: FakeTools, config: AgentConfig = CONFIG, task: str = "do it"
) -> AgentResult:
    return asyncio.run(run_agent(SPEC, config, llm, tools, task))


def last_tool_results(llm: ScriptedLLM) -> list[ToolResultBlock]:
    blocks = llm.requests[-1].messages[-1].content
    return [b for b in blocks if isinstance(b, ToolResultBlock)]


def test_runs_tools_then_returns_the_final_answer() -> None:
    llm = ScriptedLLM([tool_response(("1", "read", {"path": "a.txt"})), text_response("all done")])
    tools = FakeTools()
    result = go(llm, tools)
    assert (result.status, result.answer, result.steps) == ("completed", "all done", 2)
    assert tools.calls == [("read", {"path": "a.txt"})]
    (tool_result,) = last_tool_results(llm)
    assert tool_result.tool_use_id == "1" and "a.txt" in tool_result.content
    assert [c.outcome for c in result.tool_calls] == ["ok"]


def test_only_allow_listed_tools_are_offered_to_the_model() -> None:
    llm = ScriptedLLM([text_response("hi")])
    go(llm, FakeTools())
    assert [t.name for t in llm.requests[0].tools] == ["read", "add"]
    assert llm.requests[0].system == "sys"


def test_invalid_arguments_are_not_executed_and_the_model_can_recover() -> None:
    llm = ScriptedLLM(
        [
            tool_response(("1", "add", {"a": "not a number", "b": 2})),
            tool_response(("2", "add", {"a": 1, "b": 2})),
            text_response("3"),
        ]
    )
    tools = FakeTools()
    result = go(llm, tools)
    assert tools.calls == [("add", {"a": 1, "b": 2})]  # the bad call never reached the tool
    bad = llm.requests[1].messages[-1].content[0]
    assert isinstance(bad, ToolResultBlock) and bad.is_error and "invalid arguments" in bad.content
    assert [c.outcome for c in result.tool_calls] == ["invalid_arguments", "ok"]
    assert result.status == "completed"


def test_unexpected_extra_arguments_are_rejected() -> None:
    llm = ScriptedLLM(
        [tool_response(("1", "read", {"path": "a", "sudo": True})), text_response("x")]
    )
    tools = FakeTools()
    go(llm, tools)
    assert tools.calls == []


def test_a_tool_off_the_allow_list_is_refused_even_if_the_server_has_it() -> None:
    llm = ScriptedLLM([tool_response(("1", "secret", {})), text_response("x")])
    tools = FakeTools()
    result = go(llm, tools)
    assert tools.calls == []
    assert result.tool_calls[0].outcome == "unknown_tool"


def test_parallel_calls_run_in_order_and_return_together() -> None:
    llm = ScriptedLLM(
        [
            tool_response(("a", "read", {"path": "1"}), ("b", "read", {"path": "2"})),
            text_response("x"),
        ]
    )
    tools = FakeTools()
    go(llm, tools)
    assert [c[1]["path"] for c in tools.calls] == ["1", "2"]
    results = last_tool_results(llm)
    assert [r.tool_use_id for r in results] == ["a", "b"]  # one user message, both results


def test_a_tool_that_raises_becomes_an_error_result() -> None:
    async def boom(_: dict[str, Any]) -> ToolResult:
        raise RuntimeError("disk on fire")

    tools = FakeTools()
    tools.behaviour["read"] = boom
    llm = ScriptedLLM([tool_response(("1", "read", {"path": "a"})), text_response("recovered")])
    result = go(llm, tools)
    assert result.status == "completed"
    (tr,) = last_tool_results(llm)
    assert tr.is_error and "disk on fire" in tr.content
    assert result.tool_calls[0].outcome == "failed"


def test_a_slow_tool_times_out() -> None:
    async def slow(_: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult(text="late")

    tools = FakeTools()
    tools.behaviour["read"] = slow
    llm = ScriptedLLM([tool_response(("1", "read", {"path": "a"})), text_response("x")])
    config = AgentConfig(model="m", tool_timeout_s=0.1)
    result = go(llm, tools, config)
    (tr,) = last_tool_results(llm)
    assert tr.is_error and "timed out" in tr.content and result.status == "completed"


def test_huge_tool_output_is_truncated() -> None:
    async def huge(_: dict[str, Any]) -> ToolResult:
        return ToolResult(text="x" * 500)

    tools = FakeTools()
    tools.behaviour["read"] = huge
    llm = ScriptedLLM([tool_response(("1", "read", {"path": "a"})), text_response("x")])
    go(llm, tools, AgentConfig(model="m", max_tool_chars=100))
    (tr,) = last_tool_results(llm)
    assert len(tr.content) < 200 and "truncated" in tr.content


def test_tool_error_flag_is_passed_back_to_the_model() -> None:
    async def failing(_: dict[str, Any]) -> ToolResult:
        return ToolResult(text="no such file", is_error=True)

    tools = FakeTools()
    tools.behaviour["read"] = failing
    llm = ScriptedLLM([tool_response(("1", "read", {"path": "a"})), text_response("x")])
    result = go(llm, tools)
    assert last_tool_results(llm)[0].is_error is True
    assert result.tool_calls[0].outcome == "tool_error"


def test_stops_at_max_steps_instead_of_looping_forever() -> None:
    endless = [tool_response((str(i), "read", {"path": "a"})) for i in range(10)]
    result = go(ScriptedLLM(endless), FakeTools(), AgentConfig(model="m", max_steps=3))
    assert result.status == "max_steps" and result.steps == 3


def test_model_failure_is_reported_not_raised() -> None:
    result = go(ScriptedLLM([]), FakeTools())  # the script is empty: the "model" errors
    assert result.status == "llm_error" and "ran out" in result.error


def test_wall_clock_limit() -> None:
    class SlowLLM:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            await asyncio.sleep(5)
            raise LLMError("unreachable")

    result = go(SlowLLM(), FakeTools(), AgentConfig(model="m", wall_timeout_s=0.1))
    assert result.status == "timeout"


def test_other_stop_reasons_map_to_statuses() -> None:
    cut = LLMResponse(content=[TextBlock(text="par")], stop_reason="max_tokens", model="m")
    refused = LLMResponse(content=[], stop_reason="refusal", model="m")
    assert go(ScriptedLLM([cut]), FakeTools()).status == "truncated"
    assert go(ScriptedLLM([refused]), FakeTools()).status == "refused"


def test_the_transcript_alternates_and_keeps_the_task() -> None:
    llm = ScriptedLLM([tool_response(("1", "read", {"path": "a"})), text_response("x")])
    result = go(llm, FakeTools(), task="the task")
    assert [m.role for m in result.transcript] == ["user", "assistant", "user", "assistant"]
    first = result.transcript[0].content[0]
    assert isinstance(first, TextBlock) and first.text == "the task"
