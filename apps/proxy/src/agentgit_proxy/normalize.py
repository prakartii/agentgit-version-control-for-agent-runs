"""Turns observed message bodies into the content that gets recorded.

See docs/recording-format.md. Everything here works on copies; the bytes that
were forwarded are never involved.
"""

from dataclasses import dataclass
from typing import Any

# Per-request noise that would make identical calls look different.
VOLATILE_META_KEYS = frozenset({"progressToken", "traceparent", "tracestate", "baggage"})
# Every key under this prefix is reserved by MCP for protocol bookkeeping.
RESERVED_META_PREFIX = "io.modelcontextprotocol/"


def filter_meta(body: Any) -> Any:
    """Return ``body`` with volatile and protocol-reserved ``_meta`` keys removed.

    Only the top-level ``_meta`` is filtered. Unknown (third-party) keys are
    kept so extensions are not silently lost from the recording. An emptied
    ``_meta`` is dropped. The input is not modified.
    """
    if not isinstance(body, dict) or not isinstance(body.get("_meta"), dict):
        return body
    kept = {
        k: v
        for k, v in body["_meta"].items()
        if k not in VOLATILE_META_KEYS and not k.startswith(RESERVED_META_PREFIX)
    }
    out = {k: v for k, v in body.items() if k != "_meta"}
    if kept:
        out["_meta"] = kept
    return out


@dataclass
class SessionInfo:
    """Facts about the session, taken from the first messages that carry them."""

    protocol_version: str | None = None
    client_info: Any = None
    server_info: Any = None

    def learn(self, params: Any, response: Any) -> None:
        request_meta = _meta(params)
        if self.protocol_version is None:
            version = request_meta.get(RESERVED_META_PREFIX + "protocolVersion")
            if isinstance(version, str):
                self.protocol_version = version
        if self.client_info is None:
            self.client_info = request_meta.get(RESERVED_META_PREFIX + "clientInfo")
        if self.server_info is None:
            self.server_info = _meta(response).get(RESERVED_META_PREFIX + "serverInfo")


def _meta(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        meta = body.get("_meta")
        if isinstance(meta, dict):
            return meta
    return {}
