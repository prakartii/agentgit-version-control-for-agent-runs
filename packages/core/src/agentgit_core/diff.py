"""Compare two runs.

Steps are content-addressed, so "same step" is just "same id": no payload
needs to be read to know that two histories agree. A fork shares its parent's
prefix and the comparison starts at the first step that differs.

The differing region is aligned with ``difflib.SequenceMatcher`` over step
ids (a longest-common-subsequence style alignment, like text diff). Inside an
aligned "replace" pair, the step's fields are compared and the JSON payloads
are compared path by path, so the output says *what* changed, e.g.
``result.content[0].text``.
"""

import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

from agentgit_core.objects import Step
from agentgit_core.repo import Repository

Op = Literal["equal", "replace", "delete", "insert"]
_PLAIN_FIELDS = ("method", "tool_name", "outcome", "origin")
_BLOB_FIELDS = ("arguments", "result", "events")


@dataclass(frozen=True)
class Change:
    """One difference: a dotted path into the step, and the two values."""

    path: str
    before: Any
    after: Any


@dataclass(frozen=True)
class StepDiff:
    op: Op
    a_index: int | None  # position in run A, None for an insert
    b_index: int | None  # position in run B, None for a delete
    a: Step | None = None
    b: Step | None = None
    changes: tuple[Change, ...] = ()


@dataclass(frozen=True)
class RunDiff:
    a_id: str
    b_id: str
    common_prefix: int  # leading steps that are the same objects in both runs
    steps: tuple[StepDiff, ...] = field(default_factory=tuple)

    @property
    def identical(self) -> bool:
        return all(s.op == "equal" for s in self.steps)

    @property
    def first_divergence(self) -> int | None:
        """Position of the first differing step (in A), or None if identical."""
        for s in self.steps:
            if s.op != "equal":
                return s.a_index if s.a_index is not None else s.b_index
        return None


def json_changes(before: Any, after: Any, path: str) -> list[Change]:
    """Path-level differences between two JSON values."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[Change] = []
        for key in sorted(before.keys() | after.keys()):
            child = f"{path}.{key}"
            if key not in after:
                out.append(Change(child, before[key], None))
            elif key not in before:
                out.append(Change(child, None, after[key]))
            else:
                out.extend(json_changes(before[key], after[key], child))
        return out
    if isinstance(before, list) and isinstance(after, list):
        out = []
        for i in range(max(len(before), len(after))):
            child = f"{path}[{i}]"
            if i >= len(after):
                out.append(Change(child, before[i], None))
            elif i >= len(before):
                out.append(Change(child, None, after[i]))
            else:
                out.extend(json_changes(before[i], after[i], child))
        return out
    return [] if before == after and type(before) is type(after) else [Change(path, before, after)]


def _payload(repo: Repository, blob: str | None) -> Any:
    return None if blob is None else json.loads(repo.get_blob(blob))


def step_changes(repo: Repository, a: Step, b: Step) -> tuple[Change, ...]:
    changes = [
        Change(name, getattr(a, name), getattr(b, name))
        for name in _PLAIN_FIELDS
        if getattr(a, name) != getattr(b, name)
    ]
    for name in _BLOB_FIELDS:
        ida, idb = getattr(a, name), getattr(b, name)
        if ida != idb:  # equal ids mean equal content: skip reading the blobs
            changes.extend(json_changes(_payload(repo, ida), _payload(repo, idb), name))
    return tuple(changes)


def diff_runs(repo: Repository, a_id: str, b_id: str) -> RunDiff:
    a, b = repo.get_run(a_id), repo.get_run(b_id)
    prefix = 0
    while prefix < min(len(a.steps), len(b.steps)) and a.steps[prefix] == b.steps[prefix]:
        prefix += 1

    out = [StepDiff("equal", i, i) for i in range(prefix)]
    matcher = SequenceMatcher(None, a.steps[prefix:], b.steps[prefix:], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        a_pos, b_pos = range(prefix + i1, prefix + i2), range(prefix + j1, prefix + j2)
        if tag == "equal":
            out.extend(StepDiff("equal", i, j) for i, j in zip(a_pos, b_pos, strict=True))
            continue
        # Pair up the overlapping part of a replace block; the rest is a pure
        # delete or insert.
        paired = min(len(a_pos), len(b_pos)) if tag == "replace" else 0
        for i, j in zip(a_pos[:paired], b_pos[:paired], strict=True):
            sa, sb = repo.get_step(a.steps[i]), repo.get_step(b.steps[j])
            out.append(StepDiff("replace", i, j, sa, sb, step_changes(repo, sa, sb)))
        out.extend(StepDiff("delete", i, None, repo.get_step(a.steps[i])) for i in a_pos[paired:])
        out.extend(
            StepDiff("insert", None, j, None, repo.get_step(b.steps[j])) for j in b_pos[paired:]
        )
    return RunDiff(a_id, b_id, prefix, tuple(out))
