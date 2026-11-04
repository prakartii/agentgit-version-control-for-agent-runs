"""The demo agents driven through the real recording proxy.

The agent code is the same as when it talks to a server directly; only the
server command differs (it starts with `agentgit record --`). A scripted model
keeps the runs deterministic and offline.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters

from agentgit_agents.agents import CODING_AGENT, SUPPORT_AGENT
from agentgit_agents.llm import (
    CachingLLM,
    MemoryResponseCache,
    ScriptedLLM,
    text_response,
    tool_response,
)
from agentgit_agents.loop import AgentConfig, AgentResult, AgentSpec, McpToolClient, run_agent
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore

PY = sys.executable
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"

CODING_SCRIPT = [
    tool_response(("c1", "list_files", {})),
    tool_response(("c2", "read_file", {"path": "calc.py"})),
    tool_response(("c3", "write_file", {"path": "calc.py", "content": FIXED})),
    text_response("Fixed add(): it subtracted instead of adding."),
]

SUPPORT_SCRIPT = [
    tool_response(("s1", "search_tickets", {"query": "charged"})),
    tool_response(("s2", "get_ticket", {"ticket_id": "T-101"})),
    tool_response(
        (
            "s3",
            "update_ticket",
            {"ticket_id": "T-101", "status": "pending", "note": "Refund requested"},
        )
    ),
    text_response("T-101 is a double charge; I set it to pending with a note."),
]


def run_recorded(
    spec: AgentSpec,
    script: list[Any],
    server_module: str,
    home: Path,
    files_root: Path,
    task: str,
) -> AgentResult:
    """Run an agent against a demo server launched via `agentgit record`."""
    env = {**os.environ, "AGENTGIT_HOME": str(home), "DEMO_FILES_ROOT": str(files_root)}
    params = StdioServerParameters(
        command=PY,
        args=["-m", "agentgit_cli", "record", "--no-index", "--session", "it", "--",
              PY, "-m", server_module],
        env=env,
    )  # fmt: skip

    async def go() -> AgentResult:
        llm = CachingLLM(ScriptedLLM(list(script)), MemoryResponseCache(), "off")
        async with McpToolClient(params) as tools:
            return await run_agent(spec, AgentConfig(model="scripted"), llm, tools, task)

    return asyncio.run(go())


def recorded_runs(home: Path) -> list[Any]:
    root = home / "objects" / "run"
    repo = Repository(FilesystemStore(home / "objects"))
    runs = [repo.get_run(p.parent.name + p.name) for p in root.rglob("*") if p.is_file()]
    return [(repo, r) for r in runs]


def test_coding_agent_fixes_a_file_and_the_proxy_records_it(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    (project / "calc.py").write_text(BUGGY)

    result = run_recorded(
        CODING_AGENT, CODING_SCRIPT, "agentgit_demo_servers.files", home, project, "Fix add()"
    )

    assert result.status == "completed" and "Fixed add()" in result.answer
    assert (project / "calc.py").read_text() == FIXED
    assert [c.name for c in result.tool_calls] == ["list_files", "read_file", "write_file"]

    ((repo, run),) = recorded_runs(home)
    steps = [repo.get_step(s) for s in run.steps]
    tool_steps = [s for s in steps if s.method == "tools/call"]
    assert [s.tool_name for s in tool_steps] == ["list_files", "read_file", "write_file"]
    assert all(s.outcome == "ok" for s in tool_steps)
    assert run.status == "complete" and run.dropped_steps == 0
    assert run.metadata["session_id"] == "it"
    # The write is in the recording, with the new file content as its argument.
    write_args = repo.get_blob(tool_steps[2].arguments)
    assert b"return a + b" in write_args


def test_support_agent_updates_a_ticket_through_the_proxy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run_recorded(
        SUPPORT_AGENT,
        SUPPORT_SCRIPT,
        "agentgit_demo_servers.tickets",
        home,
        tmp_path,
        "Handle the double-charge ticket",
    )
    assert result.status == "completed"
    assert [c.name for c in result.tool_calls] == ["search_tickets", "get_ticket", "update_ticket"]

    ((repo, run),) = recorded_runs(home)
    names = [
        repo.get_step(s).tool_name for s in run.steps if repo.get_step(s).method == "tools/call"
    ]
    assert names == ["search_tickets", "get_ticket", "update_ticket"]
    update = [repo.get_step(s) for s in run.steps if repo.get_step(s).tool_name == "update_ticket"][
        0
    ]
    assert update.result and b"pending" in repo.get_blob(update.result)


def test_an_invalid_tool_call_never_reaches_the_server_or_the_recording(tmp_path: Path) -> None:
    home = tmp_path / "home"
    script = [
        tool_response(("s1", "update_ticket", {"ticket_id": "bogus", "status": "closed"})),
        tool_response(("s2", "get_ticket", {"ticket_id": "T-100"})),
        text_response("looked it up instead"),
    ]
    result = run_recorded(
        SUPPORT_AGENT, script, "agentgit_demo_servers.tickets", home, tmp_path, "Close T-100"
    )
    assert [c.outcome for c in result.tool_calls] == ["invalid_arguments", "ok"]
    ((repo, run),) = recorded_runs(home)
    called = [
        repo.get_step(s).tool_name for s in run.steps if repo.get_step(s).method == "tools/call"
    ]
    assert called == ["get_ticket"]  # the refused update_ticket was never sent


def test_same_agent_run_twice_records_identical_steps(tmp_path: Path) -> None:
    """The property replay and fork rely on: same behaviour, same step ids."""
    step_ids = []
    for n in range(2):
        home, project = tmp_path / f"home{n}", tmp_path / f"project{n}"
        project.mkdir()
        (project / "calc.py").write_text(BUGGY)
        run_recorded(
            CODING_AGENT, CODING_SCRIPT, "agentgit_demo_servers.files", home, project, "Fix add()"
        )
        ((_, run),) = recorded_runs(home)
        step_ids.append(run.steps)
    assert step_ids[0] == step_ids[1]
