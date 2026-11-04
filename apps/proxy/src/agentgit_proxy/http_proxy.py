"""Transparent Streamable HTTP proxy.

One MCP endpoint (default ``/mcp``). Every POST is forwarded to the upstream
server and the reply is streamed back chunk by chunk, unchanged. A copy of the
request body and of the reply (a JSON object, or SSE events) is fed to the
correlator. Other methods (GET, DELETE) and other paths are forwarded without
being recorded; they exist only for earlier protocol revisions.

Choices worth knowing about:

* ``Accept-Encoding: identity`` is sent upstream so replies are never
  compressed and can be observed without decoding. This is the only request
  header the proxy changes.
* ``Origin`` is validated (DNS-rebinding protection, required by the spec for
  servers and sensible for a local proxy). Headers, including ``Authorization``
  and ``Mcp-Session-Id``, are forwarded but never recorded.
* Closing the client connection closes the upstream stream, which is how the
  spec cancels a request over HTTP.
"""

import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agentgit_core.objects import RunStatus
from agentgit_proxy.correlator import Correlator, RequestKey
from agentgit_proxy.framing import SSEParser
from agentgit_proxy.sink import RecordingSession, RunResult

log = logging.getLogger("agentgit.proxy")

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
# Hop-by-hop headers (RFC 9110 7.6.1) are meaningful for one connection only.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
MAX_JSON_REPLY_BYTES = 64 * 1024 * 1024


@dataclass
class HttpProxyState:
    upstream_url: str
    session: RecordingSession
    client: httpx.AsyncClient
    correlator: Correlator = field(default_factory=Correlator)
    allowed_origins: frozenset[str] = frozenset()
    oversized_replies: int = 0
    result: RunResult | None = None


def create_app(
    upstream_url: str,
    session: RecordingSession,
    *,
    path: str = "/mcp",
    allowed_origins: Iterable[str] = (),
    client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Build the proxy application. ``client`` can be injected for in-process tests."""
    state = HttpProxyState(
        upstream_url=upstream_url,
        session=session,
        client=client or httpx.AsyncClient(timeout=None),
        allowed_origins=frozenset(o.lower() for o in allowed_origins),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await session.start()
        try:
            yield
        finally:
            unanswered = state.correlator.finish()
            for exchange in unanswered:
                session.submit(exchange)
            extra = {"transport": "http", "upstream_host": urlsplit(upstream_url).netloc}
            if state.oversized_replies:
                extra["oversized_messages"] = str(state.oversized_replies)
            status: RunStatus = "aborted" if unanswered else "complete"
            state.result = await session.close(status, state.correlator.stats, extra)
            await state.client.aclose()

    async def endpoint(request: Request) -> Response:
        return await _handle(request, state)

    app = Starlette(
        routes=[Route(path, endpoint, methods=["GET", "POST", "DELETE", "OPTIONS"])],
        lifespan=lifespan,
    )
    app.state.proxy = state
    return app


async def _handle(request: Request, state: HttpProxyState) -> Response:
    if not _origin_allowed(request.headers.get("origin"), state.allowed_origins):
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Origin not allowed"}},
            status_code=403,
        )

    body = await request.body()
    is_post = request.method == "POST"
    key: RequestKey | None = None
    if is_post:
        key = Correlator.request_key(body)
        try:
            # Observe the request before forwarding it, so its response can never
            # be seen first.
            state.correlator.observe("client", body)
        except Exception:
            log.exception("tap failed; forwarding is unaffected")

    headers = _forward_headers(request.headers.items())
    headers["accept-encoding"] = "identity"
    upstream_request = state.client.build_request(
        request.method, state.upstream_url, headers=headers, content=body
    )
    try:
        upstream = await state.client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        log.warning("upstream request failed: %s", exc)
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Upstream unavailable"}},
            status_code=502,
        )

    content_type = upstream.headers.get("content-type", "")
    observe = is_post and (content_type.startswith(("application/json", "text/event-stream")))
    stream = _stream_and_tap(upstream, state, key, content_type) if observe else _raw(upstream)
    return StreamingResponse(
        stream,
        status_code=upstream.status_code,
        headers=_forward_headers(upstream.headers.items()),
    )


async def _raw(upstream: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    finally:
        await upstream.aclose()


async def _stream_and_tap(
    upstream: httpx.Response,
    state: HttpProxyState,
    key: RequestKey | None,
    content_type: str,
) -> AsyncIterator[bytes]:
    """Yield the upstream body unchanged while observing a copy of it."""
    sse = SSEParser() if content_type.startswith("text/event-stream") else None
    buffered = bytearray()
    overflow = False
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk  # forwarding comes first; observation can never delay it
            try:
                if sse is not None:
                    for event in sse.feed(chunk):
                        _observe(state, event.data.encode(), key)
                elif not overflow:
                    buffered.extend(chunk)
                    if len(buffered) > MAX_JSON_REPLY_BYTES:
                        overflow = True
                        buffered.clear()
                        state.oversized_replies += 1
            except Exception:
                log.exception("tap failed; forwarding is unaffected")
        if sse is None and buffered:
            _observe(state, bytes(buffered), key)
    finally:
        await upstream.aclose()  # closing the stream is how HTTP cancels a request


def _observe(state: HttpProxyState, data: bytes, key: RequestKey | None) -> None:
    for exchange in state.correlator.observe("server", data, attach_to=key):
        state.session.submit(exchange)


def _forward_headers(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {k: v for k, v in items if k.lower() not in HOP_BY_HOP}


def _origin_allowed(origin: str | None, allowed: frozenset[str]) -> bool:
    if origin is None:  # non-browser clients send no Origin
        return True
    if origin.lower() in allowed:
        return True
    host = urlsplit(origin).hostname or ""
    return host in LOOPBACK_HOSTS


async def serve(
    app: Starlette, host: str = "127.0.0.1", port: int = 8765, log_level: str = "warning"
) -> None:
    """Serve the proxy until interrupted (Ctrl+C or SIGTERM)."""
    if host not in LOOPBACK_HOSTS:
        log.warning("binding to %s exposes the proxy beyond this machine", host)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level))
    await server.serve()
