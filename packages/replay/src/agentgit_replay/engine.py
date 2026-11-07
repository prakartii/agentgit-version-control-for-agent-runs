"""The replay engine: answer requests from a recorded run, and report divergence.

Pure logic, no I/O: a request (a parsed JSON-RPC message) goes in, a reply (or
nothing, for notifications) comes out. ``server.py`` puts it behind stdio.

Matching (ADR-006): a request is normalised exactly as the recorder normalised
it, hashed, and served by the first unconsumed recorded step with the same
``(method, params blob id)``. Nothing is fuzzy and nothing falls back to a live
server.
"""

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

from agentgit_core.hashing import canonical_json
from agentgit_core.objects import Run, Step, blob_id
from agentgit_core.repo import Repository
from agentgit_proxy.normalize import RESERVED_META_PREFIX, normalize_body
from agentgit_proxy.redaction import Redactor

DivergenceKind = Literal["unexpected_request", "out_of_order", "missing_request"]

# JSON-RPC "internal error": what the agent sees when replay cannot answer.
REPLAY_ERROR_CODE = -32603


@dataclass(frozen=True)
class Divergence:
    kind: DivergenceKind
    method: str
    tool_name: str | None
    # Recorded step position for out_of_order / missing_request; for an
    # unexpected request, the 0-based count of requests received so far.
    position: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "method": self.method,
            "tool_name": self.tool_name,
            "position": self.position,
            "detail": self.detail,
        }


@dataclass
class ReplayReport:
    run_id: str
    recorded_steps: int
    requests_received: int
    served: int
    divergences: list[Divergence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if the replay was faithful: no divergence of any kind."""
        return not self.divergences

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "recorded_steps": self.recorded_steps,
            "requests_received": self.requests_received,
            "served": self.served,
            "divergences": [d.to_dict() for d in self.divergences],
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        lines = [
            f"replay of {self.run_id[:12]}: "
            f"{'FAITHFUL' if self.ok else 'DIVERGED'} "
            f"({self.served}/{self.recorded_steps} recorded steps served, "
            f"{self.requests_received} requests received)"
        ]
        lines += [f"  warning: {w}" for w in self.warnings]
        for d in self.divergences:
            tool = f" {d.tool_name}" if d.tool_name else ""
            lines.append(f"  {d.kind}: {d.method}{tool} - {d.detail}")
        return "\n".join(lines)


@dataclass
class _Recorded:
    position: int  # index among the replayable (client-origin) steps
    step: Step
    consumed: bool = False


class ReplayEngine:
    def __init__(
        self,
        repo: Repository,
        run_id: str,
        *,
        allow_reorder: bool = False,
        redactor: Redactor | None = None,
    ) -> None:
        self._repo = repo
        self._run_id = run_id
        self._allow_reorder = allow_reorder
        run = repo.get_run(run_id)
        self._run = run
        self._redactor = redactor if redactor is not None else _redactor_for(run)

        self._recorded: list[_Recorded] = []
        for step_id in run.steps:
            step = repo.get_step(step_id)
            if step.origin == "client":  # server-originated requests are not replayed
                self._recorded.append(_Recorded(len(self._recorded), step))

        # (method, params blob id) -> recorded steps in order, for repeated calls.
        self._queues: dict[tuple[str, str | None], deque[_Recorded]] = defaultdict(deque)
        for rec in self._recorded:
            self._queues[(rec.step.method, rec.step.arguments)].append(rec)

        self._received = 0
        self._served = 0
        self._divergences: list[Divergence] = []
        self._server_info = _server_info(run)
        self._warnings = _warnings_for(run)

    # -- public ---------------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message. Returns None for notifications."""
        method = message.get("method")
        msg_id = message.get("id")
        if not isinstance(method, str) or not _is_id(msg_id):
            return None  # notifications, responses and junk get no reply

        self._received += 1
        params = message.get("params")
        key = (method, self._params_blob_id(params))
        queue = self._queues.get(key)
        if not queue:
            self._diverge(
                "unexpected_request",
                method,
                _tool_name(method, params),
                self._received - 1,
                "no unconsumed recorded step matches this request",
            )
            return _error(msg_id, "replay divergence: no recorded response for this request")

        rec = queue.popleft()
        rec.consumed = True
        self._served += 1
        first_pending = self._first_pending()
        if (
            first_pending is not None
            and first_pending.position < rec.position
            and not self._allow_reorder
        ):
            self._diverge(
                "out_of_order",
                method,
                rec.step.tool_name,
                rec.position,
                f"recorded step {first_pending.position} "
                f"({first_pending.step.method}) was expected first",
            )
        return self._reply(msg_id, rec.step)

    def finish(self) -> ReplayReport:
        """End of session: every recorded step never requested is a divergence."""
        for rec in self._recorded:
            if not rec.consumed:
                self._diverge(
                    "missing_request",
                    rec.step.method,
                    rec.step.tool_name,
                    rec.position,
                    "recorded, but the agent never made this request",
                )
                rec.consumed = True  # report each missing step once
        return ReplayReport(
            run_id=self._run_id,
            recorded_steps=len(self._recorded),
            requests_received=self._received,
            served=self._served,
            divergences=list(self._divergences),
            warnings=list(self._warnings),
        )

    # -- internals ------------------------------------------------------------
    def _params_blob_id(self, params: Any) -> str | None:
        if params is None:
            return None
        normalised = normalize_body(params, self._redactor)
        return blob_id(canonical_json(normalised))

    def _first_pending(self) -> _Recorded | None:
        return next((r for r in self._recorded if not r.consumed), None)

    def _diverge(
        self, kind: DivergenceKind, method: str, tool: str | None, position: int, detail: str
    ) -> None:
        self._divergences.append(Divergence(kind, method, tool, position, detail))

    def _reply(self, msg_id: Any, step: Step) -> dict[str, Any]:
        if step.outcome == "no_response":
            return _error(msg_id, "replay: this request had no response when it was recorded")
        body = json.loads(self._repo.get_blob(step.result)) if step.result else {}
        if step.outcome == "protocol_error":
            return {"jsonrpc": "2.0", "id": msg_id, "error": body}
        if self._server_info is not None and isinstance(body, dict):
            meta = {**body.get("_meta", {}), RESERVED_META_PREFIX + "serverInfo": self._server_info}
            body = {**body, "_meta": meta}
        return {"jsonrpc": "2.0", "id": msg_id, "result": body}


# ---- helpers --------------------------------------------------------------------


def _redactor_for(run: Run) -> Redactor:
    """Redact live requests with the same setting the run was recorded with."""
    if run.metadata.get("redaction") == "off":
        return Redactor(rules=[], sensitive_keys=[])
    return Redactor()


def _server_info(run: Run) -> Any:
    raw = run.metadata.get("server_info")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _warnings_for(run: Run) -> list[str]:
    warnings = []
    if run.status != "complete":
        warnings.append(f"the recorded run is '{run.status}'; replay may diverge near its end")
    if run.dropped_steps:
        warnings.append(f"the recording is missing {run.dropped_steps} exchange(s)")
    if run.metadata.get("recovered_from_journal") == "true":
        warnings.append("the run was recovered from a journal; in-flight requests are not in it")
    if run.redacted:
        warnings.append("the recording was redacted; matches are made on redacted content")
    return warnings


def _is_id(value: Any) -> bool:
    return isinstance(value, str | int) and not isinstance(value, bool)


def _tool_name(method: str, params: Any) -> str | None:
    if method == "tools/call" and isinstance(params, dict):
        name = params.get("name")
        return name if isinstance(name, str) else None
    return None


def _error(msg_id: int | str | None, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": REPLAY_ERROR_CODE, "message": message},
    }
