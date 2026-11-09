"""Re-create the committed Agent CI scenario from scratch, with no network.

Runs the coding agent with a scripted model against the demo file server through
the recording proxy, then saves the run as a scenario next to this file.

    uv run python examples/agent-ci/regenerate.py

Run it only when the agent's intended behaviour changes; the diff in the
bundle is then the reviewable record of that change.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import StdioServerParameters

from agentgit_agents.agents import CODING_AGENT
from agentgit_agents.llm import (
    CachingLLM,
    FileResponseCache,
    ScriptedLLM,
    text_response,
    tool_response,
)
from agentgit_agents.loop import AgentConfig, McpToolClient, run_agent

HERE = Path(__file__).parent
FIXED = "def add(a, b):\n    return a + b\n"
TASK = "Fix add()"
SCRIPT = [
    tool_response(("c1", "list_files", {})),
    tool_response(("c2", "read_file", {"path": "calc.py"})),
    tool_response(("c3", "write_file", {"path": "calc.py", "content": FIXED})),
    text_response("Fixed add(): it subtracted instead of adding."),
]
AGENT = [
    "{python}", "-c", "from agentgit_agents.cli import app; app()",
    "run", "coding", TASK, "--model", "scripted",
    "--cache-dir", "llm_cache", "--cache-mode", "replay", "--", "{server}",
]  # fmt: skip


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home, project = Path(tmp) / "home", Path(tmp) / "project"
        project.mkdir()
        (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        env = {**os.environ, "AGENTGIT_HOME": str(home), "DEMO_FILES_ROOT": str(project)}
        py = sys.executable
        server = [py, "-m", "agentgit_cli", "record", "--no-index", "--",
                  py, "-m", "agentgit_demo_servers.files"]  # fmt: skip
        llm = CachingLLM(ScriptedLLM(list(SCRIPT)), FileResponseCache(HERE / "llm_cache"), "record")

        async def go() -> None:
            params = StdioServerParameters(command=server[0], args=server[1:], env=env)
            async with McpToolClient(params) as tools:
                result = await run_agent(
                    CODING_AGENT, AgentConfig(model="scripted"), llm, tools, TASK
                )
            assert result.status == "completed", result

        asyncio.run(go())
        (run_dir,) = (home / "objects" / "run").iterdir()
        (run_file,) = run_dir.iterdir()
        run_id = run_dir.name + run_file.name
        subprocess.run(
            [py, "-m", "agentgit_cli", "ci", "add", run_id, "--name", "coding agent fixes add",
             "--dir", str(HERE), "--", *AGENT],
            env=env, check=True,
        )  # fmt: skip


if __name__ == "__main__":
    main()
