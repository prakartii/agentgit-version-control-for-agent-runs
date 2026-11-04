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


def test_resolve_ref_full_id_and_prefix(engine: Engine) -> None:
    repo = Repository(MemoryStore())
    index = IndexService(engine, repo)
    run_id = build_run(repo)
    index.set_ref("main", run_id)
    assert index.resolve("main") == run_id
    assert index.resolve(run_id) == run_id
    assert index.resolve(run_id[:8]) == run_id
    assert index.resolve("zzzz") is None


def test_wildcards_in_input_are_not_patterns(engine: Engine) -> None:
    repo = Repository(MemoryStore())
    index = IndexService(engine, repo)
    index.index_run(build_run(repo))
    assert index.resolve("%") is None
    assert index.resolve("_") is None


def test_index_stores_outcome_timings_and_completeness(engine: Engine) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from agentgit_core.objects import StepTiming
    from agentgit_index.models import RunStepRow, StepRow

    repo = Repository(MemoryStore())
    index = IndexService(engine, repo)
    args = repo.put_blob(b"{}")
    step = repo.put_step(
        Step(method="tools/call", tool_name="read", arguments=args, outcome="no_response")
    )
    run_id = repo.put_run(
        Run(
            steps=(step,),
            timings=(StepTiming(started_at_ms=1000, duration_ms=25),),
            status="aborted",
            redacted=True,
            dropped_steps=2,
        )
    )
    index.index_run(run_id)

    summary = index.get_run(run_id)
    assert summary is not None
    assert (summary.status, summary.redacted, summary.dropped_steps) == ("aborted", True, 2)
    with Session(engine) as session:
        row = session.get(StepRow, step)
        assert row is not None and (row.method, row.outcome) == ("tools/call", "no_response")
        timing = session.scalars(select(RunStepRow).where(RunStepRow.run_id == run_id)).one()
        assert (timing.started_at_ms, timing.duration_ms) == (1000, 25)
