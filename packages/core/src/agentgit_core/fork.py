"""Fork a run: keep a prefix of its steps and optionally edit one tool result.

A fork is a new ``Run`` whose ``parents`` is the source run and whose
``fork_step`` is the position where the two diverge. Because steps are
content-addressed, the shared prefix is the *same step objects*, not copies.

What a fork is, and is not:

* The fork's steps are ``source.steps[:at]`` plus, if a result edit is given,
  one edited copy of ``source.steps[at]``. Everything after is dropped: those
  later steps were produced in a world where the original result was true.
* The fork is a *recorded* alternative history. Replaying it serves exactly
  those steps. What the agent would do next is not known until it runs again,
  so that part is live re-execution, which is approximate (the model is not
  deterministic) and is not claimed to be exact.
"""

import json
from typing import Any

from agentgit_core.hashing import canonical_json
from agentgit_core.objects import Run, StepOutcome
from agentgit_core.repo import Repository


class ForkError(ValueError):
    """The requested fork is not possible (bad position, unsupported edit)."""


def outcome_for_result(method: str, result: Any) -> StepOutcome:
    """The outcome an edited result implies, using the same rule as the recorder."""
    if method == "tools/call" and isinstance(result, dict) and result.get("isError") is True:
        return "tool_error"
    return "ok"


def fork_run(
    repo: Repository,
    source_id: str,
    at: int,
    *,
    new_result: Any = None,
    edit: bool = False,
) -> str:
    """Create and store a fork of ``source_id`` at step position ``at``.

    ``edit=True`` replaces the result of step ``at`` with ``new_result`` (any
    JSON value, including ``None`` for JSON null); ``edit=False`` just truncates
    the history before step ``at``. Returns the new run id.
    """
    source = repo.get_run(source_id)
    if not 0 <= at <= len(source.steps):
        raise ForkError(f"step {at} is out of range; the run has {len(source.steps)} steps")
    if edit and at == len(source.steps):
        raise ForkError("there is no step at that position to edit")

    steps = list(source.steps[:at])
    timings = list(source.timings[:at])
    description = f"truncate@{at}"
    if edit:
        original = repo.get_step(source.steps[at])
        if original.method != "tools/call":
            raise ForkError(f"only tools/call results can be edited, not '{original.method}'")
        if original.outcome == "no_response":
            raise ForkError("that step never received a response; there is no result to edit")
        edited = original.model_copy(
            update={
                "result": repo.put_blob(canonical_json(new_result)),
                "outcome": outcome_for_result(original.method, new_result),
            }
        )
        steps.append(repo.put_step(edited))
        if source.timings:
            timings.append(source.timings[at])
        description = f"result@{at}"

    fork = Run(
        steps=tuple(steps),
        timings=tuple(timings),
        parents=(source_id,),
        fork_step=at,
        # Carry the source's settings (redaction mode, server label) so replay of
        # the fork behaves like replay of the source.
        metadata={**source.metadata, "origin": "fork", "fork_edit": description},
        status="complete",
        redacted=source.redacted,
        dropped_steps=source.dropped_steps,
    )
    return repo.put_run(fork)


def parse_json_value(text: str) -> Any:
    """Parse a user-supplied JSON edit, with a readable error."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ForkError(f"the edit is not valid JSON: {e}") from None
