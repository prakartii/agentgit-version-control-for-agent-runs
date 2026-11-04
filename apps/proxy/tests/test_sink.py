import asyncio
import json
from typing import Any

from agentgit_core.hashing import hash_object
from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore
from agentgit_proxy.correlator import Exchange, Stats
from agentgit_proxy.redaction import Redactor
from agentgit_proxy.sink import RecordingSession

RESERVED = "io.modelcontextprotocol/"


def exchange(seq: int, *, name: str = "read", response: Any = None, **overrides: Any) -> Exchange:
    fields: dict[str, Any] = {
        "seq": seq,
        "origin": "client",
        "method": "tools/call",
        "params": {"name": name, "arguments": {"i": seq}, "_meta": {RESERVED + "clientInfo": {}}},
        "outcome": "ok",
        "response": {"content": [], "_meta": {RESERVED + "serverInfo": {"name": "srv"}}}
        if response is None
        else response,
        "events": [],
        "started_at_ms": 1_000 + seq,
        "duration_ms": 5,
    }
    fields.update(overrides)
    return Exchange(**fields)


def record(session: RecordingSession, exchanges: list[Exchange], status: str = "complete") -> Any:
    async def go() -> Any:
        await session.start()
        for ex in exchanges:
            session.submit(ex)
        return await session.close(status)  # type: ignore[arg-type]

    return asyncio.run(go())


def test_steps_are_stored_in_request_order_with_aligned_timings() -> None:
    repo = Repository(MemoryStore())
    session = RecordingSession(repo)
    # Completion order differs from request order.
    result = record(session, [exchange(2, name="c"), exchange(0, name="a"), exchange(1, name="b")])
    run = repo.get_run(result.run_id)
    assert [repo.get_step(s).tool_name for s in run.steps] == ["a", "b", "c"]
    assert [t.started_at_ms for t in run.timings] == [1000, 1001, 1002]
    assert run.status == "complete" and run.dropped_steps == 0 and not run.redacted


def test_blobs_hold_canonical_filtered_content() -> None:
    repo = Repository(MemoryStore())
    result = record(RecordingSession(repo), [exchange(0)])
    step = repo.get_step(repo.get_run(result.run_id).steps[0])
    assert step.arguments and step.result
    assert repo.get_blob(step.arguments) == b'{"arguments":{"i":0},"name":"read"}'
    assert repo.get_blob(step.result) == b'{"content":[]}'  # reserved _meta filtered


def test_identical_calls_share_step_ids_across_runs() -> None:
    # The property forks rely on: same content, same step id, whatever the timing.
    repo = Repository(MemoryStore())
    a = record(RecordingSession(repo), [exchange(0, started_at_ms=1, duration_ms=1)])
    b = record(RecordingSession(repo), [exchange(0, started_at_ms=999, duration_ms=500)])
    assert repo.get_run(a.run_id).steps == repo.get_run(b.run_id).steps
    assert a.run_id != b.run_id  # timings differ, so the runs differ


def test_redaction_applies_to_recording_and_is_flagged() -> None:
    repo = Repository(MemoryStore())
    secret = "sk-abcdefghijklmnop1234"
    ex = exchange(0, response={"content": [{"type": "text", "text": f"key={secret}"}]})
    original = json.dumps(ex.response)
    result = record(RecordingSession(repo, redactor=Redactor()), [ex])
    run = repo.get_run(result.run_id)
    step = repo.get_step(run.steps[0])
    assert step.result
    assert secret.encode() not in repo.get_blob(step.result)
    assert b"[REDACTED:api_key]" in repo.get_blob(step.result)
    assert run.redacted is True
    assert json.dumps(ex.response) == original  # the observed copy is untouched


def test_error_and_no_response_outcomes_are_recorded() -> None:
    repo = Repository(MemoryStore())
    exchanges = [
        exchange(0, outcome="protocol_error", response={"code": -32602, "message": "bad"}),
        exchange(1, outcome="no_response", response=None),
    ]
    exchanges[1].response = None
    run = repo.get_run(record(RecordingSession(repo), exchanges, status="aborted").run_id)
    outcomes = [repo.get_step(s) for s in run.steps]
    assert [s.outcome for s in outcomes] == ["protocol_error", "no_response"]
    assert outcomes[1].result is None
    assert run.status == "aborted"


def test_events_and_server_origin_are_recorded() -> None:
    repo = Repository(MemoryStore())
    events = [{"method": "notifications/progress", "params": {"progress": 1}}]
    ex = exchange(0, events=events, origin="server", method="sampling/createMessage")
    step = repo.get_step(repo.get_run(record(RecordingSession(repo), [ex]).run_id).steps[0])
    assert step.origin == "server" and step.events
    assert json.loads(repo.get_blob(step.events)) == events


def test_session_info_and_stats_go_into_metadata() -> None:
    repo = Repository(MemoryStore())
    ex = exchange(0)
    ex.params["_meta"][RESERVED + "protocolVersion"] = "2026-07-28"

    async def go() -> Any:
        session = RecordingSession(repo, metadata={"server": "files"})
        await session.start()
        session.submit(ex)
        return await session.close("complete", Stats(malformed_messages=2), {"extra": "1"})

    meta = repo.get_run(asyncio.run(go()).run_id).metadata
    assert meta["server"] == "files" and meta["protocol_version"] == "2026-07-28"
    assert json.loads(meta["server_info"]) == {"name": "srv"}
    assert meta["malformed_messages"] == "2" and meta["extra"] == "1"
    assert "orphan_responses" not in meta  # zero counts are not noise


# ---- failure handling: recording must never break the session ---------------


class FailingStore(MemoryStore):
    """Rejects writes of the chosen object types."""

    def __init__(self, fail_types: set[str]) -> None:
        super().__init__()
        self.fail_types = fail_types

    def put(self, object_type: str, payload: bytes) -> str:
        if object_type in self.fail_types:
            raise OSError("disk full")
        return super().put(object_type, payload)


def test_step_storage_failure_is_counted_and_does_not_raise() -> None:
    repo = Repository(FailingStore({"step"}))
    result = record(RecordingSession(repo), [exchange(0), exchange(1)])
    run = repo.get_run(result.run_id)
    assert result.dropped_steps == 2 and run.dropped_steps == 2
    assert run.steps == ()


def test_partial_failure_keeps_the_good_steps_and_counts_the_rest() -> None:
    class FailsOnce(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def put(self, object_type: str, payload: bytes) -> str:
            if object_type == "step":
                self.calls += 1
                if self.calls == 2:
                    raise OSError("transient")
            return super().put(object_type, payload)

    repo = Repository(FailsOnce())
    result = record(RecordingSession(repo), [exchange(0), exchange(1), exchange(2)])
    run = repo.get_run(result.run_id)
    assert len(run.steps) == 2 and run.dropped_steps == 1


def test_run_storage_failure_returns_no_run_id_without_raising() -> None:
    repo = Repository(FailingStore({"run"}))
    result = record(RecordingSession(repo), [exchange(0)])
    assert result.run_id is None


def test_index_failure_keeps_the_stored_run() -> None:
    class BrokenIndex:
        def index_run(self, run_id: str) -> None:
            raise RuntimeError("postgres is down")

    repo = Repository(MemoryStore())
    session = RecordingSession(repo, index=BrokenIndex())  # type: ignore[arg-type]
    result = record(session, [exchange(0)])
    assert result.run_id is not None and result.indexed is False
    assert repo.get_run(result.run_id).steps


def test_full_queue_drops_and_counts_without_blocking_or_raising() -> None:
    repo = Repository(MemoryStore())

    async def go() -> Any:
        session = RecordingSession(repo, queue_size=2)
        # No consumer started: the queue fills up immediately.
        for i in range(5):
            session.submit(exchange(i))
        await session.start()
        return await session.close("complete")

    result = asyncio.run(go())
    assert result.dropped_steps == 3
    assert len(repo.get_run(result.run_id).steps) == 2


def test_submit_after_close_does_not_raise() -> None:
    repo = Repository(MemoryStore())

    async def go() -> None:
        session = RecordingSession(repo, queue_size=1)
        await session.start()
        await session.close("complete")
        session.submit(exchange(0))
        session.submit(exchange(1))

    asyncio.run(go())


def test_run_hash_helper_matches_store() -> None:
    # Guards the test helper assumptions: blobs are stored under the blob type.
    repo = Repository(MemoryStore())
    blob = repo.put_blob(b"x")
    assert blob == hash_object("blob", b"x")


def test_slow_index_does_not_hold_up_the_end_of_the_session() -> None:
    import time

    class SlowIndex:
        def index_run(self, run_id: str) -> None:
            time.sleep(1.0)

    repo = Repository(MemoryStore())
    session = RecordingSession(repo, index=SlowIndex(), index_timeout=0.1)  # type: ignore[arg-type]

    async def go() -> tuple[Any, float]:
        await session.start()
        session.submit(exchange(0))
        started = time.monotonic()
        result = await session.close("complete")
        return result, time.monotonic() - started

    result, waited = asyncio.run(go())
    assert result.run_id is not None and result.indexed is False
    assert waited < 0.9  # close() returned without waiting for the slow index


# ---- journal integration ---------------------------------------------------------


def test_graceful_close_leaves_no_journal(tmp_path: Any) -> None:
    from agentgit_proxy.journal import Journal, journals_in

    repo = Repository(MemoryStore())
    result = record(RecordingSession(repo, journal=Journal(tmp_path)), [exchange(0)])
    assert result.run_id is not None
    assert journals_in(tmp_path) == []


def test_crash_before_close_is_recoverable_from_the_journal(tmp_path: Any) -> None:
    from agentgit_proxy.journal import Journal, recover

    repo = Repository(MemoryStore())
    journal = Journal(tmp_path)
    secret = "sk-abcdefghijklmnop1234"
    ex = exchange(1, response={"content": [{"type": "text", "text": secret}]})
    ex.params["_meta"][RESERVED + "protocolVersion"] = "2026-07-28"

    async def crash() -> None:
        session = RecordingSession(repo, journal=journal, metadata={"server": "files"})
        await session.start()
        session.submit(exchange(2, name="later"))
        session.submit(ex)
        session.submit(exchange(0, name="earlier"))
        await asyncio.sleep(0.5)  # let the sink persist, then "die": no close()
        journal.release()  # the OS would drop the lock when the process disappears

    asyncio.run(crash())

    (recovery,) = recover(tmp_path, repo)
    run = repo.get_run(recovery.run_id)  # type: ignore[arg-type]
    assert len(run.steps) == 3 and run.status == "aborted"
    assert run.redacted is True  # the redaction flag survived the crash
    assert run.metadata["server"] == "files"
    assert run.metadata["protocol_version"] == "2026-07-28"
    assert run.metadata["recovered_from_journal"] == "true"
    assert [t.started_at_ms for t in run.timings] == sorted(t.started_at_ms for t in run.timings)


def test_failed_run_write_keeps_the_journal_for_recovery(tmp_path: Any) -> None:
    from agentgit_proxy.journal import Journal, journals_in, recover

    class OnceFailingRuns(MemoryStore):
        fail = True

        def put(self, object_type: str, payload: bytes) -> str:
            if object_type == "run" and self.fail:
                raise OSError("disk full")
            return super().put(object_type, payload)

    store = OnceFailingRuns()
    repo = Repository(store)
    result = record(RecordingSession(repo, journal=Journal(tmp_path)), [exchange(0)])
    assert result.run_id is None
    assert len(journals_in(tmp_path)) == 1  # not deleted: the steps are still reachable

    store.fail = False  # the disk problem is fixed
    (recovery,) = recover(tmp_path, repo)
    assert recovery.run_id is not None and recovery.steps == 1
    assert journals_in(tmp_path) == []
