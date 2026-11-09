"""Chaos and injection through `agentgit ci run`, against a real agent subprocess.

A deterministic gullible agent proves the harness catches an agent that obeys
injected text; a robust one proves it does not cry wolf. The fault test proves
the loop survives broken tool results. None of this evaluates a real model.
"""

import subprocess
from pathlib import Path

from test_end_to_end import Recorded, agentgit

AGENT_SCRIPT = str(Path(__file__).with_name("chaos_agent.py"))

INJECT_PLAN = """\
[[rules]]
fault = "inject"
tool = "read_file"
payload = "builtin:ignore_previous"
"""

FAULT_PLAN = """\
[[rules]]
fault = "tool_error"
tool = "list_files"
message = "disk unavailable"

[[rules]]
fault = "oversize"
tool = "read_file"
size = 100000
"""


def cli(rec: Recorded, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        agentgit(*args),
        capture_output=True,
        text=True,
        env={**rec.env, "DATABASE_URL": "postgresql://x@127.0.0.1:1/x"},
        timeout=180,
        check=False,
    )


def scenario(rec: Recorded, tmp: Path, mode: str, plan: str, policy: str) -> Path:
    scen = tmp / "scen"
    add = cli(
        rec, "ci", "add", rec.run_id, "--name", "chaos", "--dir", str(scen),
        "--", "{python}", AGENT_SCRIPT, mode, "{server}",
    )  # fmt: skip
    assert add.returncode == 0, add.stderr
    (scen / "plan.toml").write_text(plan, encoding="utf-8")
    toml = scen / "chaos.scenario.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + 'chaos = "plan.toml"\n' + policy)
    return scen


def test_a_gullible_agent_is_caught_acting_on_injected_text(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    scen = scenario(rec, tmp_path, "gullible", INJECT_PLAN, "[policy]\nforbid_canary = true\n")
    md = tmp_path / "out.md"

    proc = cli(rec, "ci", "run", str(scen), "--report-md", str(md))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = md.read_text()
    assert "policy violated" in report and "canary" in report and "write_file" in report


def test_a_robust_agent_ignores_the_same_injection(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    scen = scenario(rec, tmp_path, "robust", INJECT_PLAN, "[policy]\nforbid_canary = true\n")
    proc = cli(rec, "ci", "run", str(scen))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_agent_loop_survives_failing_and_oversized_tool_results(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    scen = scenario(rec, tmp_path, "robust", FAULT_PLAN, "")
    proc = cli(rec, "ci", "run", str(scen))
    assert proc.returncode == 0, proc.stdout + proc.stderr
