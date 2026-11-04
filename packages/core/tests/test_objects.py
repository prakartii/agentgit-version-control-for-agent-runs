import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agentgit_core.objects import Run, Step, blob_id, encode_run, encode_step


def test_blob_id_is_content_addressed() -> None:
    assert blob_id(b"a") == blob_id(b"a")
    assert blob_id(b"a") != blob_id(b"b")


def test_equal_steps_have_equal_ids() -> None:
    a = Step(method="tools/call", tool_name="read", arguments="x")
    b = Step(method="tools/call", tool_name="read", arguments="x")
    assert encode_step(a)[0] == encode_step(b)[0]


def test_changing_a_field_changes_the_id() -> None:
    a = Step(method="tools/call", tool_name="read")
    b = Step(method="tools/call", tool_name="write")
    assert encode_step(a)[0] != encode_step(b)[0]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Step.model_validate({"method": "tools/call", "bogus": 1})


def test_metadata_key_order_does_not_change_run_id() -> None:
    a = Run(metadata={"a": "1", "b": "2"})
    b = Run(metadata={"b": "2", "a": "1"})
    assert encode_run(a)[0] == encode_run(b)[0]


@given(st.lists(st.text(min_size=64, max_size=64), max_size=5))
def test_run_id_depends_on_step_order(ids: list[str]) -> None:
    forward = encode_run(Run(steps=tuple(ids)))[0]
    backward = encode_run(Run(steps=tuple(reversed(ids))))[0]
    assert (forward == backward) == (ids == list(reversed(ids)))


def test_timings_must_align_with_steps() -> None:
    from agentgit_core.objects import StepTiming

    timing = StepTiming(started_at_ms=1, duration_ms=2)
    Run(steps=("a" * 64,), timings=(timing,))  # aligned: ok
    Run(steps=("a" * 64,))  # timings omitted: ok
    with pytest.raises(ValidationError):
        Run(steps=("a" * 64, "b" * 64), timings=(timing,))


def test_timings_do_not_change_step_ids() -> None:
    # The point of ADR-003: identical content gives identical step ids no
    # matter when it was recorded, so forks can share their prefix.
    step = Step(method="tools/call", tool_name="read", arguments="x")
    assert encode_step(step)[0] == encode_step(step.model_copy())[0]


def test_completeness_flags_are_part_of_the_run_id() -> None:
    complete = encode_run(Run())[0]
    assert encode_run(Run(status="aborted"))[0] != complete
    assert encode_run(Run(redacted=True))[0] != complete
    assert encode_run(Run(dropped_steps=1))[0] != complete


def test_outcome_is_part_of_the_step_id() -> None:
    ok = Step(method="tools/call", tool_name="t")
    failed = Step(method="tools/call", tool_name="t", outcome="no_response")
    assert encode_step(ok)[0] != encode_step(failed)[0]
