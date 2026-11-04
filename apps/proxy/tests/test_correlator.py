import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from agentgit_proxy.correlator import Correlator


def msg(**fields: Any) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **fields}).encode()


def request(id_: int | str, method: str = "tools/call", **params: Any) -> bytes:
    return msg(id=id_, method=method, params=params)


def result(id_: int | str, **body: Any) -> bytes:
    return msg(id=id_, result=body)


class FakeClock:
    """Deterministic time source so durations are exact."""

    def __init__(self) -> None:
        self.now = 1_000

    def wall(self) -> int:
        return self.now

    def mono(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


def make() -> tuple[Correlator, FakeClock]:
    clock = FakeClock()
    return Correlator(wall_ms=clock.wall, mono_ms=clock.mono), clock


def test_request_then_response_makes_one_exchange() -> None:
    c, clock = make()
    assert c.observe("client", request(1, name="read", arguments={"p": "a"})) == []
    clock.advance(25)
    (ex,) = c.observe("server", result(1, content=[]))
    assert (ex.method, ex.outcome, ex.seq) == ("tools/call", "ok", 0)
    assert ex.params == {"name": "read", "arguments": {"p": "a"}}
    assert ex.response == {"content": []}
    assert (ex.started_at_ms, ex.duration_ms) == (1_000, 25)


def test_json_rpc_error_is_a_protocol_error() -> None:
    c, _ = make()
    c.observe("client", request(1))
    (ex,) = c.observe("server", msg(id=1, error={"code": -32602, "message": "bad"}))
    assert ex.outcome == "protocol_error"
    assert ex.response == {"code": -32602, "message": "bad"}


def test_is_error_result_is_a_tool_error_only_for_tool_calls() -> None:
    c, _ = make()
    c.observe("client", request(1))
    (call,) = c.observe("server", result(1, isError=True, content=[]))
    assert call.outcome == "tool_error"
    c.observe("client", request(2, method="tools/list"))
    (listing,) = c.observe("server", result(2, isError=True))
    assert listing.outcome == "ok"


def test_responses_may_arrive_out_of_order_but_seq_follows_requests() -> None:
    c, _ = make()
    c.observe("client", request(1, name="slow"))
    c.observe("client", request(2, name="fast"))
    (second,) = c.observe("server", result(2))
    (first,) = c.observe("server", result(1))
    assert (second.seq, second.params["name"]) == (1, "fast")
    assert (first.seq, first.params["name"]) == (0, "slow")


def test_int_and_string_ids_are_distinct() -> None:
    c, _ = make()
    c.observe("client", request(1))
    assert c.observe("server", result("1")) == []
    assert c.stats.orphan_responses == 1
    assert len(c.observe("server", result(1))) == 1


def test_bool_and_null_ids_are_not_valid_requests() -> None:
    c, _ = make()
    c.observe("client", msg(id=True, method="tools/call"))
    c.observe("client", msg(id=None, method="tools/call"))
    assert c.finish() == []  # neither became a pending request


def test_progress_notification_attaches_by_token() -> None:
    c, _ = make()
    c.observe("client", request(1, _meta={"progressToken": "t1"}))
    progress = msg(method="notifications/progress", params={"progressToken": "t1", "progress": 1})
    c.observe("server", progress)
    (ex,) = c.observe("server", result(1))
    assert ex.events == [
        {"method": "notifications/progress", "params": {"progressToken": "t1", "progress": 1}}
    ]


def test_notification_with_explicit_scope_attaches_to_that_request() -> None:
    c, _ = make()
    c.observe("client", request(1))
    c.observe("client", request(2))
    note = msg(method="notifications/message", params={"data": "x"})
    c.observe("server", note, attach_to=("client", 2))
    (a,) = c.observe("server", result(1))
    (b,) = c.observe("server", result(2))
    assert a.events == [] and len(b.events) == 1


def test_cancellation_is_attached_and_request_ends_without_response() -> None:
    c, _ = make()
    c.observe("client", request(7))
    c.observe("client", msg(method="notifications/cancelled", params={"requestId": 7}))
    (ex,) = c.finish()
    assert ex.outcome == "no_response"
    assert ex.events[0]["method"] == "notifications/cancelled"


def test_unrelated_notification_is_counted_not_recorded() -> None:
    c, _ = make()
    c.observe("server", msg(method="notifications/tools/list_changed"))
    assert c.stats.unattached_notifications == 1


def test_finish_reports_pending_requests_in_request_order() -> None:
    c, clock = make()
    c.observe("client", request(1, name="a"))
    clock.advance(10)
    c.observe("client", request(2, name="b"))
    clock.advance(5)
    leftovers = c.finish()
    assert [e.params["name"] for e in leftovers] == ["a", "b"]
    assert all(e.outcome == "no_response" and e.response is None for e in leftovers)
    assert [e.duration_ms for e in leftovers] == [15, 5]
    assert c.finish() == []


def test_malformed_input_never_raises() -> None:
    c, _ = make()
    junk_inputs = [b"not json", b"", b"\xff\xfe", b"123", b'"str"', b"[1,2]", b'{"id":1}', b"{}"]
    for junk in junk_inputs:
        assert c.observe("client", junk) == []
    assert c.stats.malformed_messages >= 6


def test_batch_is_handled_element_by_element() -> None:
    c, _ = make()
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "x"}},
    ]
    c.observe("client", json.dumps(batch).encode())
    replies = [{"jsonrpc": "2.0", "id": 2, "result": {}}, {"jsonrpc": "2.0", "id": 1, "result": {}}]
    done = c.observe("server", json.dumps(replies).encode())
    assert [e.method for e in done] == ["tools/call", "tools/list"]


def test_server_initiated_request_in_legacy_protocols_is_paired() -> None:
    c, _ = make()
    c.observe("server", request(5, method="sampling/createMessage"))
    (ex,) = c.observe("client", result(5))
    assert (ex.origin, ex.method) == ("server", "sampling/createMessage")


def test_reused_in_flight_id_pairs_in_order() -> None:
    c, _ = make()
    c.observe("client", request(1, name="first"))
    c.observe("client", request(1, name="second"))
    (a,) = c.observe("server", result(1))
    (b,) = c.observe("server", result(1))
    assert (a.params["name"], b.params["name"]) == ("first", "second")


def test_unknown_fields_are_kept_in_params_and_response() -> None:
    c, _ = make()
    c.observe("client", request(1, name="x", futureField={"a": [1, 2]}))
    (ex,) = c.observe("server", result(1, resultType="complete", newThing=True))
    assert ex.params["futureField"] == {"a": [1, 2]}
    assert ex.response["newThing"] is True


def test_request_key_helper() -> None:
    assert Correlator.request_key(request(3)) == ("client", 3)
    assert Correlator.request_key(result(3)) is None
    assert Correlator.request_key(b"junk") is None


@given(st.permutations(list(range(8))))
def test_any_interleaving_pairs_every_request_exactly_once(order: list[int]) -> None:
    """N requests answered in any order yield N correct exchanges."""
    c, _ = make()
    for i in range(8):
        c.observe("client", request(i, name=f"t{i}"))
    done = []
    for i in order:
        done += c.observe("server", result(i, echo=i))
    assert len(done) == 8
    for ex in done:
        assert ex.params["name"] == f"t{ex.seq}"
        assert ex.response == {"echo": ex.seq}
    assert c.finish() == []
