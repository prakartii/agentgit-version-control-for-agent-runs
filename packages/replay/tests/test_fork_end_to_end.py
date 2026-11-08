"""Record, fork with an edited tool result, continue live, and diff.

Real processes throughout. The model is scripted, so the only thing that can
differ between the original and the fork is the edited tool result and what
the agent does after it.
"""

import json
import subprocess
from pathlib import Path

from test_end_to_end import BUGGY, PY, Recorded, agentgit, run_agent_with

from agentgit_agents.llm import ScriptedLLM, text_response, tool_response
from agentgit_core.diff import diff_runs
from agentgit_core.hashing import canonical_json

OTHER_FIX = "def add(a, b):\n    return sum((a, b))\n"


def read_file_position(rec: Recorded) -> int:
    steps = rec.repo.get_run(rec.run_id).steps
    return next(i for i, s in enumerate(steps) if rec.repo.get_step(s).tool_name == "read_file")


def edited_read_result(rec: Recorded, position: int) -> str:
    """The recorded read_file result with the file text changed (content and structured form)."""
    step = rec.repo.get_step(rec.repo.get_run(rec.run_id).steps[position])
    assert step.result is not None
    body = json.loads(rec.repo.get_blob(step.result))
    changed = "def add(a, b):\n    return a * b  # edited by the fork\n"
    body["content"][0]["text"] = changed
    body["structuredContent"] = {"result": changed}
    return json.dumps(body)


def fork(rec: Recorded, position: int, result_json: str) -> str:
    proc = subprocess.run(
        agentgit("fork", rec.run_id, "--at", str(position), "--result", result_json),
        capture_output=True,
        text=True,
        env={**rec.env, "DATABASE_URL": "postgresql://x@127.0.0.1:1/x"},
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_fork_continues_live_and_diff_shows_where_histories_split(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    position = read_file_position(rec)
    fork_id = fork(rec, position, edited_read_result(rec, position))
    (rec.project / "calc.py").write_text(BUGGY)

    # The agent re-runs: the model now answers the edited file differently. Its first
    # calls are served from the fork; write_file is new, so it goes to the real server.
    script = [
        tool_response(("c1", "list_files", {})),
        tool_response(("c2", "read_file", {"path": "calc.py"})),
        tool_response(("c3", "write_file", {"path": "calc.py", "content": OTHER_FIX})),
        text_response("Rewrote add() with sum()."),
    ]
    live_server = [PY, "-m", "agentgit_demo_servers.files"]
    server = agentgit(
        "record", "--no-index", "--", *agentgit("replay", fork_id, "--live", "--", *live_server)
    )
    result = run_agent_with(server, ScriptedLLM(script), rec.env)

    assert result.status == "completed"
    assert (rec.project / "calc.py").read_text() == OTHER_FIX  # the write really ran, live

    new_ids = {r for r in _run_ids(rec) if r not in {rec.run_id, fork_id}}
    (new_id,) = new_ids
    # The continuation re-recorded the fork's prefix byte for byte, then diverged.
    fork_steps = rec.repo.get_run(fork_id).steps
    new_steps = rec.repo.get_run(new_id).steps
    assert new_steps[: len(fork_steps)] == fork_steps

    diff = diff_runs(rec.repo, rec.run_id, new_id)
    assert diff.first_divergence == position
    changed_paths = {c.path for s in diff.steps for c in s.changes}
    assert "result.content[0].text" in changed_paths
    assert not diff.identical
    assert canonical_json(1)  # keep the import honest for type checkers


def _run_ids(rec: Recorded) -> set[str]:
    from test_end_to_end import run_ids

    return run_ids(rec.home)


def test_without_live_the_fork_reports_the_unrecorded_write_as_divergence(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    position = read_file_position(rec)
    fork_id = fork(rec, position, edited_read_result(rec, position))
    (rec.project / "calc.py").write_text(BUGGY)
    report_file = tmp_path / "report.json"

    script = [
        tool_response(("c1", "list_files", {})),
        tool_response(("c2", "read_file", {"path": "calc.py"})),
        tool_response(("c3", "write_file", {"path": "calc.py", "content": OTHER_FIX})),
        text_response("done"),
    ]
    server = agentgit("replay", fork_id, "--report", str(report_file))
    run_agent_with(server, ScriptedLLM(script), rec.env)

    assert (rec.project / "calc.py").read_text() == BUGGY  # nothing touched the filesystem
    report = json.loads(report_file.read_text())
    kinds = [(d["kind"], d["tool_name"]) for d in report["divergences"]]
    assert kinds == [("unexpected_request", "write_file")]
