import json

import pytest

from agentgit_core.fork import ForkError, fork_run, parse_json_value
from agentgit_core.hashing import canonical_json
from agentgit_core.objects import Run, Step, StepTiming
from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore


def build(repo: Repository, results: list[object], method: str = "tools/call") -> str:
    steps = []
    for i, result in enumerate(results):
        step = Step(
            method=method,
            tool_name=f"t{i}",
            arguments=repo.put_blob(canonical_json({"i": i})),
            result=repo.put_blob(canonical_json(result)),
        )
        steps.append(repo.put_step(step))
    timings = tuple(StepTiming(started_at_ms=i, duration_ms=1) for i in range(len(steps)))
    return repo.put_run(Run(steps=tuple(steps), timings=timings, metadata={"redaction": "off"}))


def result_of(repo: Repository, step_id: str) -> object:
    step = repo.get_step(step_id)
    assert step.result is not None
    return json.loads(repo.get_blob(step.result))


def test_fork_shares_the_prefix_and_replaces_one_result() -> None:
    repo = Repository(MemoryStore())
    source = build(repo, ["a", "b", "c"])
    fork_id = fork_run(repo, source, 1, new_result={"x": 1}, edit=True)

    fork, src = repo.get_run(fork_id), repo.get_run(source)
    assert fork.parents == (source,) and fork.fork_step == 1
    assert fork.steps[0] == src.steps[0]  # same object, not a copy
    assert len(fork.steps) == 2  # everything after the edit is dropped
    assert result_of(repo, fork.steps[1]) == {"x": 1}
    assert repo.get_step(fork.steps[1]).arguments == repo.get_step(src.steps[1]).arguments
    assert len(fork.timings) == 2
    repo.verify_run(fork_id)


def test_fork_without_edit_truncates() -> None:
    repo = Repository(MemoryStore())
    source = build(repo, ["a", "b", "c"])
    fork = repo.get_run(fork_run(repo, source, 2))
    assert fork.steps == repo.get_run(source).steps[:2]
    assert fork.metadata["fork_edit"] == "truncate@2"


def test_fork_carries_source_metadata_and_marks_origin() -> None:
    repo = Repository(MemoryStore())
    fork = repo.get_run(fork_run(repo, build(repo, ["a"]), 0))
    assert fork.metadata["redaction"] == "off" and fork.metadata["origin"] == "fork"


def test_editing_a_result_to_an_error_changes_the_outcome() -> None:
    repo = Repository(MemoryStore())
    source = build(repo, ["a"])
    edited = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
    fork = repo.get_run(fork_run(repo, source, 0, new_result=edited, edit=True))
    assert repo.get_step(fork.steps[0]).outcome == "tool_error"


def test_forking_is_deterministic() -> None:
    repo = Repository(MemoryStore())
    source = build(repo, ["a", "b"])
    one = fork_run(repo, source, 1, new_result="z", edit=True)
    two = fork_run(repo, source, 1, new_result="z", edit=True)
    assert one == two


@pytest.mark.parametrize("at", [-1, 4])
def test_out_of_range_positions_are_rejected(at: int) -> None:
    repo = Repository(MemoryStore())
    with pytest.raises(ForkError, match="out of range"):
        fork_run(repo, build(repo, ["a", "b", "c"]), at)


def test_cannot_edit_past_the_end_or_a_non_tool_call() -> None:
    repo = Repository(MemoryStore())
    with pytest.raises(ForkError, match="no step at that position"):
        fork_run(repo, build(repo, ["a"]), 1, new_result="x", edit=True)
    with pytest.raises(ForkError, match="only tools/call"):
        fork_run(repo, build(repo, ["a"], method="tools/list"), 0, new_result="x", edit=True)


def test_parse_json_value() -> None:
    assert parse_json_value('{"a": [1]}') == {"a": [1]}
    assert parse_json_value("null") is None
    with pytest.raises(ForkError, match="not valid JSON"):
        parse_json_value("{oops")
