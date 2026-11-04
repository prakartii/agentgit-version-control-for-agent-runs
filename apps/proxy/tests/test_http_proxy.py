import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore
from agentgit_proxy.http_proxy import create_app
from agentgit_proxy.sink import RecordingSession

PROGRESS = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":"p","progress":1}}\n\n'  # noqa: E501


class FakeUpstream:
    """A scriptable Streamable HTTP server that remembers what it received."""

    def __init__(self) -> None:
        self.received: list[tuple[str, dict[str, str], bytes]] = []
        self.app = Starlette(routes=[Route("/mcp", self.endpoint, methods=["GET", "POST"])])

    async def endpoint(self, request: Request) -> Response:
        body = await request.body()
        self.received.append((request.method, dict(request.headers), body))
        if request.method == "GET":
            return Response("legacy stream", media_type="text/plain")
        msg = json.loads(body)
        if "id" not in msg:
            return Response(status_code=202)  # notification
        args = (msg.get("params") or {}).get("arguments") or {}
        if "reply_raw" in args:
            return Response(args["reply_raw"].encode(), media_type="application/json")
        if args.get("error"):
            error = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": "x"}}
            return JSONResponse(error, status_code=400)
        if args.get("sse"):
            final = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [], "isError": False}}

            async def stream() -> AsyncIterator[bytes]:
                yield b": keep-alive\n\n"
                yield PROGRESS
                yield b"data: " + json.dumps(final).encode() + b"\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream")
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [], "isError": False}}
        return JSONResponse(reply)


def rpc(
    id_: int, name: str = "read", meta: dict[str, Any] | None = None, **arguments: Any
) -> bytes:
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    if meta:
        params["_meta"] = meta
    return json.dumps(
        {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": params}
    ).encode()


def make(
    repo: Repository | None = None, **kwargs: Any
) -> tuple[Starlette, FakeUpstream, Repository]:
    repo = repo or Repository(MemoryStore())
    upstream = FakeUpstream()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream.app))
    app = create_app("http://upstream/mcp", RecordingSession(repo), client=client, **kwargs)
    return app, upstream, repo


def stored_steps(app: Starlette, repo: Repository) -> list[Any]:
    result = app.state.proxy.result
    assert result is not None and result.run_id
    return [repo.get_step(s) for s in repo.get_run(result.run_id).steps]


def test_json_reply_is_forwarded_unchanged_and_recorded() -> None:
    app, upstream, repo = make()
    raw_reply = '{"id":1,"result":{"big":12345678901234567890,"x":2.50},"jsonrpc":"2.0"}'
    request = rpc(1, "read", reply_raw=raw_reply)
    with TestClient(app) as client:
        response = client.post(
            "/mcp", content=request, headers={"content-type": "application/json"}
        )
    assert response.status_code == 200
    assert response.content == raw_reply.encode()  # byte for byte
    assert upstream.received[0][2] == request  # upstream got the exact body
    (step,) = stored_steps(app, repo)
    assert (step.tool_name, step.outcome) == ("read", "ok")
    run = repo.get_run(app.state.proxy.result.run_id)
    assert run.metadata["transport"] == "http" and run.status == "complete"


def test_headers_are_forwarded_but_never_recorded() -> None:
    app, upstream, repo = make()
    secret = "Bearer abcdefghijklmnop1234"
    headers = {"authorization": secret, "mcp-session-id": "sess-123", "mcp-method": "tools/call"}
    with TestClient(app) as client:
        client.post("/mcp", content=rpc(1), headers=headers)
    sent = upstream.received[0][1]
    assert sent["authorization"] == secret and sent["mcp-session-id"] == "sess-123"
    assert sent["accept-encoding"] == "identity"  # the only header the proxy sets
    run_id = app.state.proxy.result.run_id
    everything = json.dumps(repo.get_run(run_id).metadata).encode()
    for step in stored_steps(app, repo):
        for blob in (step.arguments, step.result):
            if blob:
                everything += repo.get_blob(blob)
    assert b"abcdefghijklmnop1234" not in everything and b"sess-123" not in everything


def test_sse_reply_is_forwarded_unchanged_and_events_are_recorded() -> None:
    app, _, repo = make()
    request = rpc(1, "work", meta={"progressToken": "p"}, sse=True)
    with TestClient(app) as client:
        response = client.post("/mcp", content=request)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content.startswith(b": keep-alive\n\n" + PROGRESS)  # unchanged, in order
    (step,) = stored_steps(app, repo)
    assert step.outcome == "ok" and step.events
    events = json.loads(repo.get_blob(step.events))
    assert events[0]["method"] == "notifications/progress"


def test_http_error_with_json_rpc_body_is_a_protocol_error() -> None:
    app, _, repo = make()
    with TestClient(app) as client:
        response = client.post("/mcp", content=rpc(1, error=True))
    assert response.status_code == 400
    (step,) = stored_steps(app, repo)
    assert step.outcome == "protocol_error"


def test_notification_post_passes_through() -> None:
    app, upstream, _ = make()
    note = json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}
    ).encode()
    with TestClient(app) as client:
        response = client.post("/mcp", content=note)
    assert response.status_code == 202 and upstream.received[0][2] == note


def test_get_is_forwarded_but_not_recorded() -> None:
    app, upstream, repo = make()
    with TestClient(app) as client:
        response = client.get("/mcp")
    assert response.text == "legacy stream" and upstream.received[0][0] == "GET"
    assert stored_steps(app, repo) == []


@pytest.mark.parametrize(
    ("origin", "status"),
    [
        (None, 200),
        ("http://localhost:3000", 200),
        ("http://127.0.0.1:8080", 200),
        ("https://evil.example", 403),
        ("null", 403),
    ],
)
def test_origin_validation(origin: str | None, status: int) -> None:
    app, upstream, _ = make()
    headers = {"origin": origin} if origin else {}
    with TestClient(app) as client:
        assert client.post("/mcp", content=rpc(1), headers=headers).status_code == status
    assert bool(upstream.received) == (status == 200)  # a rejected request goes nowhere


def test_explicitly_allowed_origin_is_accepted() -> None:
    app, _, _ = make(allowed_origins=["https://app.example"])
    with TestClient(app) as client:
        response = client.post("/mcp", content=rpc(1), headers={"origin": "https://app.example"})
    assert response.status_code == 200


def test_unreachable_upstream_gives_502_and_is_recorded_as_unanswered() -> None:
    repo = Repository(MemoryStore())

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    app = create_app("http://upstream/mcp", RecordingSession(repo), client=client)
    with TestClient(app) as test_client:
        assert test_client.post("/mcp", content=rpc(1)).status_code == 502
    (step,) = stored_steps(app, repo)
    assert step.outcome == "no_response"
    assert repo.get_run(app.state.proxy.result.run_id).status == "aborted"


def test_storage_failure_never_breaks_the_session() -> None:
    class BrokenStore(MemoryStore):
        def put(self, object_type: str, payload: bytes) -> str:
            if object_type in {"blob", "step"}:
                raise OSError("disk full")
            return super().put(object_type, payload)

    app, _, repo = make(Repository(BrokenStore()))
    with TestClient(app) as client:
        response = client.post("/mcp", content=rpc(1))
    assert response.status_code == 200 and response.json()["id"] == 1
    assert repo.get_run(app.state.proxy.result.run_id).dropped_steps == 1
