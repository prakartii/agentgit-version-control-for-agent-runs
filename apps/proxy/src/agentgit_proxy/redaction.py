"""Secret redaction for the *recorded copy* of traffic.

Redaction runs only on the path that writes to the object store. Forwarded
bytes are never passed through here, so redaction cannot change what the agent
or the tool server sees.

This is best-effort pattern matching, not a guarantee. It catches common
credential formats and values stored under sensitive key names. Anything else
(a password in free text, a secret in an unusual format) is recorded as-is.
Treat the object store as sensitive data.
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


def _rule(name: str, regex: str) -> Rule:
    return Rule(name, re.compile(regex))


DEFAULT_RULES: tuple[Rule, ...] = (
    _rule(
        "private_key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
    ),
    _rule("api_key", r"\bsk-[A-Za-z0-9_-]{16,}"),
    _rule("aws_access_key", r"\bAKIA[0-9A-Z]{16}\b"),
    _rule("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    _rule("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    _rule("bearer_token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)

# Values stored under these key names are replaced whole, whatever they look like.
DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "authorization",
        "private_key",
    }
)

# A custom hook receives every string value and returns it, possibly modified.
Hook = Callable[[str], str]


class Redactor:
    """Returns redacted copies of JSON-like values and counts what it changed."""

    def __init__(
        self,
        rules: Sequence[Rule] = DEFAULT_RULES,
        sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
        hooks: Sequence[Hook] = (),
    ) -> None:
        self._rules = tuple(rules)
        self._keys = frozenset(k.lower() for k in sensitive_keys)
        self._hooks = tuple(hooks)
        self.replacements = 0  # total changes made over this redactor's lifetime

    def redact(self, value: Any) -> Any:
        """Return a redacted deep copy of ``value``. The input is not modified."""
        if isinstance(value, dict):
            return {k: self._redact_member(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            return self._redact_string(value)
        return value

    def _redact_member(self, key: Any, value: Any) -> Any:
        if isinstance(key, str) and key.lower() in self._keys and value not in (None, "", False):
            self.replacements += 1
            return f"[REDACTED:{key.lower()}]"
        return self.redact(value)

    def _redact_string(self, text: str) -> str:
        result = text
        for rule in self._rules:
            result, count = rule.pattern.subn(f"[REDACTED:{rule.name}]", result)
            self.replacements += count
        for hook in self._hooks:
            hooked = hook(result)
            if hooked != result:
                self.replacements += 1
                result = hooked
        return result
