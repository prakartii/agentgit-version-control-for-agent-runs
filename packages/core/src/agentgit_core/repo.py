"""Repository: typed access to the object store with DAG integrity checks.

Because ids are content hashes, a cycle in the run DAG is impossible (a run
cannot name its own id before it exists). What we do check on write is
*referential integrity*: every id a step or run points at must already be
stored, so the DAG never contains dangling references.
"""

from agentgit_core.objects import Run, Step, blob_id, encode_run, encode_step
from agentgit_core.store import ObjectNotFoundError, ObjectStore


class IntegrityError(ValueError):
    """A write would leave a dangling or inconsistent reference."""


class Repository:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store
        self._refs: dict[str, str] = {}

    # -- writes ----------------------------------------------------------
    def put_blob(self, data: bytes) -> str:
        return self._store.put("blob", data)

    def put_step(self, step: Step) -> str:
        for ref in (step.arguments, step.result, step.events):
            if ref is not None and not self._store.has("blob", ref):
                raise IntegrityError(f"step references missing blob {ref}")
        _, payload = encode_step(step)
        return self._store.put("step", payload)

    def put_run(self, run: Run) -> str:
        for step_id in run.steps:
            if not self._store.has("step", step_id):
                raise IntegrityError(f"run references missing step {step_id}")
        for parent in run.parents:
            if not self._store.has("run", parent):
                raise IntegrityError(f"run references missing parent {parent}")
        if (run.fork_step is None) != (not run.parents):
            raise IntegrityError("fork_step must be set exactly when a run has parents")
        _, payload = encode_run(run)
        return self._store.put("run", payload)

    # -- reads -----------------------------------------------------------
    def has_step(self, object_id: str) -> bool:
        return self._store.has("step", object_id)

    def get_blob(self, object_id: str) -> bytes:
        data = self._store.get("blob", object_id)
        if blob_id(data) != object_id:
            raise IntegrityError(f"blob {object_id} is corrupt")
        return data

    def get_step(self, object_id: str) -> Step:
        return Step.model_validate_json(self._store.get("step", object_id))

    def get_run(self, object_id: str) -> Run:
        return Run.model_validate_json(self._store.get("run", object_id))

    # -- refs (mutable names) ---------------------------------------------
    def set_ref(self, name: str, run_id: str) -> None:
        if not self._store.has("run", run_id):
            raise ObjectNotFoundError(run_id)
        self._refs[name] = run_id

    def get_ref(self, name: str) -> str:
        try:
            return self._refs[name]
        except KeyError:
            raise ObjectNotFoundError(name) from None

    def list_refs(self) -> dict[str, str]:
        return dict(self._refs)

    # -- integrity --------------------------------------------------------
    def verify_run(self, run_id: str) -> None:
        """Walk a run and its ancestors, checking every reference resolves."""
        seen: set[str] = set()
        stack = [run_id]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            run = self.get_run(current)
            for step_id in run.steps:
                step = self.get_step(step_id)
                for blob in (step.arguments, step.result, step.events):
                    if blob is not None:
                        self.get_blob(blob)
            stack.extend(run.parents)
