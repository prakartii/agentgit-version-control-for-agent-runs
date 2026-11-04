"""The hard-kill scenario: a real proxy process is killed without any cleanup."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore

FAKE_SERVER = Path(__file__).parents[2] / "proxy" / "tests" / "fake_server.py"


def call(id_: int, **arguments: object) -> bytes:
    msg = {
        "jsonrpc": "2.0",
        "id": id_,
        "method": "tools/call",
        "params": {"name": f"tool{id_}", "arguments": arguments},
    }
    return json.dumps(msg).encode() + b"\n"


def hard_kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill the whole process tree with no chance to clean up."""
    if sys.platform == "win32":
        # The venv launcher is a separate parent process; /T takes the tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=30)


def agentgit(*args: str, home: Path) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "AGENTGIT_HOME": str(home)}
    return subprocess.run(
        [sys.executable, "-m", "agentgit_cli", *args],
        input=b"",  # stdin closes immediately, so a `record` here ends at once
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


def journal_has_a_step(home: Path) -> bool:
    return any(b'"type":"step"' in p.read_bytes() for p in (home / "journal").glob("*.jsonl"))


def test_killed_proxy_run_is_recoverable(tmp_path: Path) -> None:
    env = {**os.environ, "AGENTGIT_HOME": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentgit_cli", "record", "--no-index", "--",
         sys.executable, str(FAKE_SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        start_new_session=sys.platform != "win32",
    )  # fmt: skip
    assert proc.stdin and proc.stdout
    try:
        proc.stdin.write(call(1, path="a.txt"))
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["id"] == 1  # request 1 completed

        deadline = time.monotonic() + 15
        while not journal_has_a_step(tmp_path) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert journal_has_a_step(tmp_path), "the completed step was never journaled"

        proc.stdin.write(call(2, sleep=30))  # request 2 is in flight when we kill
        proc.stdin.flush()
        time.sleep(0.5)
    finally:
        hard_kill(proc)

    # No run object exists: the proxy never got to write one.
    run_dir = tmp_path / "objects" / "run"
    assert not run_dir.exists() or not any(p.is_file() for p in run_dir.rglob("*"))
    assert list((tmp_path / "journal").glob("*.jsonl"))

    # The next recording warns that something is unrecovered...
    hint = agentgit("record", "--no-index", "--", sys.executable, str(FAKE_SERVER), home=tmp_path)
    assert b"did not finish" in hint.stderr and b"agentgit recover" in hint.stderr

    # ...and `recover` saves what was captured.
    out = agentgit("recover", "--no-index", home=tmp_path)
    assert out.returncode == 0, out.stderr.decode()
    assert b"recovered" in out.stdout and b"1 steps, aborted" in out.stdout

    repo = Repository(FilesystemStore(tmp_path / "objects"))
    runs = [
        repo.get_run(p.parent.name + p.name)
        for p in (tmp_path / "objects" / "run").rglob("*")
        if p.is_file()
    ]
    (run,) = [r for r in runs if r.metadata.get("recovered_from_journal") == "true"]
    assert run.status == "aborted" and len(run.steps) == 1
    assert repo.get_step(run.steps[0]).tool_name == "tool1"
    assert run.metadata["transport"] == "stdio"

    # Recovery is one-shot, and the warning is gone.
    assert b"nothing to recover" in agentgit("recover", "--no-index", home=tmp_path).stdout
    assert not list((tmp_path / "journal").glob("*.jsonl"))
