"""Run one scenario: import its bundle, replay it to the agent, judge the outcome.

Hermetic by construction: a fresh temporary object store per scenario, the real
tool server is never started, and replay serves only what was recorded. The
agent is a real subprocess, so this tests the actual agent code.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgit_cli.ci.scenario import PYTHON_TOKEN, SERVER_TOKEN, Scenario
from agentgit_core.bundle import BundleError, import_bundle
from agentgit_core.store import FilesystemStore
from agentgit_replay.policy import evaluate

MAX_OUTPUT_CHARS = 4000


@dataclass
class ScenarioResult:
    name: str
    path: str
    passed: bool
    duration_s: float
    failures: list[str] = field(default_factory=list)
    exit_code: int | None = None
    replay: dict[str, Any] | None = None  # the replay report, if one was produced
    output_tail: str = ""


def expand_command(command: list[str], server: list[str]) -> list[str]:
    out: list[str] = []
    for token in command:
        if token == SERVER_TOKEN:
            out.extend(server)
        elif token == PYTHON_TOKEN:
            out.append(sys.executable)
        else:
            out.append(token)
    return out


def _tail(text: str) -> str:
    return text[-MAX_OUTPUT_CHARS:]


def run_scenario(scenario: Scenario, scenario_file: Path, workdir: Path) -> ScenarioResult:
    started = time.monotonic()
    result = ScenarioResult(scenario.name, str(scenario_file), False, 0.0)

    def finish() -> ScenarioResult:
        result.duration_s = round(time.monotonic() - started, 3)
        result.passed = not result.failures
        return result

    bundle_path = workdir / scenario.bundle
    with tempfile.TemporaryDirectory(prefix="agentgit-ci-") as tmp:
        home = Path(tmp) / "home"
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            run_id = import_bundle(FilesystemStore(home / "objects"), bundle)
        except (OSError, ValueError, BundleError) as e:  # BundleError is a ValueError
            result.failures.append(f"cannot load bundle {scenario.bundle}: {e}")
            return finish()

        report_file = Path(tmp) / "report.json"
        server = [
            sys.executable,
            "-m",
            "agentgit_cli",
            "replay",
            run_id,
            "--report",
            str(report_file),
        ]
        if scenario.chaos:
            server += ["--chaos", str(workdir / scenario.chaos)]
        env = {**os.environ, **scenario.env, "AGENTGIT_HOME": str(home)}
        try:
            proc = subprocess.run(
                expand_command(scenario.command, server),
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=scenario.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result.failures.append(f"timed out after {scenario.timeout_seconds:g}s")
            return finish()
        except OSError as e:
            result.failures.append(f"could not start the agent command: {e}")
            return finish()

        result.exit_code = proc.returncode
        result.output_tail = _tail(proc.stdout + ("\n" + proc.stderr if proc.stderr else ""))
        if report_file.exists():
            result.replay = json.loads(report_file.read_text(encoding="utf-8"))

    if proc.returncode != scenario.expect_exit:
        result.failures.append(f"agent exited {proc.returncode}, expected {scenario.expect_exit}")
    if result.replay is None:
        result.failures.append("no replay report: the agent never connected to the replay server")
    elif not result.replay["ok"] and not scenario.allow_divergence and not scenario.chaos:
        # (Under chaos the data is changed on purpose, so divergence is expected; the
        # policy is what judges those runs.)
        kinds = sorted({d["kind"] for d in result.replay["divergences"]})
        result.failures.append(
            f"replay diverged ({len(result.replay['divergences'])}: {', '.join(kinds)})"
        )
    if result.replay is not None:
        violations = evaluate(result.replay, scenario.policy)
        result.failures.extend(f"policy violated: {v.describe()}" for v in violations)
    for needle in scenario.stdout_contains:
        if needle not in proc.stdout:
            result.failures.append(f"output does not contain {needle!r}")
    return finish()
