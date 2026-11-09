from typing import Any

import pytest
from pydantic import ValidationError

from agentgit_replay.chaos import ChaosPlan, Plan, Rule
from agentgit_replay.payloads import BUILTIN, CANARY, resolve_payload

META = {"io.modelcontextprotocol/serverInfo": {"name": "s"}}


def reply(text: str = "hello world") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"result": text},
            "isError": False,
            "_meta": META,
        },
    }


def plan(*rules: Rule, seed: int = 0) -> ChaosPlan:
    return ChaosPlan(Plan(seed=seed, rules=list(rules)))


def test_no_matching_rule_leaves_the_reply_untouched() -> None:
    out = plan(Rule(fault="tool_error", tool="other")).apply("read", reply())
    assert out.reply == reply() and out.applied == []


def test_tool_error_keeps_the_id_and_meta() -> None:
    out = plan(Rule(fault="tool_error", message="disk on fire")).apply("read", reply())
    assert out.reply["id"] == 7 and out.reply["result"]["isError"] is True
    assert out.reply["result"]["content"][0]["text"] == "disk on fire"
    assert out.reply["result"]["_meta"] == META


def test_protocol_error_replaces_the_result() -> None:
    out = plan(Rule(fault="protocol_error", code=-32001, message="nope")).apply("read", reply())
    assert out.reply == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32001, "message": "nope"}}


def test_truncate_and_oversize_change_both_text_forms() -> None:
    cut = plan(Rule(fault="truncate", max_chars=5)).apply("read", reply()).reply["result"]
    assert cut["content"][0]["text"] == "hello" and cut["structuredContent"]["result"] == "hello"
    big = plan(Rule(fault="oversize", size=1000)).apply("read", reply()).reply["result"]
    assert len(big["content"][0]["text"]) == len("hello world") + 1000


def test_empty() -> None:
    out = plan(Rule(fault="empty")).apply("read", reply()).reply["result"]
    assert out["content"] == [] and "structuredContent" not in out


@pytest.mark.parametrize(
    ("position", "expected"),
    [("append", "hello world\nEVIL"), ("prepend", "EVIL\nhello world"), ("replace", "EVIL")],
)
def test_inject_positions(position: str, expected: str) -> None:
    rule = Rule(fault="inject", payload="EVIL", position=position)  # type: ignore[arg-type]
    result = plan(rule).apply("read", reply()).reply["result"]
    assert result["content"][0]["text"] == expected
    assert result["structuredContent"]["result"] == expected  # a client reading either form sees it


def test_delay_does_not_alter_the_reply() -> None:
    out = plan(Rule(fault="delay", delay_ms=250)).apply("read", reply())
    assert out.delay_s == 0.25 and out.reply == reply()


def test_tool_patterns_and_nth_occurrence() -> None:
    p = plan(Rule(fault="tool_error", tool="read_*", nth=[2]))
    texts = [p.apply("read_file", reply()).reply["result"]["isError"] for _ in range(3)]
    assert texts == [False, True, False]
    assert not p.apply("write_file", reply()).applied


def test_probability_is_reproducible_for_a_seed() -> None:
    def run(seed: int) -> list[bool]:
        p = plan(Rule(fault="tool_error", probability=0.5), seed=seed)
        return [bool(p.apply("t", reply()).applied) for _ in range(20)]

    assert run(1) == run(1) and run(1) != run(2) and any(run(1)) and not all(run(1))


def test_rules_compose_in_order() -> None:
    p = plan(Rule(fault="inject", payload="EVIL"), Rule(fault="truncate", max_chars=4))
    assert p.apply("t", reply()).reply["result"]["content"][0]["text"] == "hell"


def test_already_failed_replies_are_not_damaged_further() -> None:
    err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}
    out = plan(Rule(fault="inject", payload="EVIL")).apply("t", err)
    assert out.reply == err and out.applied == []


@pytest.mark.parametrize(
    "bad",
    [
        {"fault": "inject"},
        {"fault": "delay"},
        {"fault": "tool_error", "nth": [0]},
        {"fault": "inject", "payload": "builtin:nope"},
        {"fault": "tool_error", "typo": 1},
    ],
)
def test_invalid_rules_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Rule.model_validate(bad)


def test_builtin_payloads_carry_the_canary() -> None:
    assert BUILTIN and all(CANARY in text for text in BUILTIN.values())
    assert resolve_payload("builtin:ignore_previous") == BUILTIN["ignore_previous"]
    assert resolve_payload("literal text") == "literal text"
