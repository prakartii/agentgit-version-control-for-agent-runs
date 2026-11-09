"""Agent CI end to end: record, save as a scenario, then run `agentgit ci run`.

The agent under test is a real `agentgit-agent` subprocess; the model comes
from a committed-style response cache, the tools from the replayed bundle.
"""

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from test_end_to_end import Recorded, agentgit

AGENT = [
    "{python}",
    "-c",
    "from agentgit_agents.cli import app; app()",
    "run",
    "coding",
    "Fix add()",
    "--model",
    "scripted",
    "--cache-dir",
    "llm_cache",
    "--cache-mode",
    "replay",
    "--",
    "{server}",
]


def cli(rec: Recorded, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        agentgit(*args),
        capture_output=True,
        text=True,
        env={**rec.env, "DATABASE_URL": "postgresql://x@127.0.0.1:1/x"},
        timeout=180,
        check=False,
    )


def make_scenarios(rec: Recorded, tmp: Path, run_id: str, name: str) -> Path:
    scen = tmp / "scenarios"
    proc = cli(rec, "ci", "add", run_id, "--name", name, "--dir", str(scen), "--", *AGENT)
    assert proc.returncode == 0, proc.stderr
    if not (scen / "llm_cache").exists():
        shutil.copytree(rec.cache_dir, scen / "llm_cache")  # the committed model responses
    return scen


def test_a_saved_scenario_passes_in_ci(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    scen = make_scenarios(rec, tmp_path, rec.run_id, "fix add")
    junit = tmp_path / "junit.xml"
    md = tmp_path / "summary.md"

    proc = cli(rec, "ci", "run", str(scen), "--junit", str(junit), "--report-md", str(md))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all passed" in md.read_text()
    suite = ET.fromstring(junit.read_text())
    assert suite.get("tests") == "1" and suite.get("failures") == "0"


def test_a_behaviour_change_fails_the_build_and_names_the_divergence(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    # A recording that stops before write_file: the agent will now do something it never did.
    steps = rec.repo.get_run(rec.run_id).steps
    write_at = next(
        i for i, s in enumerate(steps) if rec.repo.get_step(s).tool_name == "write_file"
    )
    forked = cli(rec, "fork", rec.run_id, "--at", str(write_at))
    assert forked.returncode == 0, forked.stderr
    scen = make_scenarios(rec, tmp_path, forked.stdout.strip(), "stale recording")
    md = tmp_path / "summary.md"

    proc = cli(rec, "ci", "run", str(scen), "--report-md", str(md))

    assert proc.returncode == 1
    report = md.read_text()
    assert "1 failed" in report and "replay diverged" in report and "unexpected_request" in report


def test_bundle_and_unbundle_roundtrip_through_the_cli(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    out = tmp_path / "run.bundle.json"
    assert cli(rec, "bundle", rec.run_id, "-o", str(out)).returncode == 0
    fresh = Recorded.__new__(Recorded)
    fresh.env = {**rec.env, "AGENTGIT_HOME": str(tmp_path / "other_home")}
    proc = cli(fresh, "unbundle", str(out))
    assert proc.returncode == 0 and proc.stdout.strip() == rec.run_id


def test_a_bad_scenario_is_a_usage_error_not_a_failure(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    bad = tmp_path / "x.scenario.toml"
    bad.write_text('name = "x"\nbundle = "b.json"\ncommand = ["echo"]\nexpect_exit_code = 0\n')
    proc = cli(rec, "ci", "run", str(bad))
    assert proc.returncode == 2
