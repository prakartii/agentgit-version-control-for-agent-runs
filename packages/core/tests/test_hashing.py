import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentgit_core.hashing import canonical_json, hash_object

# JSON-safe values: no NaN/inf, string keys only.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text()
    | st.floats(allow_nan=False, allow_infinity=False),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=15,
)


def test_known_vector_matches_independent_computation() -> None:
    # sha256(b"blob 5\0hello"), computed outside this module.
    assert (
        hash_object("blob", b"hello")
        == "8aec4e4876f854f688d0ebfc8f37598f38e5fd6903cccc850ca36591175aeb60"
    )


def test_type_prefix_separates_ids() -> None:
    assert hash_object("blob", b"x") != hash_object("step", b"x")


def test_key_order_does_not_change_json() -> None:
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_nan_is_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


@given(json_values)
def test_hash_is_stable(value: object) -> None:
    first = hash_object("step", canonical_json(value))
    second = hash_object("step", canonical_json(value))
    assert first == second and len(first) == 64
