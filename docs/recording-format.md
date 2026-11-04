# Recording format

How one MCP session becomes a `Run` (see ADR-003). The recording is a
*canonical content record*, not a wire capture. What the agent and server
exchange is forwarded byte for byte; only the recorded copy is normalised.

## Objects

- **Blob**: canonical JSON bytes of one payload (request params, result, error
  or event list).
- **Step**: `method`, `tool_name`, `arguments` (blob id), `result` (blob id),
  `outcome`, `events` (blob id). No position, no timing, no ids from the wire.
- **Run**: ordered `steps`, aligned `timings`, plus `metadata`, `status`,
  `redacted` and `dropped_steps`.

## What goes into the blobs

| Blob | Content |
|---|---|
| `arguments` | the request `params`, with `_meta` filtered (below) |
| `result` | the response `result`, or the response `error` object, with `_meta` filtered |
| `events` | the request-scoped notifications seen before the response, in order, each as `{method, params}` |

JSON-RPC `id` values are never recorded: they depend on call order and would
make identical calls look different.

### `_meta` filtering

Removed because they are connection identity or per-request noise:
`progressToken`, `traceparent`, `tracestate`, `baggage`, and every key with
the reserved prefix `io.modelcontextprotocol/` (protocol version, client and
server info, capabilities, subscription id, log level). If `_meta` ends up
empty it is removed. **Other keys, including unknown third-party extension
keys, are kept.**

The protocol version and the first-seen client and server info are stored in
the run metadata (`protocol_version`, `client_info`, `server_info`).

### Normalisation

Blobs are canonical JSON (sorted keys, no whitespace, UTF-8). This means that
in the *recording only*: object key order is not preserved, duplicate keys
collapse to the last value, and numbers are re-spelled by value (`1e3`
becomes `1000.0`). Integers of any size are preserved. Forwarded bytes are
never touched.

## Outcomes

| Outcome | Meaning |
|---|---|
| `ok` | a result was returned (for `tools/call`, `isError` was absent or false) |
| `tool_error` | a `tools/call` result had `isError: true` |
| `protocol_error` | the response was a JSON-RPC `error` |
| `no_response` | the request was seen but the session ended before a reply |

## Run completeness

Nothing is implied; the run says what it is.

- `status`: `complete` if the session ended normally, `aborted` if the
  upstream crashed, the client disconnected mid-request or the proxy was
  interrupted.
- `dropped_steps`: exchanges seen but not stored because of a storage error.
  Non-zero means the recording has gaps and cannot be replayed faithfully.
- `redacted`: true if redaction changed any recorded value. A redacted run
  replays placeholders, not the original data.

## Run metadata keys

`recovered_from_journal` (`true` if rebuilt by `agentgit recover` after a hard
kill; such runs are `aborted` and omit requests that were in flight), `server` (label of the upstream), `session_id` (shared by the proxies of one
agent session), `transport` (`stdio` or `http`), `protocol_version`,
`client_info`, `server_info`, `proxy_version`.

## What is not recorded

- Anything the proxy did not see: the agent's calls to its LLM never pass
  through MCP.
- HTTP headers, including `Authorization` and `Mcp-Session-Id`.
- The upstream process environment and arguments (only a label is stored).
- stderr output of a stdio server.
