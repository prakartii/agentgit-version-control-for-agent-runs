"""Property tests: replay is faithful for *any* recorded run, not just examples.

Recording goes through the real sink with redaction disabled, so the properties
isolate matching and ordering. Redaction is covered in test_engine.py.
"""

import copy
from typing import Any

from helpers import RESERVED, exchange, live, record
from hypothesis import given, settings
from hypothesis import strategies as st

from agentgit_core.hashing import canonical_json
from agentgit_proxy.normalize import filter_meta
from agentgit_proxy.redaction import Redactor
from agentgit_replay.engine import ReplayEngine

OFF = Redactor(rules=[], sensitive_keys=[])

# A small alphabet makes repeated identical calls common, which is the hard case.
scalars = st.none() | st.booleans() | st.integers(-3, 3) | st.sampled_from(["a", "b", "é"])
params_st = st.dictionaries(
    st.sampled_from(["name", "path", "n"]), scalars | st.lists(scalars, max_size=2), max_size=3
)
result_st = st.dictionaries(
    st.sampled_from(["content", "ok", "n"]), scalars | st.lists(scalars, max_size=2), max_size=3
)
methods = st.sampled_from(["tools/call", "tools/list", "resources/read"])
spec_st = st.tuples(methods, params_st, result_st)
specs_st = st.lists(spec_st, min_size=1, max_size=8)


def build(specs: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> tuple[Any, str]:
    exchanges = [
        exchange(i, method, {**params, "_meta": {RESERVED + "clientInfo": {"name": "rec"}}}, result)
        for i, (method, params, result) in enumerate(specs)
    ]
    return record(exchanges, redactor=OFF, metadata={"redaction": "off"})


def key_of(method: str, params: dict[str, Any]) -> tuple[str, bytes]:
    return method, canonical_json(filter_meta(params))


def replay(engine: ReplayEngine, order: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    replies = []
    for i, (method, params) in enumerate(order):
        reply = engine.handle(live(i, method, params))
        assert reply is not None and reply["id"] == i
        replies.append(reply)
    return replies


@given(specs_st)
@settings(max_examples=60, deadline=None)
def test_replaying_in_order_reproduces_every_recorded_result(specs: list[Any]) -> None:
    repo, run_id = build(specs)
    engine = ReplayEngine(repo, run_id)
    replies = replay(engine, [(m, p) for m, p, _ in specs])
    for reply, (_, _, result) in zip(replies, specs, strict=True):
        assert "result" in reply
        assert canonical_json(reply["result"]) == canonical_json(filter_meta(result))
    report = engine.finish()
    assert report.ok and report.served == len(specs)


@given(specs_st, st.data())
@settings(max_examples=60, deadline=None)
def test_any_reordering_still_gets_correct_per_call_results(specs: list[Any], data: Any) -> None:
    repo, run_id = build(specs)
    order = data.draw(st.permutations(list(range(len(specs)))))
    live_calls = [(specs[i][0], specs[i][1]) for i in order]

    # The k-th live call for a key must get the k-th recorded result for that key.
    recorded: dict[tuple[str, bytes], list[bytes]] = {}
    for method, params, result in specs:
        recorded.setdefault(key_of(method, params), []).append(canonical_json(filter_meta(result)))
    seen: dict[tuple[str, bytes], int] = {}

    engine = ReplayEngine(repo, run_id)
    for reply, (method, params) in zip(replay(engine, live_calls), live_calls, strict=True):
        key = key_of(method, params)
        expected = recorded[key][seen.get(key, 0)]
        seen[key] = seen.get(key, 0) + 1
        assert canonical_json(reply["result"]) == expected

    report = engine.finish()
    assert {d.kind for d in report.divergences} <= {"out_of_order"}  # nothing lost or invented
    same_sequence = [key_of(m, p) for m, p in live_calls] == [key_of(m, p) for m, p, _ in specs]
    assert report.ok == same_sequence


@given(specs_st, params_st)
@settings(max_examples=60, deadline=None)
def test_a_request_that_was_never_recorded_is_always_flagged(specs: list[Any], extra: Any) -> None:
    repo, run_id = build(specs)
    extra = {**extra, "never_recorded": "zz"}  # guaranteed to differ from every recorded call
    engine = ReplayEngine(repo, run_id)
    ordered = [(m, p) for m, p, _ in specs]
    replay(engine, ordered)
    reply = engine.handle(live(99, "tools/call", extra))
    assert reply is not None and "error" in reply
    report = engine.finish()
    assert [d.kind for d in report.divergences] == ["unexpected_request"]


@given(specs_st, st.data())
@settings(max_examples=60, deadline=None)
def test_stopping_early_reports_exactly_the_unrequested_steps(specs: list[Any], data: Any) -> None:
    repo, run_id = build(specs)
    k = data.draw(st.integers(0, len(specs)))
    engine = ReplayEngine(repo, run_id)
    replay(engine, [(m, p) for m, p, _ in specs[:k]])
    report = engine.finish()
    assert [d.kind for d in report.divergences] == ["missing_request"] * (len(specs) - k)
    assert [d.position for d in report.divergences] == list(range(k, len(specs)))


@given(specs_st)
@settings(max_examples=40, deadline=None)
def test_replay_does_not_mutate_the_requests_it_is_given(specs: list[Any]) -> None:
    repo, run_id = build(specs)
    engine = ReplayEngine(repo, run_id)
    requests = [live(i, m, p) for i, (m, p, _) in enumerate(specs)]
    snapshot = copy.deepcopy(requests)
    for request in requests:
        engine.handle(request)
    assert requests == snapshot
