"""Pairs JSON-RPC requests with responses and attaches related notifications.

Pure state machine: bytes in, ``Exchange`` objects out. No I/O, no storage and
no redaction. It only *observes* copies of traffic, so nothing it does can
change what is forwarded, and it never raises on malformed input.
"""

import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agentgit_core.objects import StepOutcome

# Who sent the message. In the modern protocol only the client sends requests;
# older revisions let servers send requests too (sampling, roots, elicitation).
Direction = Literal["client", "server"]
RequestKey = tuple[Direction, int | str]


@dataclass
class Exchange:
    """One request and what became of it. Unredacted, not yet canonicalised."""

    seq: int  # order in which the request was first seen
    origin: Direction
    method: str
    params: Any
    outcome: StepOutcome
    response: Any  # the result or error object; None when there was no response
    events: list[dict[str, Any]]
    started_at_ms: int
    duration_ms: int


@dataclass
class Stats:
    """Counts of things the correlator could not or did not record."""

    malformed_messages: int = 0
    orphan_responses: int = 0
    unattached_notifications: int = 0


@dataclass
class _Pending:
    seq: int
    origin: Direction
    method: str
    params: Any
    started_at_ms: int
    started_mono_ms: int
    progress_token: Any
    events: list[dict[str, Any]] = field(default_factory=list)


def _now_wall_ms() -> int:
    return time.time_ns() // 1_000_000


def _now_mono_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class Correlator:
    def __init__(
        self,
        wall_ms: Callable[[], int] = _now_wall_ms,
        mono_ms: Callable[[], int] = _now_mono_ms,
    ) -> None:
        self._wall_ms = wall_ms
        self._mono_ms = mono_ms
        self._seq = 0
        # FIFO per key: a peer that wrongly reuses an in-flight id still
        # pairs responses in order instead of corrupting state.
        self._pending: dict[RequestKey, deque[_Pending]] = {}
        self.stats = Stats()

    # -- public ----------------------------------------------------------
    @staticmethod
    def request_key(raw: bytes, origin: Direction = "client") -> RequestKey | None:
        """Key of the request in ``raw``, or None if it is not a request.

        The HTTP adapter uses this to scope a response stream to its request.
        """
        try:
            message = json.loads(raw)
        except ValueError:
            return None
        if isinstance(message, dict) and "method" in message:
            return _key(origin, message.get("id"))
        return None

    def observe(
        self, sender: Direction, raw: bytes, attach_to: RequestKey | None = None
    ) -> list[Exchange]:
        """Observe one message (or JSON-RPC batch) sent by ``sender``.

        ``attach_to`` scopes a notification to a specific request when the
        transport knows it (an SSE stream belongs to one HTTP request).
        Returns exchanges completed by this message.
        """
        try:
            message = json.loads(raw)
        except ValueError:
            self.stats.malformed_messages += 1
            return []
        items = message if isinstance(message, list) else [message]
        done: list[Exchange] = []
        for item in items:
            if not isinstance(item, dict):
                self.stats.malformed_messages += 1
                continue
            exchange = self._observe_one(sender, item, attach_to)
            if exchange is not None:
                done.append(exchange)
        return done

    def finish(self) -> list[Exchange]:
        """End of session: every still-pending request becomes ``no_response``."""
        leftovers = [p for queue in self._pending.values() for p in queue]
        self._pending.clear()
        leftovers.sort(key=lambda p: p.seq)
        return [self._complete(p, "no_response", None, now_mono=self._mono_ms()) for p in leftovers]

    # -- internals -------------------------------------------------------
    def _observe_one(
        self, sender: Direction, msg: dict[str, Any], attach_to: RequestKey | None
    ) -> Exchange | None:
        method = msg.get("method")
        msg_id = msg.get("id")
        if isinstance(method, str):
            if _valid_id(msg_id):
                self._start(sender, method, msg)
            else:
                self._notification(method, msg, attach_to)
            return None
        if _valid_id(msg_id) and ("result" in msg or "error" in msg):
            return self._response(sender, msg)
        self.stats.malformed_messages += 1
        return None

    def _start(self, sender: Direction, method: str, msg: dict[str, Any]) -> None:
        params = msg.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        pending = _Pending(
            seq=self._seq,
            origin=sender,
            method=method,
            params=params,
            started_at_ms=self._wall_ms(),
            started_mono_ms=self._mono_ms(),
            progress_token=token,
        )
        self._seq += 1
        key: RequestKey = (sender, msg["id"])
        self._pending.setdefault(key, deque()).append(pending)

    def _response(self, sender: Direction, msg: dict[str, Any]) -> Exchange | None:
        # A response travels opposite to the request it answers.
        requester: Direction = "server" if sender == "client" else "client"
        key: RequestKey = (requester, msg["id"])
        queue = self._pending.get(key)
        if not queue:
            self.stats.orphan_responses += 1
            return None
        pending = queue.popleft()
        if not queue:
            del self._pending[key]
        if "error" in msg:
            return self._complete(pending, "protocol_error", msg["error"])
        result = msg["result"]
        outcome: StepOutcome = "ok"
        if pending.method == "tools/call" and isinstance(result, dict) and result.get("isError"):
            outcome = "tool_error"
        return self._complete(pending, outcome, result)

    def _notification(self, method: str, msg: dict[str, Any], attach_to: RequestKey | None) -> None:
        params = msg.get("params")
        target = self._find_target(method, params, attach_to)
        if target is None:
            self.stats.unattached_notifications += 1
            return
        target.events.append({"method": method, "params": params})

    def _find_target(
        self, method: str, params: Any, attach_to: RequestKey | None
    ) -> _Pending | None:
        if attach_to is not None:
            queue = self._pending.get(attach_to)
            return queue[0] if queue else None
        if not isinstance(params, dict):
            return None
        if method == "notifications/progress":
            token = params.get("progressToken")
            if token is not None:
                for queue in self._pending.values():
                    for pending in queue:
                        if pending.progress_token == token:
                            return pending
        elif method == "notifications/cancelled":
            request_id = params.get("requestId")
            # The cancelling side is the requester; try both directions.
            if _valid_id(request_id):
                for origin in ("client", "server"):
                    queue = self._pending.get((origin, request_id))  # type: ignore[arg-type]
                    if queue:
                        return queue[0]
        return None

    def _complete(
        self, pending: _Pending, outcome: StepOutcome, response: Any, now_mono: int | None = None
    ) -> Exchange:
        end = self._mono_ms() if now_mono is None else now_mono
        return Exchange(
            seq=pending.seq,
            origin=pending.origin,
            method=pending.method,
            params=pending.params,
            outcome=outcome,
            response=response,
            events=pending.events,
            started_at_ms=pending.started_at_ms,
            duration_ms=max(0, end - pending.started_mono_ms),
        )


def _valid_id(value: Any) -> bool:
    # JSON-RPC ids are strings or numbers; MCP forbids null. bool is an int in
    # Python but is not a valid id.
    return isinstance(value, str | int) and not isinstance(value, bool)


def _key(origin: Direction, msg_id: Any) -> RequestKey | None:
    return (origin, msg_id) if _valid_id(msg_id) else None
