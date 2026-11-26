from pathlib import Path

from agentgit_core.maintenance import find_unreferenced, verify
from agentgit_core.objects import Run, Step
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore


def build(tmp_path: Path) -> tuple[FilesystemStore, Repository, str, str]:
    store = FilesystemStore(tmp_path)
    repo = Repository(store)
    blob = repo.put_blob(b'{"x": 1}')
    step = repo.put_step(Step(method="tools/call", tool_name="t", result=blob))
    return store, repo, repo.put_run(Run(steps=(step,))), blob


def test_an_intact_store_verifies(tmp_path: Path) -> None:
    store, *_ = build(tmp_path)
    report = verify(store)
    assert report.ok and report.checked == 3


def test_a_tampered_object_is_reported_as_corrupt(tmp_path: Path) -> None:
    store, _, _, blob = build(tmp_path)
    store._path("blob", blob).write_bytes(b"something else")
    report = verify(store)
    assert report.corrupt == [("blob", blob)] and not report.ok


def test_a_missing_blob_is_reported_as_dangling(tmp_path: Path) -> None:
    store, _, _, blob = build(tmp_path)
    store.delete("blob", blob)
    report = verify(store)
    assert report.corrupt == [] and any("missing blob" in d for d in report.dangling)


def test_unreferenced_steps_and_blobs_are_found_but_runs_never_are(tmp_path: Path) -> None:
    store, repo, run_id, blob = build(tmp_path)
    assert find_unreferenced(store) == []
    orphan_blob = repo.put_blob(b"orphan")
    orphan_step = repo.put_step(Step(method="tools/call", tool_name="lost"))
    # A run with no ref and no index entry must survive.
    repo.put_run(Run(steps=(), metadata={"note": "unindexed"}))
    assert find_unreferenced(store) == [("step", orphan_step), ("blob", orphan_blob)]
    assert store.has("run", run_id)


def test_recent_objects_are_left_alone_so_a_live_recording_is_safe(tmp_path: Path) -> None:
    store, repo, *_ = build(tmp_path)
    repo.put_blob(b"just written, its run is not stored yet")
    assert find_unreferenced(store, min_age_s=3600) == []
    assert len(find_unreferenced(store, min_age_s=0)) == 1
