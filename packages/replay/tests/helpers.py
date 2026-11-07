"""Build recorded runs for tests using the real recording sink.

Going through RecordingSession (not hand-built steps) means these tests also
exercise the contract that matters: what the recorder stores is what replay
looks up.
"""

import asyncio
from typing import Any

from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore
from agentgit_proxy.correlator import Exchange
from agentgit_proxy.redaction import Redactor
from agentgit_proxy.sink import RecordingSession

RESERVED = "io.modelcontextprotocol/"


def exchange(
    seq: int,
    method: str = "tools/call",
    params: Any = None,
    response: Any = None,
    outcome: str = "ok",
    origin: str = "client",
) -> Exchange:
    return Exchange(
        seq=seq,
        origin=origin,  # type: ignore[arg-type]
        method=method,
        params=params,
        outcome=outcome,  # type: ignore[arg-type]
        response=response,
        events=[],
        started_at_ms=1_000 + seq,
        duration_ms=1,
    )


def call(seq: int, name: str, result: Any, **arguments: Any) -> Exchange:
    """A recorded tools/call with volatile _meta, as a real client sends it."""
    params = {
        "name": name,
        "arguments": arguments,
        "_meta": {RESERVED + "clientInfo": {"name": "client-A"}, "progressToken": "p-A"},
    }
    return exchange(seq, "tools/call", params, {"content": [{"type": "text", "text": result}]})


def record(
    exchanges: list[Exchange],
    *,
    redactor: Redactor | None = None,
    metadata: dict[str, str] | None = None,
    status: str = "complete",
) -> tuple[Repository, str]:
    repo = Repository(MemoryStore())
    session = RecordingSession(
        repo, redactor=redactor if redactor is not None else Redactor(), metadata=metadata or {}
    )

    async def go() -> str:
        await session.start()
        for ex in exchanges:
            session.submit(ex)
        result = await session.close(status)  # type: ignore[arg-type]
        assert result.run_id is not None
        return result.run_id

    return repo, asyncio.run(go())


def live(id_: int, method: str, params: Any = None, **meta: Any) -> dict[str, Any]:
    """A request as a *different* client session would send it (other ids and _meta)."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        params = dict(params)
        params["_meta"] = {
            RESERVED + "clientInfo": {"name": "client-B", "version": "9"},
            "progressToken": f"p-{id_}",
            **meta,
        }
        message["params"] = params
    return message
