"""Object model: blobs, steps, runs and refs.

Blobs hold raw payload bytes. Steps reference blobs by id. Runs reference
steps (in order) and parent runs, forming a DAG. Refs are mutable names that
point at a run id; they are the only objects that are not content-addressed.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentgit_core.hashing import canonical_json, hash_object

StepKind = Literal["tool_listing", "tool_call"]

# How an exchange ended. "tool_error" is a result with isError=true;
# "protocol_error" is a JSON-RPC error response; "no_response" means the
# request was seen but no reply arrived before the session ended.
StepOutcome = Literal["ok", "tool_error", "protocol_error", "no_response"]
RunStatus = Literal["complete", "aborted"]


class Step(BaseModel):
    """One recorded interaction with a tool server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Legacy fields, removed once all readers use `method`/`outcome` (ADR-003).
    index: int | None = Field(default=None, ge=0)
    kind: StepKind | None = None
    # Content fields (ADR-003).
    method: str | None = None  # JSON-RPC method, e.g. "tools/call"
    outcome: StepOutcome = "ok"
    events: str | None = None  # blob id of notifications seen during the request
    tool_name: str | None = None
    arguments: str | None = None  # blob id of the call arguments
    result: str | None = None  # blob id of the result (or error payload)
    is_error: bool = False
    duration_ms: int = Field(default=0, ge=0)


class StepTiming(BaseModel):
    """Wall-clock timing of one step, kept on the run so steps stay content-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at_ms: int = Field(ge=0)  # Unix epoch milliseconds
    duration_ms: int = Field(ge=0)


class Run(BaseModel):
    """An ordered list of steps plus lineage.

    ``parents`` holds parent run ids (a fork has one). ``fork_step`` is the
    step index in the parent where this run diverged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[str, ...] = ()  # step ids, in order
    timings: tuple[StepTiming, ...] = ()  # aligned with steps, or empty
    parents: tuple[str, ...] = ()
    fork_step: int | None = Field(default=None, ge=0)
    metadata: dict[str, str] = Field(default_factory=dict)
    # Recording completeness, made explicit rather than implied:
    status: RunStatus = "complete"  # "aborted" if the session ended abnormally
    redacted: bool = False  # True if redaction changed any recorded content
    dropped_steps: int = Field(default=0, ge=0)  # exchanges lost to storage errors

    @model_validator(mode="after")
    def _timings_align_with_steps(self) -> "Run":
        if self.timings and len(self.timings) != len(self.steps):
            raise ValueError("timings must be empty or the same length as steps")
        return self


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
