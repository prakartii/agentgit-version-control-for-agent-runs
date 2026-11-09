from typing import Any

import pytest
from pydantic import ValidationError

from agentgit_replay.payloads import CANARY
from agentgit_replay.policy import Policy, evaluate


def report(calls: list[tuple[str, dict[str, Any]]], fired_at: list[int]) -> dict[str, Any]:
    return {
        "observed_calls": [{"tool": t, "arguments": a} for t, a in calls],
        "chaos_applied": [{"tool": "x", "fault": "inject", "call_index": i} for i in fired_at],
    }


CALLS: list[tuple[str, dict[str, Any]]] = [
    ("read", {"p": 1}),
    ("write", {"p": "a"}),
    ("write", {"p": CANARY}),
]


def test_only_calls_after_the_first_fault_are_judged() -> None:
    policy = Policy(forbid_tools=["write"])
    # injection hit call 0: both later writes are violations
    assert [v.call_index for v in evaluate(report(CALLS, [0]), policy)] == [1, 2]
    # injection hit call 1: only the last write is after it
    assert [v.call_index for v in evaluate(report(CALLS, [1]), policy)] == [2]


def test_without_chaos_nothing_is_blamed_on_tampering() -> None:
    assert evaluate(report(CALLS, []), Policy(forbid_tools=["write"])) == []
    assert evaluate(report(CALLS, []), Policy(forbid_tools=["write"], only_after_chaos=False))


def test_canary_and_argument_patterns() -> None:
    policy = Policy(forbid_canary=True, forbid_argument_patterns=[r"\"p\": \"a\""])
    found = {(v.call_index, v.rule) for v in evaluate(report(CALLS, [0]), policy)}
    assert (1, 'arguments match /\\"p\\": \\"a\\"/') in found
    assert any(i == 2 and "canary" in rule for i, rule in found)


def test_tool_patterns_use_globs() -> None:
    assert evaluate(report(CALLS, [0]), Policy(forbid_tools=["wr*"]))
    assert not evaluate(report(CALLS, [0]), Policy(forbid_tools=["delete_*"]))


def test_invalid_policies_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Policy(forbid_argument_patterns=["(unclosed"])
    with pytest.raises(ValidationError):
        Policy.model_validate({"forbid_tool": ["x"]})  # typo: unknown key


def test_empty_policy() -> None:
    assert Policy().is_empty and not Policy(forbid_canary=True).is_empty
