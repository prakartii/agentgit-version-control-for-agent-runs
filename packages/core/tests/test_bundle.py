import base64
import copy

import pytest

from agentgit_core.bundle import BundleError, dumps, export_bundle, import_bundle
from agentgit_core.fork import fork_run
from agentgit_core.hashing import canonical_json
from agentgit_core.objects import Run, Step
from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore


def build(repo: Repository) -> str:
    steps = []
    for i in range(3):
        step = Step(
            method="tools/call",
            tool_name=f"t{i}",
            arguments=repo.put_blob(canonical_json({"i": i})),
            result=repo.put_blob(canonical_json({"r": i})),
        )
        steps.append(repo.put_step(step))
    return repo.put_run(Run(steps=tuple(steps)))


def test_roundtrip_into_an_empty_store() -> None:
    src = Repository(MemoryStore())
    run_id = build(src)
    bundle = export_bundle(src, run_id)

    dest = Repository(MemoryStore())
    assert import_bundle(dest._store, bundle) == run_id
    assert dest.get_run(run_id) == src.get_run(run_id)
    dest.verify_run(run_id)


def test_a_fork_bundle_carries_its_parent() -> None:
    src = Repository(MemoryStore())
    parent = build(src)
    fork = fork_run(src, parent, 1, new_result={"edited": True}, edit=True)
    dest = Repository(MemoryStore())
    import_bundle(dest._store, export_bundle(src, fork))
    assert dest.get_run(fork).parents == (parent,)
    dest.verify_run(fork)


def test_export_is_byte_identical_every_time() -> None:
    src = Repository(MemoryStore())
    run_id = build(src)
    assert dumps(export_bundle(src, run_id)) == dumps(export_bundle(src, run_id))


def test_tampered_content_is_rejected() -> None:
    src = Repository(MemoryStore())
    bundle = export_bundle(src, build(src))
    bad = copy.deepcopy(bundle)
    blob = next(o for o in bad["objects"] if o["type"] == "blob")
    blob["data"] = base64.b64encode(b'{"r":"forged"}').decode()
    with pytest.raises(BundleError, match="does not match its content"):
        import_bundle(MemoryStore(), bad)


@pytest.mark.parametrize(
    "bundle",
    [None, {}, {"bundle_version": 99}, {"bundle_version": 1, "run": "x", "objects": [{}]}],
)
def test_malformed_bundles_are_rejected(bundle: object) -> None:
    with pytest.raises(BundleError):
        import_bundle(MemoryStore(), bundle)
