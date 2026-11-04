import pytest

from agentgit_core.objects import Run, Step
from agentgit_core.repo import IntegrityError, Repository
from agentgit_core.store import MemoryStore, ObjectNotFoundError


def make_run(repo: Repository, result: bytes = b"ok") -> tuple[str, str]:
    args = repo.put_blob(b'{"path": "a.txt"}')
    res = repo.put_blob(result)
    step_id = repo.put_step(
        Step(index=0, kind="tool_call", tool_name="read", arguments=args, result=res)
    )
    return repo.put_run(Run(steps=(step_id,))), step_id


def test_roundtrip_run() -> None:
    repo = Repository(MemoryStore())
    run_id, step_id = make_run(repo)
    assert repo.get_run(run_id).steps == (step_id,)
    assert repo.get_step(step_id).tool_name == "read"
    repo.verify_run(run_id)


def test_identical_runs_dedup_to_one_id() -> None:
    repo = Repository(MemoryStore())
    assert make_run(repo)[0] == make_run(repo)[0]


def test_step_with_missing_blob_is_rejected() -> None:
    repo = Repository(MemoryStore())
    with pytest.raises(IntegrityError):
        repo.put_step(Step(index=0, kind="tool_call", result="f" * 64))


def test_run_with_missing_step_is_rejected() -> None:
    repo = Repository(MemoryStore())
    with pytest.raises(IntegrityError):
        repo.put_run(Run(steps=("f" * 64,)))


def test_fork_requires_fork_step() -> None:
    repo = Repository(MemoryStore())
    parent, _ = make_run(repo)
    with pytest.raises(IntegrityError):
        repo.put_run(Run(parents=(parent,)))
    child = repo.put_run(Run(parents=(parent,), fork_step=0))
    repo.verify_run(child)


def test_corrupt_blob_is_detected() -> None:
    store = MemoryStore()
    repo = Repository(store)
    blob = repo.put_blob(b"good")
    store._objects[("blob", blob)] = b"tampered"
    with pytest.raises(IntegrityError):
        repo.get_blob(blob)


def test_refs_point_at_existing_runs() -> None:
    repo = Repository(MemoryStore())
    run_id, _ = make_run(repo)
    repo.set_ref("main", run_id)
    assert repo.get_ref("main") == run_id
    with pytest.raises(ObjectNotFoundError):
        repo.set_ref("bad", "0" * 64)
