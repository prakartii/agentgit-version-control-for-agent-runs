"""`agentgit fork`, `diff` and `checkout` as real subprocesses, with no database."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentgit_core.objects import Run
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore

UNREACHABLE_DB = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none"
EDITED = {"content": [{"type": "text", "text": "EDITED"}]}


def cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "AGENTGIT_HOME": str(home), "DATABASE_URL": UNREACHABLE_DB}
    return subprocess.run(
        [sys.executable, "-m", "agentgit_cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str]:
    """A three-step run written into a fresh object store."""
    from agentgit_core.hashing import canonical_json
    from agentgit_core.objects import Step

    home = tmp_path / "home"
    repo = Repository(FilesystemStore(home / "objects"))
    ids = []
    for i, text in enumerate(["alpha", "beta", "gamma"]):
        step = Step(
            method="tools/call",
            tool_name=f"tool{i}",
            arguments=repo.put_blob(canonical_json({"i": i})),
            result=repo.put_blob(canonical_json({"content": [{"type": "text", "text": text}]})),
        )
        ids.append(repo.put_step(step))
    return home, repo.put_run(Run(steps=tuple(ids), metadata={"server": "demo"}))


def test_fork_edit_then_diff_and_replay_source_of_truth(source: tuple[Path, str]) -> None:
    home, run_id = source
    forked = cli(home, "fork", run_id[:10], "--at", "1", "--result", json.dumps(EDITED))
    assert forked.returncode == 0, forked.stderr
    fork_id = forked.stdout.strip()
    assert len(fork_id) == 64 and "not added to the index" in forked.stderr  # DB down is non-fatal

    repo = Repository(FilesystemStore(home / "objects"))
    fork = repo.get_run(fork_id)
    assert fork.parents == (run_id,) and fork.fork_step == 1 and len(fork.steps) == 2

    diff = cli(home, "diff", run_id[:10], fork_id[:10])
    assert diff.returncode == 1  # they differ
    assert "first difference at step 1" in diff.stdout
    assert "result.content[0].text" in diff.stdout
    assert '- "beta"' in diff.stdout and '+ "EDITED"' in diff.stdout
    assert "only in A" in diff.stdout  # step 2 was dropped by the fork

    machine = json.loads(cli(home, "diff", run_id, fork_id, "--json").stdout)
    assert machine["identical"] is False and machine["first_divergence"] == 1


def test_diff_of_a_run_with_itself_is_identical(source: tuple[Path, str]) -> None:
    home, run_id = source
    same = cli(home, "diff", run_id, run_id)
    assert same.returncode == 0 and "identical" in same.stdout


def test_fork_without_an_edit_truncates(source: tuple[Path, str]) -> None:
    home, run_id = source
    proc = cli(home, "fork", run_id, "--at", "2")
    fork = Repository(FilesystemStore(home / "objects")).get_run(proc.stdout.strip())
    assert len(fork.steps) == 2


def test_bad_fork_requests_fail_cleanly(source: tuple[Path, str]) -> None:
    home, run_id = source
    far = cli(home, "fork", run_id, "--at", "9")
    assert far.returncode == 2 and "out of range" in far.stderr and far.stdout == ""
    bad = cli(home, "fork", run_id, "--at", "0", "--result", "{oops")
    assert bad.returncode == 2 and "not valid JSON" in bad.stderr
    unknown = cli(home, "diff", "deadbeef", run_id)
    assert unknown.returncode == 2 and "no run matches" in unknown.stderr


def test_result_can_come_from_a_checked_out_file(source: tuple[Path, str], tmp_path: Path) -> None:
    home, run_id = source
    out = tmp_path / "export"
    assert cli(home, "checkout", run_id, "--to", str(out)).returncode == 0
    step_file = out / "steps" / "001-tool1.json"
    exported = json.loads(step_file.read_text(encoding="utf-8"))
    assert exported["tool_name"] == "tool1" and exported["result"]["content"][0]["text"] == "beta"
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))["steps"] == 3

    edit = tmp_path / "edit.json"
    edit.write_text(json.dumps(EDITED), encoding="utf-8")
    proc = cli(home, "fork", run_id, "--at", "1", "--result-file", str(edit))
    assert proc.returncode == 0, proc.stderr
