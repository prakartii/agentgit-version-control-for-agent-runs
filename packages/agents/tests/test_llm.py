import asyncio
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.types import Message as ApiMessage

from agentgit_agents.llm import (
    AnthropicLLM,
    CacheMiss,
    CachingLLM,
    CostLog,
    FileResponseCache,
    LLMError,
    LLMRequest,
    LLMResponse,
    MemoryResponseCache,
    Message,
    ModelPrice,
    OpaqueBlock,
    ScriptedLLM,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
    text_response,
    tool_response,
)


def request(text: str = "hi", **overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "model": "m",
        "messages": [Message(role="user", content=[TextBlock(text=text)])],
    }
    fields.update(overrides)
    return LLMRequest(**fields)


def reply(text: str, tokens: tuple[int, int] = (10, 5), model: str = "m") -> LLMResponse:
    return LLMResponse(
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1]),
        model=model,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---- cache key ---------------------------------------------------


def test_equal_requests_have_equal_keys_and_any_change_alters_it() -> None:
    assert request().cache_key() == request().cache_key()
    assert request().cache_key() != request("other").cache_key()
    assert request().cache_key() != request(model="n").cache_key()
    assert request().cache_key() != request(system="be brief").cache_key()
    assert request().cache_key() != request(max_tokens=5).cache_key()
    tool = ToolSpec(name="t", input_schema={"type": "object"})
    assert request().cache_key() != request(tools=[tool]).cache_key()


def test_key_ignores_schema_key_order() -> None:
    a = ToolSpec(name="t", input_schema={"type": "object", "required": ["x"]})
    b = ToolSpec(name="t", input_schema={"required": ["x"], "type": "object"})
    assert request(tools=[a]).cache_key() == request(tools=[b]).cache_key()


# ---- caching modes -----------------------------------------------


def test_readwrite_calls_the_model_once_for_repeated_requests() -> None:
    inner = ScriptedLLM([reply("first"), reply("second")])
    llm = CachingLLM(inner, MemoryResponseCache(), "readwrite")
    assert run(llm.complete(request())).text == "first"
    assert run(llm.complete(request())).text == "first"  # served from cache
    assert len(inner.requests) == 1
    assert (llm.cost_log.live_calls, llm.cost_log.cached_calls) == (1, 1)


def test_record_mode_always_calls_and_refreshes_the_cache() -> None:
    cache = MemoryResponseCache()
    inner = ScriptedLLM([reply("old"), reply("new")])
    llm = CachingLLM(inner, cache, "record")
    run(llm.complete(request()))
    assert run(llm.complete(request())).text == "new"
    assert cache.get(request().cache_key()).text == "new"  # type: ignore[union-attr]


def test_replay_serves_recorded_responses_without_a_model() -> None:
    cache = MemoryResponseCache()
    run(CachingLLM(ScriptedLLM([reply("recorded")]), cache, "record").complete(request()))
    replay = CachingLLM(None, cache, "replay")
    assert run(replay.complete(request())).text == "recorded"


def test_replay_miss_is_an_error_not_a_silent_live_call() -> None:
    replay = CachingLLM(None, MemoryResponseCache(), "replay")
    with pytest.raises(CacheMiss):
        run(replay.complete(request()))


def test_only_replay_may_omit_the_model_client() -> None:
    with pytest.raises(ValueError):
        CachingLLM(None, MemoryResponseCache(), "readwrite")


def test_off_mode_never_touches_the_cache() -> None:
    cache = MemoryResponseCache()
    llm = CachingLLM(ScriptedLLM([reply("a")]), cache, "off")
    run(llm.complete(request()))
    assert cache.entries == {}


# ---- file cache --------------------------------------------------


def test_file_cache_roundtrips_every_block_type(tmp_path: Path) -> None:
    cache = FileResponseCache(tmp_path)
    response = LLMResponse(
        content=[
            OpaqueBlock(data={"type": "thinking", "thinking": "", "signature": "sig"}),
            TextBlock(text="é世界"),
            ToolUseBlock(id="t1", name="read", input={"path": "a"}),
        ],
        stop_reason="tool_use",
        usage=Usage(input_tokens=3, output_tokens=4),
        model="m",
    )
    key = request().cache_key()
    cache.put(key, request(), response)
    assert cache.get(key) == response
    assert cache.get("0" * 64) is None


def test_corrupt_cache_entry_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = FileResponseCache(tmp_path)
    key = request().cache_key()
    cache.put(key, request(), reply("x"))
    next(tmp_path.rglob("*.json")).write_text("{ not json")
    assert cache.get(key) is None


def test_file_cache_entry_stores_the_request_for_humans(tmp_path: Path) -> None:
    cache = FileResponseCache(tmp_path)
    cache.put(request("hello").cache_key(), request("hello"), reply("x"))
    assert "hello" in next(tmp_path.rglob("*.json")).read_text(encoding="utf-8")


# ---- cost log ----------------------------------------------------


def test_cost_is_computed_from_supplied_prices() -> None:
    log = CostLog(pricing={"m": ModelPrice(input=2.0, output=10.0)})
    log.record(reply("x", tokens=(1_000_000, 500_000)), from_cache=False, duration_ms=1)
    assert log.estimated_cost_usd == pytest.approx(2.0 + 5.0)


def test_unknown_price_means_unknown_cost_not_zero() -> None:
    log = CostLog()
    log.record(reply("x"), from_cache=False, duration_ms=1)
    assert log.estimated_cost_usd is None
    assert log.tokens_spent.input_tokens == 10


def test_cache_hits_cost_nothing_and_bill_no_tokens() -> None:
    log = CostLog()
    log.record(reply("x"), from_cache=True, duration_ms=1)
    assert log.estimated_cost_usd == 0.0
    assert log.tokens_spent == Usage()


# ---- Claude provider ---------------------------------------------


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.messages = FakeMessages(result)


def api_message(content: list[dict[str, Any]], stop: str = "end_turn") -> ApiMessage:
    return ApiMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
    )


def test_provider_sends_a_minimal_valid_request() -> None:
    client = FakeClient(api_message([{"type": "text", "text": "ok"}]))
    tool = ToolSpec(name="read", description="Read", input_schema={"type": "object"})
    run(
        AnthropicLLM(client).complete(
            request(system="sys", tools=[tool], effort="high", model="claude-test")
        )
    )
    sent = client.messages.kwargs
    assert sent["model"] == "claude-test" and sent["system"] == "sys"
    assert sent["tools"] == [
        {"name": "read", "description": "Read", "input_schema": {"type": "object"}}
    ]
    assert sent["output_config"] == {"effort": "high"}
    # Removed on current models; sending any of these is a 400.
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert forbidden not in sent


def test_provider_omits_optional_fields_when_unset() -> None:
    client = FakeClient(api_message([{"type": "text", "text": "ok"}]))
    run(AnthropicLLM(client).complete(request()))
    assert set(client.messages.kwargs) == {"model", "max_tokens", "messages"}


def test_provider_parses_text_tool_use_and_keeps_thinking_verbatim() -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "abc123"}
    client = FakeClient(
        api_message(
            [thinking, {"type": "text", "text": "reading"}]
            + [{"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "a"}}],
            stop="tool_use",
        )
    )
    response = run(AnthropicLLM(client).complete(request()))
    assert response.stop_reason == "tool_use" and response.model == "claude-test"
    assert response.usage == Usage(input_tokens=11, output_tokens=7)
    assert response.text == "reading"
    assert response.tool_uses[0].input == {"path": "a"}
    assert isinstance(response.content[0], OpaqueBlock)
    assert response.content[0].data["signature"] == "abc123"


def test_assistant_turn_is_sent_back_with_thinking_unchanged() -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "abc123"}
    history = [
        Message(role="user", content=[TextBlock(text="go")]),
        Message(
            role="assistant",
            content=[
                OpaqueBlock(data=thinking),
                ToolUseBlock(id="t1", name="read", input={"p": 1}),
            ],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="data")]),
    ]
    client = FakeClient(api_message([{"type": "text", "text": "done"}]))
    run(AnthropicLLM(client).complete(request(messages=history)))
    sent = client.messages.kwargs["messages"]
    assert sent[1]["content"][0] == thinking
    assert sent[1]["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "read",
        "input": {"p": 1},
    }
    assert sent[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "data",
        "is_error": False,
    }


def test_unknown_stop_reason_is_mapped_to_other() -> None:
    client = FakeClient(api_message([{"type": "text", "text": "x"}], stop="pause_turn"))
    assert run(AnthropicLLM(client).complete(request())).stop_reason == "other"


def test_api_errors_become_llm_errors() -> None:
    boom = anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]
    with pytest.raises(LLMError):
        run(AnthropicLLM(FakeClient(boom)).complete(request()))


# ---- scripted helpers --------------------------------------------


def test_scripted_llm_runs_out_loudly() -> None:
    llm = ScriptedLLM([text_response("only")])
    run(llm.complete(request()))
    with pytest.raises(LLMError):
        run(llm.complete(request()))


def test_tool_response_helper_builds_tool_use_blocks() -> None:
    r = tool_response(("a", "read", {"path": "x"}), ("b", "list", {}), text="looking")
    assert r.stop_reason == "tool_use" and [t.name for t in r.tool_uses] == ["read", "list"]
