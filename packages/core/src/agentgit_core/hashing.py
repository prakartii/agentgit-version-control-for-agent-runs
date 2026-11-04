"""Canonical serialization and content hashing.

An object id is SHA-256 over ``b"<type> <length>\0" + payload`` (git-style
framing). Structured data is first turned into canonical JSON so the same
logical value always produces the same bytes.
"""

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` to deterministic JSON bytes.

    Keys are sorted, no whitespace is emitted, and NaN/Infinity are rejected
    because they are not valid JSON and would break cross-language readers.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def hash_object(object_type: str, payload: bytes) -> str:
    """Return the hex SHA-256 id of an object of the given type."""
    header = f"{object_type} {len(payload)}".encode() + b"\0"
    return hashlib.sha256(header + payload).hexdigest()
