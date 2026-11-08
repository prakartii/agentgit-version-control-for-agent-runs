from typing import Any

from helpers import RESERVED, call, exchange, live, record

from agentgit_proxy.redaction import Redactor
from agentgit_replay.engine import REPLAY_ERROR_CODE, ReplayEngine


def tool_params(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def text_of(reply: dict[str, Any]) -> str:
    return str(reply["result"]["content"][0]["text"])


def test_serves_the_recorded_result_with_the_live_request_id() -> None:
    repo, run_id = record([call(0, "read", "file contents", path="a.txt")])
    engine = ReplayEngine(repo, run_id)
    reply = engine.handle(live(77, "tools/call", tool_params("read", path="a.txt")))
    assert reply is not None
    assert reply["id"] == 77 and reply["jsonrpc"] == "2.0"
    assert text_of(reply) == "file contents"
    assert engine.finish().ok


def test_volatile_meta_and_ids_do_not_affect_matching() -> None:
    repo, run_id = record([call(0, "read", "x", path="a")])
    engine = ReplayEngine(repo, run_id)
    request = live(5, "tools/call", tool_params("read", path="a"), traceparent="00-abc-def-01")
    reply = engine.handle(request)
    assert reply is not None and "result" in reply and engine.finish().ok


def test_different_arguments_do_not_match() -> None:
    repo, run_id = record([call(0, "read", "x", path="a.txt")])
    engine = ReplayEngine(repo, run_id)
    reply = engine.handle(live(1, "tools/call", tool_params("read", path="OTHER.txt")))
    assert reply is not None and reply["error"]["code"] == REPLAY_ERROR_CODE
    report = engine.finish()
    kinds = sorted(d.kind for d in report.divergences)
    assert kinds == ["missing_request", "unexpected_request"] and not report.ok


def test_repeated_identical_calls_are_served_in_recorded_order() -> None:
    repo, run_id = record(
        [
            call(0, "poll", "first", n=1),
            call(1, "poll", "second", n=1),
            call(2, "poll", "third", n=1),
        ]
    )
    engine = ReplayEngine(repo, run_id)
    replies = [engine.handle(live(i, "tools/call", tool_params("poll", n=1))) for i in range(3)]
    assert [text_of(r) for r in replies if r] == ["first", "second", "third"]
    assert engine.finish().ok


def test_an_extra_repeat_beyond_the_recording_is_unexpected() -> None:
    repo, run_id = record([call(0, "poll", "only", n=1)])
    engine = ReplayEngine(repo, run_id)
    engine.handle(live(1, "tools/call", tool_params("poll", n=1)))
    reply = engine.handle(live(2, "tools/call", tool_params("poll", n=1)))
    assert reply is not None and "error" in reply
    assert [d.kind for d in engine.finish().divergences] == ["unexpected_request"]


def test_recorded_outcomes_are_replayed_as_recorded() -> None:
    repo, run_id = record(
        [
            exchange(0, params=tool_params("a"), response={"content": [], "isError": True},
                     outcome="tool_error"),
            exchange(1, params=tool_params("b"), response={"code": -32602, "message": "bad"},
                     outcome="protocol_error"),
            exchange(2, params=tool_params("c"), response=None, outcome="no_response"),
        ]
    )  # fmt: skip
    engine = ReplayEngine(repo, run_id)
    tool_error = engine.handle(live(1, "tools/call", tool_params("a")))
    protocol_error = engine.handle(live(2, "tools/call", tool_params("b")))
    unanswered = engine.handle(live(3, "tools/call", tool_params("c")))
    assert tool_error is not None and tool_error["result"]["isError"] is True
    assert protocol_error is not None
    assert protocol_error["error"] == {"code": -32602, "message": "bad"}
    assert unanswered is not None and "no response" in unanswered["error"]["message"]
    assert engine.finish().ok  # all three were exercised; replaying them is not a divergence


def test_recorded_server_info_is_reattached_to_results() -> None:
    response = {"content": [], "_meta": {RESERVED + "serverInfo": {"name": "srv", "version": "2"}}}
    repo, run_id = record([exchange(0, params=tool_params("a"), response=response)])
    reply = ReplayEngine(repo, run_id).handle(live(1, "tools/call", tool_params("a")))
    assert reply is not None
    assert reply["result"]["_meta"][RESERVED + "serverInfo"] == {"name": "srv", "version": "2"}


def test_requests_without_params_match() -> None:
    repo, run_id = record([exchange(0, "tools/list", None, {"tools": []})])
    engine = ReplayEngine(repo, run_id)
    reply = engine.handle(live(1, "tools/list"))
    assert reply is not None and reply["result"] == {"tools": []} and engine.finish().ok


def test_unrequested_recorded_steps_are_missing_requests() -> None:
    repo, run_id = record([call(0, "a", "x"), call(1, "b", "y"), call(2, "c", "z")])
    engine = ReplayEngine(repo, run_id)
    engine.handle(live(1, "tools/call", tool_params("a")))
    report = engine.finish()
    missing = [(d.kind, d.tool_name, d.position) for d in report.divergences]
    assert missing == [("missing_request", "b", 1), ("missing_request", "c", 2)]
    assert engine.finish().divergences == report.divergences  # finishing twice adds nothing


def test_out_of_order_is_reported_but_still_served() -> None:
    repo, run_id = record([call(0, "a", "A"), call(1, "b", "B")])
    engine = ReplayEngine(repo, run_id)
    first = engine.handle(live(1, "tools/call", tool_params("b")))
    second = engine.handle(live(2, "tools/call", tool_params("a")))
    assert first is not None and text_of(first) == "B"
    assert second is not None and text_of(second) == "A"
    report = engine.finish()
    assert [d.kind for d in report.divergences] == ["out_of_order"] and not report.ok


def test_allow_reorder_accepts_a_different_order() -> None:
    repo, run_id = record([call(0, "a", "A"), call(1, "b", "B")])
    engine = ReplayEngine(repo, run_id, allow_reorder=True)
    engine.handle(live(1, "tools/call", tool_params("b")))
    engine.handle(live(2, "tools/call", tool_params("a")))
    assert engine.finish().ok


def test_notifications_and_junk_get_no_reply_and_are_not_counted() -> None:
    repo, run_id = record([call(0, "a", "A")])
    engine = ReplayEngine(repo, run_id)
    assert engine.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None
    assert engine.handle({"jsonrpc": "2.0", "id": 1, "result": {}}) is None
    assert engine.handle({"id": True, "method": "tools/call"}) is None
    assert engine.finish().requests_received == 0


def test_live_secrets_match_their_redacted_recording() -> None:
    secret = "sk-abcdefghijklmnop1234"
    repo, run_id = record([call(0, "send", "sent", api_key=secret, note=f"use {secret}")])
    engine = ReplayEngine(repo, run_id)
    reply = engine.handle(
        live(1, "tools/call", tool_params("send", api_key=secret, note=f"use {secret}"))
    )
    assert reply is not None and text_of(reply) == "sent"
    report = engine.finish()
    assert report.ok and any("redacted" in w for w in report.warnings)


def test_runs_recorded_without_redaction_are_matched_without_redaction() -> None:
    off = Redactor(rules=[], sensitive_keys=[])
    secret = "sk-abcdefghijklmnop1234"
    repo, run_id = record(
        [call(0, "send", "ok", api_key=secret)], redactor=off, metadata={"redaction": "off"}
    )
    engine = ReplayEngine(repo, run_id)
    assert engine.handle(live(1, "tools/call", tool_params("send", api_key=secret))) is not None
    # With redaction wrongly applied, a different secret would collide; here it must not.
    other = engine.handle(
        live(2, "tools/call", tool_params("send", api_key="sk-zzzzzzzzzzzzzzzz9999"))
    )
    assert other is not None and "error" in other


def test_weak_recordings_produce_warnings() -> None:
    repo, run_id = record([call(0, "a", "A")], status="aborted")
    report = ReplayEngine(repo, run_id).finish()
    assert any("aborted" in w for w in report.warnings)


def test_server_originated_steps_are_not_replayed_or_missing() -> None:
    repo, run_id = record(
        [exchange(0, "sampling/createMessage", {"x": 1}, {"ok": 1}, origin="server"),
         call(1, "a", "A")]
    )  # fmt: skip
    engine = ReplayEngine(repo, run_id)
    engine.handle(live(1, "tools/call", tool_params("a")))
    report = engine.finish()
    assert report.ok and report.recorded_steps == 1


def test_report_serialises_and_summarises() -> None:
    repo, run_id = record([call(0, "a", "A"), call(1, "b", "B")])
    engine = ReplayEngine(repo, run_id)
    engine.handle(live(1, "tools/call", tool_params("a")))
    report = engine.finish()
    data = report.to_dict()
    assert data["ok"] is False and data["divergences"][0]["kind"] == "missing_request"
    assert "DIVERGED" in report.summary() and "missing_request" in report.summary()


def test_live_mode_hands_unrecorded_requests_back_without_flagging_divergence() -> None:
    from agentgit_replay.engine import PASSTHROUGH

    repo, run_id = record([call(0, "read", "x", path="a.txt")])
    engine = ReplayEngine(repo, run_id, live=True)
    recorded = engine.handle(live(1, "tools/call", tool_params("read", path="a.txt")))
    assert recorded is not None and text_of(recorded) == "x"  # still served from the recording
    new = engine.handle(live(2, "tools/call", tool_params("write", path="b.txt")))
    assert new is PASSTHROUGH
    report = engine.finish()
    assert report.ok and report.live == 1 and report.served == 1  # reported separately
    assert report.to_dict()["live_requests"] == 1
    assert "went to a live server" in report.summary()


def test_without_live_mode_the_same_request_is_a_divergence() -> None:
    repo, run_id = record([call(0, "read", "x", path="a.txt")])
    engine = ReplayEngine(repo, run_id)
    reply = engine.handle(live(2, "tools/call", tool_params("write", path="b.txt")))
    assert reply is not None and "error" in reply and not engine.finish().ok
