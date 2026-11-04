"""End-to-end tests of `agentgit record` as a real subprocess.

Postgres is deliberately unreachable (port 1): recording must still work and
only report that the run was not indexed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore

FAKE_SERVER = Path(__file__).parents[2] / "proxy" / "tests" / "fake_server.py"
UNREACHABLE_DB = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none"


def record_cmd(*options: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "agentgit_cli",
        "record",
        *options,
        "--",
        sys.executable,
        str(FAKE_SERVER),
    ]


def env_for(home: Path) -> dict[str, str]:
    return {**os.environ, "AGENTGIT_HOME": str(home), "DATABASE_URL": UNREACHABLE_DB}


def stored_run_ids(home: Path) -> list[str]:
    root = home / "objects" / "run"
    return sorted(p.parent.name + p.name for p in root.rglob("*") if p.is_file())


def test_stdio_record_forwards_protocol_bytes_and_stores_the_run(tmp_path: Path) -> None:
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "read", "arguments": {"path": "a.txt"}},
        }
    ).encode()
    proc = subprocess.run(
        record_cmd("--session", "s1", "--server-name", "fake"),
        input=request + b"\n",
        capture_output=True,
        env=env_for(tmp_path),
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr.decode()
    # stdout carries nothing but the server's protocol message.
    reply = json.loads(proc.stdout)
    assert reply["id"] == 7 and reply["result"]["isError"] is False
    assert len(proc.stdout.splitlines()) == 1
    # Human messages go to stderr, and say what was (not) done.
    stderr = proc.stderr.decode()
    assert "recorded run" in stderr and "1 steps" in stderr
    assert "not indexed" in stderr

    (run_id,) = stored_run_ids(tmp_path)
    repo = Repository(FilesystemStore(tmp_path / "objects"))
    run = repo.get_run(run_id)
    assert run.metadata["server"] == "fake" and run.metadata["session_id"] == "s1"
    assert run.status == "complete" and len(run.steps) == 1


def test_exit_code_of_the_server_is_propagated(tmp_path: Path) -> None:
    crash = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "boom", "arguments": {"crash": True}},
        }
    )
    proc = subprocess.run(
        record_cmd(),
        input=crash.encode() + b"\n",
        capture_output=True,
        env=env_for(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 3
    repo = Repository(FilesystemStore(tmp_path / "objects"))
    (run_id,) = stored_run_ids(tmp_path)
    run = repo.get_run(run_id)
    assert run.status == "aborted"
    assert repo.get_step(run.steps[0]).outcome == "no_response"


def test_unknown_server_command_fails_cleanly(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "agentgit_cli", "record", "--", "no-such-binary-xyz"],
        input=b"",
        capture_output=True,
        env=env_for(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 127 and proc.stdout == b""
    assert b"cannot start" in proc.stderr


def test_command_and_upstream_are_mutually_exclusive(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "agentgit_cli", "record"],
        input=b"",
        capture_output=True,
        env=env_for(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 2
    assert b"either a server command" in proc.stderr
