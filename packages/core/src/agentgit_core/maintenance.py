"""Check a filesystem object store, and clean out objects nothing refers to.

``verify`` answers "is anything damaged?": every object is re-hashed and must
match the id it is stored under, and every reference from a run to its steps,
blobs and parent runs must resolve.

``find_unreferenced`` answers "what is dead weight?": steps and blobs that no run
uses (typically left by a recording that crashed before its run was written).
**Runs are never garbage.** They are the thing being kept, and a run recorded while
the index was down has no ref, so "unreferenced runs" would delete real data.
"""

from dataclasses import dataclass, field

from agentgit_core.hashing import hash_object
from agentgit_core.objects import Run, Step
from agentgit_core.store import FilesystemStore, ObjectNotFoundError

TYPES = ("blob", "step", "run")


@dataclass
class VerifyReport:
    checked: int = 0
    corrupt: list[tuple[str, str]] = field(default_factory=list)  # (type, id): content != id
    dangling: list[str] = field(default_factory=list)  # human-readable missing references

    @property
    def ok(self) -> bool:
        return not self.corrupt and not self.dangling


def verify(store: FilesystemStore) -> VerifyReport:
    report = VerifyReport()
    for object_type in TYPES:
        for object_id in store.iter_ids(object_type):
            report.checked += 1
            payload = store.get(object_type, object_id)
            if hash_object(object_type, payload) != object_id:
                report.corrupt.append((object_type, object_id))

    corrupt = set(report.corrupt)
    for run_id in store.iter_ids("run"):
        if ("run", run_id) in corrupt:
            continue  # cannot trust its contents; already reported
        run = Run.model_validate_json(store.get("run", run_id))
        for parent in run.parents:
            if not store.has("run", parent):
                report.dangling.append(f"run {run_id[:12]} -> missing parent run {parent[:12]}")
        for step_id in run.steps:
            if not store.has("step", step_id):
                report.dangling.append(f"run {run_id[:12]} -> missing step {step_id[:12]}")
                continue
            if ("step", step_id) in corrupt:
                continue
            step = Step.model_validate_json(store.get("step", step_id))
            for blob in (step.arguments, step.result, step.events):
                if blob is not None and not store.has("blob", blob):
                    report.dangling.append(f"step {step_id[:12]} -> missing blob {blob[:12]}")
    return report


def find_unreferenced(store: FilesystemStore, min_age_s: float = 0) -> list[tuple[str, str]]:
    """Steps and blobs that no run reaches, as (type, id).

    A recording writes its blobs and steps *before* the run that refers to them,
    so a live session briefly has unreferenced objects. ``min_age_s`` skips
    anything newer than that, so cleanup never races a recording in progress.
    """
    used_steps: set[str] = set()
    used_blobs: set[str] = set()
    for run_id in store.iter_ids("run"):
        try:
            run = Run.model_validate_json(store.get("run", run_id))
        except ValueError:
            continue
        used_steps.update(run.steps)
    for step_id in used_steps:
        try:
            step = Step.model_validate_json(store.get("step", step_id))
        except (ObjectNotFoundError, ValueError):
            continue
        used_blobs.update(b for b in (step.arguments, step.result, step.events) if b)
    candidates = [("step", i) for i in store.iter_ids("step") if i not in used_steps] + [
        ("blob", i) for i in store.iter_ids("blob") if i not in used_blobs
    ]
    return [(t, i) for t, i in candidates if store.age_seconds(t, i) >= min_age_s]
