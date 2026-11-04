from sqlalchemy.engine import Engine

from agentgit_core.objects import Run, Step
from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore
from agentgit_index.service import IndexService


def build_run(repo: Repository, tool: str = "read", parent: str | None = None) -> str:
    result = repo.put_blob(tool.encode())
    step = repo.put_step(Step(index=0, kind="tool_call", tool_name=tool, result=result))
    if parent is None:
        return repo.put_run(Run(steps=(step,), metadata={"agent": "demo"}))
    return repo.put_run(Run(steps=(step,), parents=(parent,), fork_step=0))


def test_index_and_query_run(engine: Engine) -> None:
    repo = Repository(MemoryStore())
    index = IndexService(engine, repo)
    run_id = build_run(repo)
    index.index_run(run_id)
    summary = index.get_run(run_id)
    assert summary is not None
    assert summary.step_count == 1
    assert summary.metadata == {"agent": "demo"}


def test_indexing_twice_is_idempotent(engine: Engine) -> None:
    repo = Repository(MemoryStore())
    index = IndexService(engine, repo)
    run_id = build_run(repo)
    index.index_run(run_id)
    index.index_run(run_id)
    assert len(index.list_runs()) == 1


def test_fork_indexes_parent_and_edge(engine: Engine) -> None:
    repo = Repository(MemoryStore())
    index = IndexService(engine, repo)
    parent = build_run(repo, "read")
    child = build_run(repo, "write", parent=parent)
    index.index_run(child)
    summary = index.get_run(child)
    assert summary is not None and summary.parents == (parent,)
    assert index.get_run(parent) is not None


def test_ref_can_be_created_and_moved(engine: Engine) -> None:
    repo = Repository(MemoryStore())
    index = IndexService(engine, repo)
    first = build_run(repo, "read")
    second = build_run(repo, "write")
    index.set_ref("main", first)
    assert index.resolve_ref("main") == first
    index.set_ref("main", second)
    assert index.resolve_ref("main") == second
    assert index.resolve_ref("missing") is None
