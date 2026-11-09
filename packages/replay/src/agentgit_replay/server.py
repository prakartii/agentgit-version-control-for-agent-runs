"""Serve a ReplayEngine over stdio, as a drop-in MCP server.

Newline-delimited JSON-RPC in, newline-delimited JSON-RPC out. Replies are
produced in request order, one at a time. The session ends when the client
closes stdin; the engine then reports what was and was not exercised.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from agentgit_proxy.framing import DEFAULT_MAX_LINE_BYTES, LineSplitter
from agentgit_replay.engine import PASSTHROUGH, ReplayEngine, ReplayReport
from agentgit_replay.live import LiveUpstream

PARSE_ERROR = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}


def _encode(message: Any) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


async def _one(
    engine: ReplayEngine, message: dict[str, Any], upstream: LiveUpstream | None
) -> list[dict[str, Any]]:
    """Messages to send back for one request: normally 0 or 1; more if live notifications."""
    reply = engine.handle(message)
    if engine.last_delay_s:
        await asyncio.sleep(engine.last_delay_s)  # chaos: a slow tool
    if reply is PASSTHROUGH:
        # Only reachable in live mode, where the CLI always supplies an upstream.
        assert upstream is not None
        return await upstream.ask(message)
    return [reply] if reply is not None else []


async def _answer(engine: ReplayEngine, line: bytes, upstream: LiveUpstream | None) -> list[bytes]:
    try:
        message = json.loads(line)
    except ValueError:
        return [_encode(PARSE_ERROR)]
    if isinstance(message, list):
        replies: list[dict[str, Any]] = []
        for m in message:
            if isinstance(m, dict):
                replies.extend(r for r in await _one(engine, m, upstream) if "method" not in r)
        return [_encode(replies)] if replies else []
    if isinstance(message, dict):
        return [_encode(r) for r in await _one(engine, message, upstream)]
    return [_encode(PARSE_ERROR)]


async def serve_stdio(
    engine: ReplayEngine,
    client_chunks: AsyncIterator[bytes],
    client_write: Callable[[bytes], Awaitable[None]],
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    upstream: LiveUpstream | None = None,
) -> ReplayReport:
    """Answer requests until the client hangs up, then return the replay report."""
    splitter = LineSplitter(max_line_bytes)
    try:
        async for chunk in client_chunks:
            for line in splitter.feed(chunk):
                if line.strip():
                    for out in await _answer(engine, line, upstream):
                        await client_write(out)
        tail = splitter.finish()
        if tail and tail.strip():
            for out in await _answer(engine, tail, upstream):
                await client_write(out)
    finally:
        if upstream is not None:
            await upstream.close()
    return engine.finish()
