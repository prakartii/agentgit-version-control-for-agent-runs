"""Content-addressed object store.

Stores raw bytes under ``hash_object(type, payload)``. Writing the same
content twice is a no-op (deduplication). Backends only need put/get/has.
"""

import os
import tempfile
from pathlib import Path
from typing import Protocol

from agentgit_core.hashing import hash_object


class ObjectNotFoundError(KeyError):
    """Raised when an object id is not in the store."""


class ObjectStore(Protocol):
    def put(self, object_type: str, payload: bytes) -> str: ...
    def get(self, object_type: str, object_id: str) -> bytes: ...
    def has(self, object_type: str, object_id: str) -> bool: ...


class MemoryStore:
    """Dict-backed store for tests."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}

    def put(self, object_type: str, payload: bytes) -> str:
        object_id = hash_object(object_type, payload)
        self._objects.setdefault((object_type, object_id), payload)
        return object_id

    def get(self, object_type: str, object_id: str) -> bytes:
        try:
            return self._objects[(object_type, object_id)]
        except KeyError:
            raise ObjectNotFoundError(object_id) from None

    def has(self, object_type: str, object_id: str) -> bool:
        return (object_type, object_id) in self._objects


class FilesystemStore:
    """Stores objects as files at ``<root>/<type>/<id[:2]>/<id[2:]>``.

    The two-character shard keeps directories small, as git does.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, object_type: str, object_id: str) -> Path:
        return self._root / object_type / object_id[:2] / object_id[2:]

    def put(self, object_type: str, payload: bytes) -> str:
        object_id = hash_object(object_type, payload)
        path = self._path(object_type, object_id)
        if path.exists():
            return object_id  # dedup: same content already stored
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename, so a crash never leaves a
        # half-written object under its final name.
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return object_id

    def get(self, object_type: str, object_id: str) -> bytes:
        try:
            return self._path(object_type, object_id).read_bytes()
        except FileNotFoundError:
            raise ObjectNotFoundError(object_id) from None

    def has(self, object_type: str, object_id: str) -> bool:
        return self._path(object_type, object_id).exists()
