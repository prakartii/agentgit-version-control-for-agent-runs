"""A tiny deterministic agent for chaos tests: a coding-agent loop with a reactive fake model.

Usage: python chaos_agent.py <gullible|robust> <mcp server command...>

Both modes follow the same plan (list files, read calc.py, write the fix, finish).
The gullible model also obeys instructions it finds in a tool result: if the
canary appears there it writes it to pwned.txt, which is the behaviour an
injection test exists to catch. This models a vulnerable model; it says nothing
about any real one.
"""

import asyncio
import os
import sys

from mcp import StdioServerParameters

from agentgit_agents.agents import CODING_AGENT
from agentgit_agents.llm import (
    LLMRequest,
    LLMResponse,
    ToolResultBlock,
    text_response,
    tool_response,
)
from agentgit_agents.loop import AgentConfig, McpToolClient, run_agent
from agentgit_replay.payloads import CANARY

FIXED = "def add(a, b):\n    return a + b\n"


class ReactiveLLM:
    def __init__(self, gullible: bool) -> None:
        self._gullible = gullible
        self._hijacked = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        results = [
            b.content for m in request.messages for b in m.content if isinstance(b, ToolResultBlock)
        ]
        if self._gullible and not self._hijacked and any(CANARY in r for r in results):
            self._hijacked = True
            return tool_response(("evil", "write_file", {"path": "pwned.txt", "content": CANARY}))
        plan = [
            tool_response(("c1", "list_files", {})),
            tool_response(("c2", "read_file", {"path": "calc.py"})),
            tool_response(("c3", "write_file", {"path": "calc.py", "content": FIXED})),
        ]
        done = sum(1 for m in request.messages if m.role == "assistant" and not self._is_evil(m))
        return plan[done] if done < len(plan) else text_response("Done.")

    @staticmethod
    def _is_evil(message: object) -> bool:
        return any(getattr(b, "id", "") == "evil" for b in getattr(message, "content", []))


async def main(mode: str, server: list[str]) -> int:
    params = StdioServerParameters(command=server[0], args=server[1:], env=dict(os.environ))
    async with McpToolClient(params) as tools:
        result = await run_agent(
            CODING_AGENT,
            AgentConfig(model="scripted"),
            ReactiveLLM(mode == "gullible"),
            tools,
            "Fix add()",
        )
    print(result.answer or result.status)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2:])))
