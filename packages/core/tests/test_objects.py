import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agentgit_core.objects import Run, Step, blob_id, encode_run, encode_step


def test_blob_id_is_content_addressed() -> None:
    assert blob_id(b"a") == blob_id(b"a")
    assert blob_id(b"a") != blob_id(b"b")


def test_equal_steps_have_equal_ids() -> None:
    a = Step(index=0, kind="tool_call", tool_name="read", arguments="x")
    b = Step(index=0, kind="tool_call", tool_name="read", arguments="x")
    assert encode_step(a)[0] == encode_step(b)[0]


def test_changing_a_field_changes_the_id() -> None:
    a = Step(index=0, kind="tool_call", tool_name="read")
    b = Step(index=0, kind="tool_call", tool_name="write")
    assert encode_step(a)[0] != encode_step(b)[0]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Step.model_validate({"index": 0, "kind": "tool_call", "bogus": 1})


def test_metadata_key_order_does_not_change_run_id() -> None:
    a = Run(metadata={"a": "1", "b": "2"})
    b = Run(metadata={"b": "2", "a": "1"})
    assert encode_run(a)[0] == encode_run(b)[0]


@given(st.lists(st.text(min_size=64, max_size=64), max_size=5))
def test_run_id_depends_on_step_order(ids: list[str]) -> None:
    forward = encode_run(Run(steps=tuple(ids)))[0]
    backward = encode_run(Run(steps=tuple(reversed(ids))))[0]
    assert (forward == backward) == (ids == list(reversed(ids)))
