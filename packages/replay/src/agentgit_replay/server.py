"""Serve a ReplayEngine over stdio, as a drop-in MCP server.

Newline-delimited JSON-RPC in, newline-delimited JSON-RPC out. Replies are
produced in request order, one at a time. The session ends when the client
closes stdin; the engine then reports what was and was not exercised.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from agentgit_proxy.framing import DEFAULT_MAX_LINE_BYTES, LineSplitter
from agentgit_replay.engine import ReplayEngine, ReplayReport

PARSE_ERROR = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}


def _encode(message: Any) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _answer(engine: ReplayEngine, line: bytes) -> bytes | None:
    try:
        message = json.loads(line)
    except ValueError:
        return _encode(PARSE_ERROR)
    if isinstance(message, list):
        replies = [r for m in message if isinstance(m, dict) and (r := engine.handle(m))]
        return _encode(replies) if replies else None
    if isinstance(message, dict):
        reply = engine.handle(message)
        return _encode(reply) if reply is not None else None
    return _encode(PARSE_ERROR)


async def serve_stdio(
    engine: ReplayEngine,
    client_chunks: AsyncIterator[bytes],
    client_write: Callable[[bytes], Awaitable[None]],
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> ReplayReport:
    """Answer requests until the client hangs up, then return the replay report."""
    splitter = LineSplitter(max_line_bytes)
    async for chunk in client_chunks:
        for line in splitter.feed(chunk):
            if line.strip():
                reply = _answer(engine, line)
                if reply is not None:
                    await client_write(reply)
    tail = splitter.finish()
    if tail and tail.strip():
        reply = _answer(engine, tail)
        if reply is not None:
            await client_write(reply)
    return engine.finish()
