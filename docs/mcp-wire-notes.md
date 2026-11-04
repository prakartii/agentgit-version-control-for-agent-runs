# MCP wire behaviour observed in the spike

Checked on 2026-11-04 against spec revision `2026-07-28` and `mcp==2.3.0`
(Python 3.12). These are observations, not guarantees; the integration tests
in `apps/proxy/tests` re-check the important ones.

## stdio (SDK `Client` to SDK `MCPServer`, through a logging tap)

- Framing: one JSON message per line, UTF-8, no embedded newlines.
- The client's first message is `server/discover` (no `initialize`).
- Then `tools/list` and `tools/call`, each with `params._meta` containing
  `io.modelcontextprotocol/protocolVersion`, `.../clientInfo` and
  `.../clientCapabilities`.
- Results include `resultType: "complete"`, `ttlMs`, `cacheScope` (list
  results) and `_meta["io.modelcontextprotocol/serverInfo"]`.
- A tool result carries both `content` (text) and `structuredContent`, and
  `isError: false`.
- Request ids were small integers (1, 2, 3) in call order.

## Streamable HTTP (`MCPServer.run("streamable-http", ...)`)

- Single endpoint `POST /mcp`.
- Request headers sent: `Accept: application/json, text/event-stream`,
  `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` (for `tools/call`).
- A plain `tools/call` reply was `200` with `Content-Type: application/json`
  and a `Content-Length`, in both the default and `json_response=True` modes.
  The proxy must still handle `text/event-stream` replies, which the spec
  allows when a server streams notifications.
- `GET /mcp` returned `400`.
- Constructor arguments `host` and `port` do not exist in 2.3.0; they are
  arguments of `run(...)`.

## Not yet observed

- An SSE reply with progress notifications.
- `subscriptions/listen`.
- A legacy (`initialize`-based) client or server.
- `resultType: "input_required"` round trips.
