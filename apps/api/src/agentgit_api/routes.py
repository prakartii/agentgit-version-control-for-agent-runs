"""Read-only query endpoints over the run index."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from agentgit_api.deps import get_index, get_repo
from agentgit_core.repo import Repository
from agentgit_core.store import ObjectNotFoundError
from agentgit_index.service import IndexService

router = APIRouter()


class RunOut(BaseModel):
    id: str
    parents: list[str]
    step_count: int
    metadata: dict[str, str]


class StepOut(BaseModel):
    index: int
    kind: str
    tool_name: str | None
    is_error: bool
    duration_ms: int


class RunDetail(RunOut):
    fork_step: int | None
    steps: list[StepOut]


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    index: Annotated[IndexService, Depends(get_index)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[RunOut]:
    return [
        RunOut(id=r.id, parents=list(r.parents), step_count=r.step_count, metadata=r.metadata)
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
        fork_step=run.fork_step,
        steps=[
            StepOut(
                index=s.index,
                kind=s.kind,
                tool_name=s.tool_name,
                is_error=s.is_error,
                duration_ms=s.duration_ms,
            )
            for s in steps
        ],
    )
