import copy
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentgit_proxy.redaction import Redactor

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)


@pytest.mark.parametrize(
    ("secret", "label"),
    [
        ("sk-ant-abcdefghijklmnop1234", "api_key"),
        ("AKIAABCDEFGHIJKLMNOP", "aws_access_key"),
        ("ghp_" + "a" * 36, "github_token"),
        ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4", "jwt"),
        ("Authorization: Bearer abcdefghijklmnop1234", "bearer_token"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----", "private_key"),
    ],
)
def test_known_credential_formats_are_replaced(secret: str, label: str) -> None:
    out = Redactor().redact({"text": f"here it is: {secret} ok"})
    assert f"[REDACTED:{label}]" in out["text"]
    assert secret not in out["text"]


def test_sensitive_key_names_redact_the_whole_value() -> None:
    out = Redactor().redact({"arguments": {"Password": "hunter2", "user": "ann", "token": "t"}})
    assert out == {
        "arguments": {"Password": "[REDACTED:password]", "user": "ann", "token": "[REDACTED:token]"}
    }


def test_empty_sensitive_values_are_left_alone() -> None:
    r = Redactor()
    assert r.redact({"token": None, "password": ""}) == {"token": None, "password": ""}
    assert r.replacements == 0


def test_input_is_not_mutated() -> None:
    original = {"a": [{"password": "x"}, "sk-abcdefghijklmnop1234"]}
    snapshot = copy.deepcopy(original)
    Redactor().redact(original)
    assert original == snapshot


def test_replacements_are_counted() -> None:
    r = Redactor()
    r.redact({"password": "a", "t": "sk-abcdefghijklmnop1234 and sk-zyxwvutsrqponmlk9876"})
    assert r.replacements == 3


def test_ordinary_text_and_numbers_pass_through() -> None:
    value = {"path": "/tmp/a.txt", "n": 3, "ok": True, "list": [1, "two", None]}
    r = Redactor()
    assert r.redact(value) == value
    assert r.replacements == 0


def test_custom_hook_is_applied_and_counted() -> None:
    r = Redactor(hooks=[lambda s: s.replace("ACME-42", "[CUSTOM]")])
    assert r.redact(["id ACME-42"]) == ["id [CUSTOM]"]
    assert r.replacements == 1


@given(json_values)
def test_redaction_is_idempotent(value: Any) -> None:
    once = Redactor().redact(value)
    assert Redactor().redact(once) == once


@given(json_values)
def test_redaction_without_secrets_keeps_structure(value: Any) -> None:
    out = Redactor(rules=[], sensitive_keys=[]).redact(value)
    assert out == value
