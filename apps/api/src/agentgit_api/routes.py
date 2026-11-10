"""Read-only query endpoints over the run index."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from agentgit_api.deps import get_index, get_repo
from agentgit_core.diff import diff_runs
from agentgit_core.objects import Step
from agentgit_core.repo import Repository
from agentgit_core.store import ObjectNotFoundError
from agentgit_index.service import IndexService

router = APIRouter()


class RunOut(BaseModel):
    id: str
    parents: list[str]
    step_count: int
    metadata: dict[str, str]
    status: str
    redacted: bool
    dropped_steps: int


class StepOut(BaseModel):
    position: int
    method: str | None
    tool_name: str | None
    outcome: str
    started_at_ms: int | None
    duration_ms: int | None


class RunDetail(RunOut):
    fork_step: int | None
    steps: list[StepOut]


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    index: Annotated[IndexService, Depends(get_index)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[RunOut]:
    return [
        RunOut(
            id=r.id,
            parents=list(r.parents),
            step_count=r.step_count,
            metadata=r.metadata,
            status=r.status,
            redacted=r.redacted,
            dropped_steps=r.dropped_steps,
        )
        for r in index.list_runs(limit)
    ]


@router.get("/runs/{target}", response_model=RunDetail)
def get_run(
    target: str,
    index: Annotated[IndexService, Depends(get_index)],
    repo: Annotated[Repository, Depends(get_repo)],
) -> RunDetail:
    """``target`` may be a ref name, a full run id or a unique id prefix."""
    try:
        run_id = index.resolve(target)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"no run matches '{target}'")
    try:
        run = repo.get_run(run_id)
    except ObjectNotFoundError:
        raise HTTPException(status_code=500, detail="run indexed but missing from store") from None
    steps = [repo.get_step(s) for s in run.steps]
    return RunDetail(
        id=run_id,
        parents=list(run.parents),
        step_count=len(steps),
        metadata=run.metadata,
        status=run.status,
        redacted=run.redacted,
        dropped_steps=run.dropped_steps,
        fork_step=run.fork_step,
        steps=[
            StepOut(
                position=i,
                method=s.method,
                tool_name=s.tool_name,
                outcome=s.outcome,
                started_at_ms=run.timings[i].started_at_ms if run.timings else None,
                duration_ms=run.timings[i].duration_ms if run.timings else None,
            )
            for i, s in enumerate(steps)
        ],
    )


def _resolve(index: IndexService, target: str) -> str:
    try:
        run_id = index.resolve(target)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    if run_id is None:
        raise HTTPException(status_code=404, detail=f"no run matches '{target}'")
    return run_id


def _payload(repo: Repository, blob: str | None) -> Any:
    return None if blob is None else json.loads(repo.get_blob(blob))


class StepFull(StepOut):
    origin: str
    arguments: Any
    result: Any
    events: Any


@router.get("/runs/{target}/steps/{position}", response_model=StepFull)
def get_step(
    target: str,
    position: int,
    index: Annotated[IndexService, Depends(get_index)],
    repo: Annotated[Repository, Depends(get_repo)],
) -> StepFull:
    """One step with its decoded arguments, result and notifications."""
    run = repo.get_run(_resolve(index, target))
    if not 0 <= position < len(run.steps):
        raise HTTPException(status_code=404, detail=f"run has {len(run.steps)} steps")
    step = repo.get_step(run.steps[position])
    timing = run.timings[position] if run.timings else None
    return StepFull(
        position=position,
        method=step.method,
        tool_name=step.tool_name,
        outcome=step.outcome,
        started_at_ms=timing.started_at_ms if timing else None,
        duration_ms=timing.duration_ms if timing else None,
        origin=step.origin,
        arguments=_payload(repo, step.arguments),
        result=_payload(repo, step.result),
        events=_payload(repo, step.events),
    )


def _label(step: Step | None) -> str:
    return "step" if step is None else (step.tool_name or step.method)


class ChangeOut(BaseModel):
    path: str
    before: Any
    after: Any


class StepDiffOut(BaseModel):
    op: str
    a_index: int | None
    b_index: int | None
    label: str
    changes: list[ChangeOut]


class DiffOut(BaseModel):
    a: str
    b: str
    identical: bool
    common_prefix: int
    first_divergence: int | None
    steps: list[StepDiffOut]  # only the steps after the shared prefix


@router.get("/diff", response_model=DiffOut)
def diff(
    a: str,
    b: str,
    index: Annotated[IndexService, Depends(get_index)],
    repo: Annotated[Repository, Depends(get_repo)],
) -> DiffOut:
    """Compare two runs (ids, prefixes or refs), as ``agentgit diff`` does."""
    result = diff_runs(repo, _resolve(index, a), _resolve(index, b))
    return DiffOut(
        a=result.a_id,
        b=result.b_id,
        identical=result.identical,
        common_prefix=result.common_prefix,
        first_divergence=result.first_divergence,
        steps=[
            StepDiffOut(
                op=s.op,
                a_index=s.a_index,
                b_index=s.b_index,
                label=_label(s.b or s.a),
                changes=[ChangeOut(path=c.path, before=c.before, after=c.after) for c in s.changes],
            )
            for s in result.steps[result.common_prefix :]
        ],
    )
