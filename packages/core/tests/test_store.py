from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentgit_core.hashing import hash_object
from agentgit_core.store import FilesystemStore, MemoryStore, ObjectNotFoundError, ObjectStore


@pytest.fixture(params=["memory", "fs"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStore:
    return MemoryStore() if request.param == "memory" else FilesystemStore(tmp_path)


def test_put_then_get_roundtrips(store: ObjectStore) -> None:
    object_id = store.put("blob", b"hello")
    assert object_id == hash_object("blob", b"hello")
    assert store.get("blob", object_id) == b"hello"


def test_put_is_idempotent(store: ObjectStore) -> None:
    assert store.put("blob", b"x") == store.put("blob", b"x")


def test_missing_object_raises(store: ObjectStore) -> None:
    with pytest.raises(ObjectNotFoundError):
        store.get("blob", "0" * 64)
    assert not store.has("blob", "0" * 64)


def test_same_bytes_different_type_are_separate(store: ObjectStore) -> None:
    blob = store.put("blob", b"x")
    step = store.put("step", b"x")
    assert blob != step


def test_filesystem_dedup_writes_one_file(tmp_path: Path) -> None:
    fs = FilesystemStore(tmp_path)
    fs.put("blob", b"same")
    fs.put("blob", b"same")
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1


@given(st.binary(max_size=200))
def test_roundtrip_property(payload: bytes) -> None:
    mem = MemoryStore()
    assert mem.get("blob", mem.put("blob", payload)) == payload
