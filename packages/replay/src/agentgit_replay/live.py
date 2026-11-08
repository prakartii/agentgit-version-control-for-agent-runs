"""A live stdio MCP server that a forked run continues against.

Used only in ``--live`` mode, for requests the recording cannot answer. The
upstream is started lazily and never sees the replayed part of the session, so
this relies on the stateless protocol revision, where every request carries its
own ``_meta`` (version and capabilities) and needs no prior handshake. A server
that needs an ``initialize`` handshake first would reject these requests; that
limit is documented, not worked around.
"""

import asyncio
import json
from typing import Any

from agentgit_replay.engine import REPLAY_ERROR_CODE

LIVE_TIMEOUT_SECONDS = 60.0
# Same limit the stdio framing layer uses for one message line.
MAX_LINE_BYTES = 16 * 1024 * 1024


def _error(msg_id: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": REPLAY_ERROR_CODE, "message": message},
    }


class LiveUpstream:
    def __init__(self, command: list[str], timeout: float = LIVE_TIMEOUT_SECONDS) -> None:
        self._command = command
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is None:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                limit=MAX_LINE_BYTES,
            )
        return self._proc

    async def ask(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Send one request; return notifications seen first, then the response.

        Never raises: failures become a JSON-RPC error response for that id, so
        the agent sees a failed tool call rather than a broken connection.
        """
        msg_id = message.get("id")
        try:
            return await asyncio.wait_for(self._exchange(message, msg_id), self._timeout)
        except TimeoutError:
            return [_error(msg_id, "live server did not answer in time")]
        except (OSError, ValueError) as e:
            return [_error(msg_id, f"live server unavailable: {type(e).__name__}")]

    async def _exchange(self, message: dict[str, Any], msg_id: Any) -> list[dict[str, Any]]:
        proc = await self._ensure()
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await proc.stdin.drain()
        seen: list[dict[str, Any]] = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                seen.append(_error(msg_id, "live server exited before answering"))
                return seen
            if not line.strip():
                continue
            parsed = json.loads(line)
            seen.append(parsed)
            if isinstance(parsed, dict) and "method" not in parsed and parsed.get("id") == msg_id:
                return seen

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
