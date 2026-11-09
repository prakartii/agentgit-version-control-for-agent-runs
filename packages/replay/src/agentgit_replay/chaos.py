"""Chaos: deliberately damage replayed tool results to test how an agent copes.

A plan is a list of rules. Each rule picks tool calls (by tool name, by
occurrence number, or with a seeded probability) and applies one fault to the
*result the agent is about to receive*:

* ``tool_error``, ``protocol_error``: the tool fails.
* ``empty``, ``truncate``, ``oversize``: the result is missing, cut short or huge.
* ``delay``: the reply arrives late (tests agent timeouts).
* ``inject``: attacker-controlled text is added to the result (prompt injection).

Everything is deterministic: the same plan and seed against the same recording
produce the same damage, so a failure can be reproduced. Faults only touch what
replay serves; nothing real is changed. Replay serves *recorded* results, so a
chaos run exercises the agent's handling of bad data, not the tool's.
"""

import random
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentgit_replay.payloads import resolve_payload

FaultName = Literal[
    "tool_error", "protocol_error", "empty", "truncate", "oversize", "inject", "delay"
]
Position = Literal["append", "prepend", "replace"]


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fault: FaultName
    tool: str = "*"  # fnmatch pattern on the tool name
    nth: list[int] = []  # 1-based occurrences among calls this rule matches; empty = all
    probability: float = Field(default=1.0, gt=0, le=1)
    message: str = "chaos: injected failure"  # tool_error / protocol_error
    code: int = -32000  # protocol_error
    max_chars: int = Field(default=20, ge=0)  # truncate
    size: int = Field(default=200_000, gt=0)  # oversize: characters added
    payload: str = ""  # inject: literal text, or "builtin:<name>"
    position: Position = "append"  # inject
    delay_ms: int = Field(default=0, ge=0)  # delay

    @model_validator(mode="after")
    def _required_fields(self) -> "Rule":
        if self.fault == "inject" and not self.payload:
            raise ValueError("inject needs a payload")
        if self.fault == "inject":
            resolve_payload(self.payload)  # fail at load time on an unknown builtin name
        if self.fault == "delay" and self.delay_ms <= 0:
            raise ValueError("delay needs delay_ms > 0")
        if any(n < 1 for n in self.nth):
            raise ValueError("nth is 1-based")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int = 0
    rules: list[Rule] = Field(default_factory=list)


@dataclass(frozen=True)
class Applied:
    """What chaos did to one reply; kept in the replay report."""

    tool_name: str
    fault: FaultName
    detail: str = ""
    call_index: int = -1  # position among the agent's tool calls; set by the engine


@dataclass
class Outcome:
    reply: dict[str, Any]
    delay_s: float = 0.0
    applied: list[Applied] = field(default_factory=list)


def _map_text(result: dict[str, Any], fn: Any) -> dict[str, Any]:
    """Apply ``fn`` to the text of a tool result, in both its content and structured forms.

    Clients may read either form, so both change together; otherwise an agent
    reading ``structuredContent`` would never see the injected text.
    """
    out = dict(result)
    content = []
    for item in result.get("content", []):
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ):
            item = {**item, "text": fn(item["text"])}
        content.append(item)
    out["content"] = content
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        out["structuredContent"] = {**structured, "result": fn(structured["result"])}
    return out


def _inject(rule: Rule) -> Any:
    payload = resolve_payload(rule.payload)

    def fn(text: str) -> str:
        if rule.position == "replace":
            return payload
        return f"{payload}\n{text}" if rule.position == "prepend" else f"{text}\n{payload}"

    return fn


class ChaosPlan:
    """A plan plus the state needed to apply it (occurrence counters, seeded RNG)."""

    def __init__(self, plan: Plan) -> None:
        self._plan = plan
        self._rng = random.Random(plan.seed)
        self._matches = [0] * len(plan.rules)

    def apply(self, tool_name: str | None, reply: dict[str, Any]) -> Outcome:
        outcome = Outcome(reply)
        name = tool_name or ""
        for i, rule in enumerate(self._plan.rules):
            if not fnmatchcase(name, rule.tool):
                continue
            self._matches[i] += 1
            if rule.nth and self._matches[i] not in rule.nth:
                continue
            # Draw even when probability is 1 so adding a rule never shifts another's draws.
            if self._rng.random() >= rule.probability:
                continue
            self._apply_one(rule, name, outcome)
        return outcome

    def _apply_one(self, rule: Rule, name: str, outcome: Outcome) -> None:
        reply = outcome.reply
        meta = {"_meta": reply["result"]["_meta"]} if "_meta" in reply.get("result", {}) else {}
        if rule.fault == "delay":
            outcome.delay_s += rule.delay_ms / 1000
        elif rule.fault == "protocol_error":
            outcome.reply = {
                "jsonrpc": "2.0",
                "id": reply["id"],
                "error": {"code": rule.code, "message": rule.message},
            }
        elif "result" not in reply:
            return  # nothing to damage: the recorded reply was already an error
        elif rule.fault == "tool_error":
            outcome.reply = {
                **reply,
                "result": {
                    "content": [{"type": "text", "text": rule.message}],
                    "isError": True,
                    **meta,
                },
            }
        elif rule.fault == "empty":
            outcome.reply = {**reply, "result": {"content": [], "isError": False, **meta}}
        else:
            if rule.fault == "truncate":

                def fn(s: str) -> str:
                    return s[: rule.max_chars]
            elif rule.fault == "oversize":

                def fn(s: str) -> str:
                    return s + "A" * rule.size
            else:  # inject
                fn = _inject(rule)
            outcome.reply = {**reply, "result": _map_text(reply["result"], fn)}
        outcome.applied.append(
            Applied(name, rule.fault, rule.payload[:60] if rule.fault == "inject" else "")
        )


def load_plan(path: Path) -> Plan:
    """Read a chaos plan from TOML (``seed = 1`` and one ``[[rules]]`` table per rule)."""
    try:
        return Plan.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from None
