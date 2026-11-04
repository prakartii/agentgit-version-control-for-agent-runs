import copy

from agentgit_proxy.normalize import SessionInfo, filter_meta

RESERVED = "io.modelcontextprotocol/"


def test_reserved_and_volatile_meta_keys_are_removed() -> None:
    body = {
        "name": "read",
        "_meta": {
            RESERVED + "protocolVersion": "2026-07-28",
            RESERVED + "clientInfo": {"name": "c"},
            "progressToken": "p1",
            "traceparent": "00-abc-def-01",
        },
    }
    assert filter_meta(body) == {"name": "read"}


def test_unknown_third_party_meta_keys_are_kept() -> None:
    body = {"_meta": {"com.example/trace": 1, RESERVED + "clientInfo": {}, "progressToken": 3}}
    assert filter_meta(body) == {"_meta": {"com.example/trace": 1}}


def test_filter_does_not_mutate_input_and_ignores_non_objects() -> None:
    body = {"a": 1, "_meta": {"progressToken": 1}}
    snapshot = copy.deepcopy(body)
    filter_meta(body)
    assert body == snapshot
    for other in (None, 5, "x", [1], {"no": "meta"}, {"_meta": "not-a-dict"}):
        assert filter_meta(other) == other


def test_nested_meta_is_left_alone() -> None:
    body = {"content": [{"type": "text", "_meta": {"progressToken": 1}}]}
    assert filter_meta(body) == body


def test_session_info_learns_first_seen_values() -> None:
    info = SessionInfo()
    params = {
        "_meta": {
            RESERVED + "protocolVersion": "2026-07-28",
            RESERVED + "clientInfo": {"name": "client", "version": "1"},
        }
    }
    response = {"_meta": {RESERVED + "serverInfo": {"name": "srv", "version": "2"}}}
    info.learn(params, response)
    info.learn({"_meta": {RESERVED + "protocolVersion": "other"}}, None)
    assert info.protocol_version == "2026-07-28"
    assert info.client_info == {"name": "client", "version": "1"}
    assert info.server_info == {"name": "srv", "version": "2"}


def test_session_info_tolerates_missing_meta() -> None:
    info = SessionInfo()
    info.learn(None, None)
    info.learn({"x": 1}, {"y": 2})
    assert (info.protocol_version, info.client_info, info.server_info) == (None, None, None)
