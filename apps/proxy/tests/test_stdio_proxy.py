import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore
from agentgit_proxy.redaction import Redactor
from agentgit_proxy.sink import RecordingSession
from agentgit_proxy.stdio_proxy import StdioProxyResult, run_stdio_proxy

FAKE_SERVER = str(Path(__file__).with_name("fake_server.py"))


def call(id_: int, name: str = "read", **arguments: Any) -> bytes:
    msg = {
        "jsonrpc": "2.0",
        "id": id_,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return json.dumps(msg).encode() + b"\n"


class FakeClient:
    """Plays the agent: feeds scripted bytes in, collects what the proxy returns."""

    def __init__(self, chunks: list[bytes], hold_open: float = 0.0, chunk_size: int | None = None):
        self.output = bytearray()
        self._chunks = chunks
        self._hold_open = hold_open
        self._chunk_size = chunk_size
        self.first_output = asyncio.Event()

    async def chunks(self) -> AsyncIterator[bytes]:
        for data in self._chunks:
            step = self._chunk_size or len(data) or 1
            for i in range(0, len(data), step):
                yield data[i : i + step]
                await asyncio.sleep(0)
        # Stay connected until the server has answered, then hang up (EOF).
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.first_output.wait(), self._hold_open or 5)

    async def write(self, data: bytes) -> None:
        self.output.extend(data)
        self.first_output.set()


def run(
    client: FakeClient,
    *,
    repo: Repository | None = None,
    env: dict[str, str] | None = None,
    command: list[str] | None = None,
    **kwargs: Any,
) -> tuple[StdioProxyResult, Repository]:
    repo = repo or Repository(MemoryStore())
    session = RecordingSession(repo, redactor=kwargs.pop("redactor", Redactor()))

    async def go() -> StdioProxyResult:
        import os

        full_env = {**os.environ, **(env or {})}
        return await run_stdio_proxy(
            command or [sys.executable, FAKE_SERVER],
            client.chunks(),
            client.write,
            session,
            env=full_env,
            grace_seconds=kwargs.pop("grace_seconds", 3.0),
            **kwargs,
        )

    return asyncio.run(go()), repo


def steps_of(repo: Repository, result: StdioProxyResult) -> list[Any]:
    assert result.run.run_id
    return [repo.get_step(s) for s in repo.get_run(result.run.run_id).steps]


def test_roundtrip_is_forwarded_and_recorded(capfd: pytest.CaptureFixture[str]) -> None:
    client = FakeClient([call(1, "read", path="a.txt")])
    result, repo = run(client)

    reply = json.loads(bytes(client.output))
    assert reply["id"] == 1 and reply["result"]["content"][0]["text"] == "echo:read"
    assert result.exit_code == 0

    (step,) = steps_of(repo, result)
    assert (step.method, step.tool_name, step.outcome) == ("tools/call", "read", "ok")
    run_obj = repo.get_run(result.run.run_id)  # type: ignore[arg-type]
    assert run_obj.status == "complete" and run_obj.dropped_steps == 0
    assert run_obj.metadata["transport"] == "stdio"
    # The server's stderr reaches ours untouched.
    assert "fake-server-started" in capfd.readouterr().err


def test_forwarded_bytes_are_identical_in_both_directions(tmp_path: Path) -> None:
    # Odd but legal JSON: key order, spacing, unicode escapes, 64-bit ints,
    # duplicate keys and unknown fields. None of it may be normalised in transit.
    log = tmp_path / "received.log"
    request = (
        b'{ "params" : {"name":"read","arguments":{"reply_raw":'
        + json.dumps(
            '{"id":1,   "result":{"big":12345678901234567890,'
            '"u":"\\u00e9\\ud83d\\ude00","dup":1,"dup":2},'
            '"jsonrpc":"2.0","futureField":[1,2.50]}'
        ).encode()
        + b'},"unknown":{"x":null}},"method":"tools/call","id":1,"jsonrpc":"2.0"}\n'
    )
    client = FakeClient([request])
    result, _ = run(client, env={"RECEIVED_LOG": str(log)})

    expected_reply = (
        b'{"id":1,   "result":{"big":12345678901234567890,"u":"\\u00e9\\ud83d\\ude00",'
        b'"dup":1,"dup":2},"jsonrpc":"2.0","futureField":[1,2.50]}\n'
    )
    assert bytes(client.output) == expected_reply  # server -> agent, bit for bit
    assert log.read_bytes() == request  # agent -> server, bit for bit
    assert result.exit_code == 0


def test_chunking_does_not_change_forwarding_or_recording(tmp_path: Path) -> None:
    log = tmp_path / "received.log"
    request = call(1, "read", path="é世界.txt")
    client = FakeClient([request], chunk_size=3)  # tiny chunks, split inside UTF-8 sequences
    result, repo = run(client, env={"RECEIVED_LOG": str(log)})
    assert log.read_bytes() == request
    (step,) = steps_of(repo, result)
    assert step.outcome == "ok"


def test_recording_is_redacted_but_forwarding_is_not(tmp_path: Path) -> None:
    secret = "sk-abcdefghijklmnop1234"
    log = tmp_path / "received.log"
    request = call(1, "send", api_key=secret, note=f"use {secret}")
    client = FakeClient([request])
    result, repo = run(client, env={"RECEIVED_LOG": str(log)})

    assert secret.encode() in log.read_bytes()  # the server got the real secret
    (step,) = steps_of(repo, result)
    assert step.arguments
    recorded = repo.get_blob(step.arguments)
    assert secret.encode() not in recorded
    assert repo.get_run(result.run.run_id).redacted is True  # type: ignore[arg-type]


def test_progress_notifications_are_recorded_as_events() -> None:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "work",
            "arguments": {"progress": True},
            "_meta": {"progressToken": "p"},
        },
    }
    client = FakeClient([json.dumps(msg).encode() + b"\n"])
    # Hold the connection until the final response (the second server line).
    result, repo = run(client, grace_seconds=3.0)
    (step,) = steps_of(repo, result)
    assert step.events
    events = json.loads(repo.get_blob(step.events))
    assert events[0]["method"] == "notifications/progress"
    assert events[0]["params"]["progress"] == 1


def test_tool_error_and_protocol_error_outcomes() -> None:
    client = FakeClient([call(1, "a", is_error=True), call(2, "b", error=True)])
    result, repo = run(client)
    outcomes = {s.tool_name: s.outcome for s in steps_of(repo, result)}
    assert outcomes == {"a": "tool_error", "b": "protocol_error"}


def test_server_crash_mid_request_is_recorded_as_aborted() -> None:
    client = FakeClient([call(1, "boom", crash=True)])
    result, repo = run(client)
    assert result.exit_code == 3
    (step,) = steps_of(repo, result)
    assert step.outcome == "no_response" and step.result is None
    assert repo.get_run(result.run.run_id).status == "aborted"  # type: ignore[arg-type]


def test_client_hangup_with_pending_request_is_aborted() -> None:
    client = FakeClient([call(1, "slow", sleep=5)], hold_open=0.3)
    result, repo = run(client, grace_seconds=0.5)
    (step,) = steps_of(repo, result)
    assert step.outcome == "no_response"
    run_obj = repo.get_run(result.run.run_id)  # type: ignore[arg-type]
    assert run_obj.status == "aborted"
    assert run_obj.metadata["terminated_by_proxy"] == "true"


def test_server_that_ignores_eof_is_terminated() -> None:
    client = FakeClient([call(1)])
    result, repo = run(client, env={"IGNORE_EOF": "1"}, grace_seconds=0.5)
    run_obj = repo.get_run(result.run.run_id)  # type: ignore[arg-type]
    assert run_obj.metadata["terminated_by_proxy"] == "true"
    assert run_obj.status == "complete"  # every request was answered


def test_spawn_failure_is_reported_and_recorded() -> None:
    client = FakeClient([call(1)])
    result, repo = run(client, command=["definitely-not-a-real-binary-xyz"])
    assert result.exit_code == 127
    run_obj = repo.get_run(result.run.run_id)  # type: ignore[arg-type]
    assert run_obj.status == "aborted" and run_obj.steps == ()
    assert "spawn_error" in run_obj.metadata


def test_storage_failure_never_breaks_the_session() -> None:
    class BrokenStore(MemoryStore):
        def put(self, object_type: str, payload: bytes) -> str:
            if object_type in {"blob", "step"}:
                raise OSError("disk full")
            return super().put(object_type, payload)

    client = FakeClient([call(1, "a"), call(2, "b")])
    result, repo = run(client, repo=Repository(BrokenStore()))

    replies = [json.loads(line) for line in bytes(client.output).splitlines()]
    assert {r["id"] for r in replies} == {1, 2}  # the agent saw both answers
    assert result.exit_code == 0
    run_obj = repo.get_run(result.run.run_id)  # type: ignore[arg-type]
    assert run_obj.dropped_steps == 2 and run_obj.steps == ()


def test_large_message_roundtrips_intact(tmp_path: Path) -> None:
    log = tmp_path / "received.log"
    request = call(1, "bulk", blob="x" * 1_000_000)
    client = FakeClient([request])
    result, repo = run(client, env={"RECEIVED_LOG": str(log)})
    assert log.read_bytes() == request
    (step,) = steps_of(repo, result)
    assert step.arguments and len(repo.get_blob(step.arguments)) > 1_000_000


def test_oversized_message_is_forwarded_but_reported_as_unrecorded(tmp_path: Path) -> None:
    log = tmp_path / "received.log"
    request = call(1, "bulk", blob="x" * 5_000)
    client = FakeClient([request])
    result, repo = run(client, env={"RECEIVED_LOG": str(log)}, max_line_bytes=1_000)
    assert log.read_bytes() == request  # still delivered in full
    assert steps_of(repo, result) == []  # but not recorded
    meta = repo.get_run(result.run.run_id).metadata  # type: ignore[arg-type]
    assert meta["oversized_messages"] == "1"
