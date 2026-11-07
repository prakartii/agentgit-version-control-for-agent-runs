import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from helpers import call, live, record

from agentgit_replay.engine import ReplayEngine, ReplayReport
from agentgit_replay.server import serve_stdio


def line(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode() + b"\n"


def serve(chunks: list[bytes], engine: ReplayEngine) -> tuple[list[dict[str, Any]], ReplayReport]:
    out = bytearray()

    async def source() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    async def write(data: bytes) -> None:
        out.extend(data)

    report = asyncio.run(serve_stdio(engine, source(), write))
    return [json.loads(raw) for raw in bytes(out).splitlines()], report


def engine_for(*names: str) -> ReplayEngine:
    repo, run_id = record([call(i, n, f"result-{n}") for i, n in enumerate(names)])
    return ReplayEngine(repo, run_id)


def req(id_: int, name: str) -> dict[str, Any]:
    return live(id_, "tools/call", {"name": name, "arguments": {}})


def test_answers_each_request_with_its_recorded_result() -> None:
    replies, report = serve([line(req(1, "a")), line(req(2, "b"))], engine_for("a", "b"))
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["content"][0]["text"] == "result-b"
    assert report.ok


def test_arbitrary_chunking_does_not_change_the_replies() -> None:
    data = line(req(1, "a")) + line(req(2, "b"))
    chunks = [data[i : i + 7] for i in range(0, len(data), 7)]
    replies, report = serve(chunks, engine_for("a", "b"))
    assert [r["id"] for r in replies] == [1, 2] and report.ok


def test_a_final_message_without_a_newline_is_still_answered() -> None:
    replies, _ = serve([line(req(1, "a"))[:-1]], engine_for("a"))
    assert [r["id"] for r in replies] == [1]


def test_notifications_get_no_reply() -> None:
    note = line({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}})
    replies, _ = serve([note, line(req(1, "a"))], engine_for("a"))
    assert [r["id"] for r in replies] == [1]


def test_garbage_gets_a_parse_error_and_the_session_continues() -> None:
    replies, report = serve([b"not json\n", line(req(1, "a"))], engine_for("a"))
    assert replies[0]["error"]["code"] == -32700 and replies[0]["id"] is None
    assert replies[1]["id"] == 1 and report.ok


def test_a_batch_gets_a_batch_of_replies() -> None:
    batch = json.dumps([req(1, "a"), req(2, "b")]).encode() + b"\n"
    out = bytearray()

    async def source() -> AsyncIterator[bytes]:
        yield batch

    async def write(data: bytes) -> None:
        out.extend(data)

    asyncio.run(serve_stdio(engine_for("a", "b"), source(), write))
    assert [r["id"] for r in json.loads(bytes(out))] == [1, 2]


def test_the_report_covers_the_whole_session() -> None:
    _, report = serve([line(req(1, "a"))], engine_for("a", "b"))
    assert [d.kind for d in report.divergences] == ["missing_request"]
    assert (report.served, report.recorded_steps) == (1, 2)
