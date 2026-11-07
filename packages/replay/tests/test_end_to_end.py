"""Record a real agent run, then replay it with no tool server and no model.

Everything here is a real process or the real SDK: the agent loop, the SDK MCP
client, the recording proxy, the demo servers and `agentgit replay`. The model
is scripted, so the only nondeterminism is the system under test.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters

from agentgit_agents.agents import CODING_AGENT
from agentgit_agents.llm import (
    CachingLLM,
    FileResponseCache,
    LLMClient,
    ScriptedLLM,
    text_response,
    tool_response,
)
from agentgit_agents.loop import AgentConfig, AgentResult, McpToolClient, run_agent
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore

PY = sys.executable
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TASK = "Fix add()"

SCRIPT = [
    tool_response(("c1", "list_files", {})),
    tool_response(("c2", "read_file", {"path": "calc.py"})),
    tool_response(("c3", "write_file", {"path": "calc.py", "content": FIXED})),
    text_response("Fixed add(): it subtracted instead of adding."),
]


def run_agent_with(server: list[str], llm: LLMClient, env: dict[str, str]) -> AgentResult:
    params = StdioServerParameters(command=server[0], args=server[1:], env=env)

    async def go() -> AgentResult:
        async with McpToolClient(params) as tools:
            return await run_agent(CODING_AGENT, AgentConfig(model="scripted"), llm, tools, TASK)

    return asyncio.run(go())


def agentgit(*args: str) -> list[str]:
    return [PY, "-m", "agentgit_cli", *args]


def run_ids(home: Path) -> set[str]:
    root = home / "objects" / "run"
    return (
        {p.parent.name + p.name for p in root.rglob("*") if p.is_file()} if root.exists() else set()
    )


class Recorded:
    """A recorded coding-agent run plus the model cache that goes with it."""

    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "home"
        self.project = tmp / "project"
        self.cache_dir = tmp / "llm_cache"
        self.project.mkdir()
        (self.project / "calc.py").write_text(BUGGY)
        self.env = {
            **os.environ,
            "AGENTGIT_HOME": str(self.home),
            "DEMO_FILES_ROOT": str(self.project),
        }

        llm = CachingLLM(ScriptedLLM(list(SCRIPT)), FileResponseCache(self.cache_dir), "record")
        server = agentgit("record", "--no-index", "--", PY, "-m", "agentgit_demo_servers.files")
        self.result = run_agent_with(server, llm, self.env)
        (self.run_id,) = run_ids(self.home)
        self.repo = Repository(FilesystemStore(self.home / "objects"))

    def replay_llm(self) -> LLMClient:
        """Model outputs from the cache only: a miss is an error, never a live call."""
        return CachingLLM(None, FileResponseCache(self.cache_dir), "replay")


def test_strict_replay_reproduces_the_run_with_no_server_and_no_model(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    assert rec.result.status == "completed" and (rec.project / "calc.py").read_text() == FIXED

    # Put the sandbox back. If replay touched the real world, it would be fixed again.
    (rec.project / "calc.py").write_text(BUGGY)
    report_file = tmp_path / "report.json"
    server = agentgit("replay", rec.run_id[:12], "--report", str(report_file))

    replayed = run_agent_with(server, rec.replay_llm(), rec.env)

    assert replayed.status == "completed"
    assert replayed.answer == rec.result.answer  # same model output, same tool results
    assert [c.model_dump() for c in replayed.tool_calls] == [
        c.model_dump() for c in rec.result.tool_calls
    ]
    assert (rec.project / "calc.py").read_text() == BUGGY  # nothing ran against the filesystem
    report = json.loads(report_file.read_text())
    assert report["ok"] is True and report["divergences"] == []
    assert report["served"] == report["recorded_steps"] > 0


def test_a_replayed_session_records_exactly_the_original_steps(tmp_path: Path) -> None:
    """Fidelity: record the replay and compare it with the original, step id by step id."""
    rec = Recorded(tmp_path)
    server = agentgit("record", "--no-index", "--", *agentgit("replay", rec.run_id))
    run_agent_with(server, rec.replay_llm(), rec.env)

    (second_id,) = run_ids(rec.home) - {rec.run_id}
    first, second = rec.repo.get_run(rec.run_id), rec.repo.get_run(second_id)
    assert second.steps == first.steps  # identical content, identical content hashes
    assert second.status == "complete"


def test_a_different_agent_behaviour_is_reported_as_divergence(tmp_path: Path) -> None:
    rec = Recorded(tmp_path)
    report_file = tmp_path / "report.json"
    server = agentgit("replay", rec.run_id, "--report", str(report_file))

    changed = [  # reads a file the recording never read, and stops early
        tool_response(("c1", "list_files", {})),
        tool_response(("c2", "read_file", {"path": "other.py"})),
        text_response("gave up"),
    ]
    result = run_agent_with(server, ScriptedLLM(changed), rec.env)

    # The agent got a protocol error for the unrecorded call, not an invented answer.
    assert result.tool_calls[1].outcome == "failed"
    assert "replay divergence" in result.tool_calls[1].detail
    report: dict[str, Any] = json.loads(report_file.read_text())
    assert report["ok"] is False
    kinds = {d["kind"] for d in report["divergences"]}
    assert {"unexpected_request", "missing_request"} <= kinds
    unexpected = [d for d in report["divergences"] if d["kind"] == "unexpected_request"]
    assert unexpected[0]["tool_name"] == "read_file"
    missing = [d["tool_name"] for d in report["divergences"] if d["kind"] == "missing_request"]
    assert missing == ["read_file", "write_file"]


def test_replay_of_an_unknown_run_fails_cleanly(tmp_path: Path) -> None:
    import subprocess

    proc = subprocess.run(
        agentgit("replay", "deadbeef"),
        input=b"",
        capture_output=True,
        env={
            **os.environ,
            "AGENTGIT_HOME": str(tmp_path),
            "DATABASE_URL": "postgresql://x@127.0.0.1:1/x",
        },
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2 and proc.stdout == b"" and b"no run matches" in proc.stderr
