from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from agentgit_core.diff import Change, diff_runs, json_changes
from agentgit_core.fork import fork_run
from agentgit_core.hashing import canonical_json
from agentgit_core.objects import Run, Step
from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore


def build(repo: Repository, results: list[str], tools: list[str] | None = None) -> str:
    steps = []
    for i, text in enumerate(results):
        name = tools[i] if tools else f"t{i}"
        step = Step(
            method="tools/call",
            tool_name=name,
            arguments=repo.put_blob(canonical_json({"n": name})),
            result=repo.put_blob(canonical_json({"content": [{"type": "text", "text": text}]})),
        )
        steps.append(repo.put_step(step))
    return repo.put_run(Run(steps=tuple(steps)))


def ops(repo: Repository, a: str, b: str) -> list[str]:
    return [s.op for s in diff_runs(repo, a, b).steps]


def test_identical_runs() -> None:
    repo = Repository(MemoryStore())
    a = build(repo, ["x", "y"])
    d = diff_runs(repo, a, a)
    assert d.identical and d.common_prefix == 2 and d.first_divergence is None


def test_a_fork_diff_starts_at_the_edited_step_and_names_the_path() -> None:
    repo = Repository(MemoryStore())
    a = build(repo, ["one", "two", "three"])
    edited: dict[str, Any] = {"content": [{"type": "text", "text": "CHANGED"}]}
    b = fork_run(repo, a, 1, new_result=edited, edit=True)

    d = diff_runs(repo, a, b)
    assert d.common_prefix == 1 and d.first_divergence == 1 and not d.identical
    assert ops(repo, a, b) == ["equal", "replace", "delete"]
    replaced = d.steps[1]
    assert replaced.changes == (Change("result.content[0].text", "two", "CHANGED"),)
    assert d.steps[2].a is not None and d.steps[2].a.tool_name == "t2"  # dropped by the fork


def test_inserted_and_deleted_steps_are_aligned_not_cascaded() -> None:
    repo = Repository(MemoryStore())
    a = build(repo, ["1", "2", "3"], ["a", "b", "c"])
    b = build(repo, ["1", "9", "2", "3"], ["a", "x", "b", "c"])  # one step inserted in the middle
    assert ops(repo, a, b) == ["equal", "insert", "equal", "equal"]


def test_changed_fields_are_reported() -> None:
    repo = Repository(MemoryStore())
    a = build(repo, ["same"], ["read"])
    b = build(repo, ["same"], ["write"])
    (step,) = diff_runs(repo, a, b).steps
    assert step.op == "replace"
    assert Change("tool_name", "read", "write") in step.changes


def test_json_changes_covers_missing_keys_lists_and_types() -> None:
    assert json_changes({"a": 1, "b": 2}, {"a": 1, "c": 3}, "r") == [
        Change("r.b", 2, None),
        Change("r.c", None, 3),
    ]
    assert json_changes([1, 2], [1], "r") == [Change("r[1]", 2, None)]
    assert json_changes(1, True, "r") == [Change("r", 1, True)]  # 1 and True are not the same value
    assert json_changes({"a": [1]}, {"a": [1]}, "r") == []


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=5),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=3), children, max_size=3)
    ),
    max_leaves=8,
)


@given(json_values)
def test_a_value_has_no_changes_against_itself(value: Any) -> None:
    assert json_changes(value, value, "r") == []


@given(json_values, json_values)
def test_changes_are_empty_exactly_when_the_values_are_equal(a: Any, b: Any) -> None:
    same = canonical_json(a) == canonical_json(b)
    assert (json_changes(a, b, "r") == []) == same
