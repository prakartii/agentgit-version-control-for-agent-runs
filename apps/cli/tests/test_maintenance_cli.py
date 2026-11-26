"""`agentgit verify` and `gc` as real subprocesses, with no database."""

import os
import subprocess
import sys
from pathlib import Path

from agentgit_core.objects import Run, Step
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore


def cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "AGENTGIT_HOME": str(home)}
    return subprocess.run(
        [sys.executable, "-m", "agentgit_cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def make_store(home: Path) -> tuple[FilesystemStore, Repository, str]:
    store = FilesystemStore(home / "objects")
    repo = Repository(store)
    blob = repo.put_blob(b'{"ok": true}')
    step = repo.put_step(Step(method="tools/call", tool_name="t", result=blob))
    return store, repo, repo.put_run(Run(steps=(step,)))


def test_verify_passes_on_an_intact_store_and_fails_on_damage(tmp_path: Path) -> None:
    store, _, run_id = make_store(tmp_path)
    assert cli(tmp_path, "verify").returncode == 0
    store._path("run", run_id).write_bytes(b'{"steps": []}')  # valid JSON, wrong hash
    result = cli(tmp_path, "verify")
    assert result.returncode == 1 and "CORRUPT" in result.stdout


def test_gc_reports_by_default_and_deletes_only_with_the_flag(tmp_path: Path) -> None:
    store, repo, run_id = make_store(tmp_path)
    orphan = repo.put_blob(b"orphan")
    dry = cli(tmp_path, "gc", "--grace-seconds", "0")
    assert "would delete  blob" in dry.stdout and store.has("blob", orphan)

    real = cli(tmp_path, "gc", "--grace-seconds", "0", "--delete")
    assert real.returncode == 0 and not store.has("blob", orphan)
    assert store.has("run", run_id)
    assert cli(tmp_path, "verify").returncode == 0  # nothing the run needs was removed


def test_gc_spares_recent_objects_by_default(tmp_path: Path) -> None:
    store, repo, _ = make_store(tmp_path)
    fresh = repo.put_blob(b"written a moment ago")
    result = cli(tmp_path, "gc", "--delete")  # default one-hour grace
    assert store.has("blob", fresh) and "deleted 0 object(s)" in result.stdout
