"""Persists exchanges as steps and, at the end, a run.

Design rules (ADR-003):

* Recording must never break the agent's session. ``submit`` never raises and
  never blocks; every storage error is caught, logged and counted.
* Nothing is hidden. Exchanges that could not be stored are counted in the
  run's ``dropped_steps``; ``redacted`` and ``status`` are stored on the run.
* Redaction runs here, on the recorded copy only.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agentgit_core.hashing import canonical_json
from agentgit_core.objects import Run, RunStatus, Step, StepTiming
from agentgit_core.repo import Repository
from agentgit_index.service import IndexService
from agentgit_proxy.correlator import Exchange, Stats
from agentgit_proxy.normalize import SessionInfo, filter_meta
from agentgit_proxy.redaction import Redactor

log = logging.getLogger("agentgit.proxy")

DEFAULT_QUEUE_SIZE = 10_000


@dataclass(frozen=True)
class RunResult:
    run_id: str | None  # None if the run object itself could not be stored
    indexed: bool
    dropped_steps: int


@dataclass
class _Stored:
    seq: int
    step_id: str
    timing: StepTiming


@dataclass
class RecordingSession:
    repo: Repository
    redactor: Redactor = field(default_factory=Redactor)
    index: IndexService | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    queue_size: int = DEFAULT_QUEUE_SIZE

    def __post_init__(self) -> None:
        self._queue: asyncio.Queue[Exchange | None] = asyncio.Queue(self.queue_size)
        self._task: asyncio.Task[None] | None = None
        self._stored: list[_Stored] = []
        self._info = SessionInfo()
        self._dropped = 0
        self._redacted = False
        self._closed = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._consume(), name="agentgit-sink")

    # -- hot path: called while forwarding, must be cheap and safe ---------
    def submit(self, exchange: Exchange) -> None:
        if self._closed:
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(exchange)
        except Exception:  # queue full, or the session was already closed
            self._dropped += 1
            log.warning("recording queue full or closed; dropped one exchange")

    # -- background persistence ----------------------------------------------
    async def _consume(self) -> None:
        while True:
            exchange = await self._queue.get()
            if exchange is None:
                return
            try:
                # Blocking storage calls run in a thread, off the event loop.
                await asyncio.to_thread(self._persist, exchange)
            except Exception:
                self._dropped += 1
                log.exception("failed to record an exchange; continuing")

    def _persist(self, ex: Exchange) -> None:
        self._info.learn(ex.params, ex.response)
        before = self.redactor.replacements
        params = self.redactor.redact(filter_meta(ex.params))
        response = self.redactor.redact(filter_meta(ex.response))
        events = self.redactor.redact(
            [{"method": e["method"], "params": filter_meta(e["params"])} for e in ex.events]
        )
        if self.redactor.replacements != before:
            self._redacted = True

        tool_name = None
        if ex.method == "tools/call" and isinstance(params, dict):
            name = params.get("name")
            tool_name = name if isinstance(name, str) else None

        step = Step(
            method=ex.method,
            origin=ex.origin,
            outcome=ex.outcome,
            tool_name=tool_name,
            arguments=None if params is None else self._blob(params),
            result=None if response is None else self._blob(response),
            events=self._blob(events) if events else None,
        )
        step_id = self.repo.put_step(step)
        self._stored.append(
            _Stored(
                ex.seq,
                step_id,
                StepTiming(started_at_ms=ex.started_at_ms, duration_ms=ex.duration_ms),
            )
        )

    def _blob(self, value: Any) -> str:
        return self.repo.put_blob(canonical_json(value))

    # -- end of session ------------------------------------------------------
    async def close(
        self,
        status: RunStatus,
        stats: Stats | None = None,
        extra_metadata: dict[str, str] | None = None,
        drain_timeout: float = 30.0,
    ) -> RunResult:
        """Flush pending exchanges, write the run and index it. Never raises."""
        self._closed = True  # later submissions are counted as dropped
        if self._task is not None:
            try:
                # Wait for room: the consumer is running, so a full queue drains.
                await asyncio.wait_for(self._queue.put(None), drain_timeout)
                await asyncio.wait_for(self._task, drain_timeout)
            except Exception:
                log.warning("recording did not drain before the timeout")
                self._task.cancel()
                # Whatever was still queued is lost; say so.
                self._dropped += self._queue.qsize()

        ordered = sorted(self._stored, key=lambda s: s.seq)
        metadata = dict(self.metadata)
        metadata.update(self._session_metadata(stats))
        metadata.update(extra_metadata or {})
        run = Run(
            steps=tuple(s.step_id for s in ordered),
            timings=tuple(s.timing for s in ordered),
            metadata=metadata,
            status=status,
            redacted=self._redacted,
            dropped_steps=self._dropped,
        )
        try:
            run_id = await asyncio.to_thread(self.repo.put_run, run)
        except Exception:
            log.exception("failed to store the run object")
            return RunResult(None, False, self._dropped)
        indexed = False
        if self.index is not None:
            try:
                await asyncio.to_thread(self.index.index_run, run_id)
                indexed = True
            except Exception:
                log.exception("failed to index run %s (objects are stored)", run_id)
        return RunResult(run_id, indexed, self._dropped)

    def _session_metadata(self, stats: Stats | None) -> dict[str, str]:
        out: dict[str, str] = {}
        if self._info.protocol_version:
            out["protocol_version"] = self._info.protocol_version
        if self._info.client_info is not None:
            out["client_info"] = json.dumps(self._info.client_info, sort_keys=True)
        if self._info.server_info is not None:
            out["server_info"] = json.dumps(self._info.server_info, sort_keys=True)
        if stats is not None:
            for name in ("malformed_messages", "orphan_responses", "unattached_notifications"):
                count = getattr(stats, name)
                if count:
                    out[name] = str(count)
        return out
