"""Object model: blobs, steps, runs and refs.

Blobs hold raw payload bytes. Steps reference blobs by id. Runs reference
steps (in order) and parent runs, forming a DAG. Refs are mutable names that
point at a run id; they are the only objects that are not content-addressed.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentgit_core.hashing import canonical_json, hash_object

StepKind = Literal["tool_listing", "tool_call"]


class Step(BaseModel):
    """One recorded interaction with a tool server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    kind: StepKind
    tool_name: str | None = None
    arguments: str | None = None  # blob id of the call arguments
    result: str | None = None  # blob id of the result (or error payload)
    is_error: bool = False
    duration_ms: int = Field(default=0, ge=0)


class Run(BaseModel):
    """An ordered list of steps plus lineage.

    ``parents`` holds parent run ids (a fork has one). ``fork_step`` is the
    step index in the parent where this run diverged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[str, ...] = ()  # step ids, in order
    parents: tuple[str, ...] = ()
    fork_step: int | None = Field(default=None, ge=0)
    metadata: dict[str, str] = Field(default_factory=dict)


class Ref(BaseModel):
    """A mutable name pointing at a run id (like a git branch)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    run_id: str


def blob_id(data: bytes) -> str:
    """Id of a blob holding ``data``."""
    return hash_object("blob", data)


def encode_step(step: Step) -> tuple[str, bytes]:
    """Return ``(id, canonical bytes)`` for a step."""
    payload = canonical_json(step.model_dump(mode="json"))
    return hash_object("step", payload), payload


def encode_run(run: Run) -> tuple[str, bytes]:
    """Return ``(id, canonical bytes)`` for a run."""
    payload = canonical_json(run.model_dump(mode="json"))
    return hash_object("run", payload), payload
