from hypothesis import given
from hypothesis import strategies as st

from agentgit_proxy.framing import LineSplitter, SSEEvent, SSEParser


def split_at(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({c % (len(data) + 1) for c in cuts} | {0, len(data)})
    return [data[a:b] for a, b in zip(points, points[1:], strict=False)]


# ---- LineSplitter ------------------------------------------------------


def test_splits_complete_lines() -> None:
    assert LineSplitter().feed(b'{"a":1}\n{"b":2}\n') == [b'{"a":1}', b'{"b":2}']


def test_line_split_across_chunks() -> None:
    s = LineSplitter()
    assert s.feed(b'{"a"') == []
    assert s.feed(b":1}\n") == [b'{"a":1}']


def test_crlf_is_tolerated() -> None:
    assert LineSplitter().feed(b"x\r\n") == [b"x"]


def test_trailing_partial_line_is_returned_by_finish() -> None:
    s = LineSplitter()
    s.feed(b"complete\npartial")
    assert s.finish() == b"partial"
    assert s.finish() is None


def test_oversized_line_is_skipped_once_and_stream_recovers() -> None:
    s = LineSplitter(max_line_bytes=10)
    assert s.feed(b"x" * 25) == []
    assert s.feed(b"y" * 25 + b"\nshort\n") == [b"short"]
    assert s.oversized_lines == 1


def test_multibyte_utf8_split_across_chunks_is_intact() -> None:
    raw = '{"t":"héllo 世界"}\n'.encode()
    s = LineSplitter()
    lines = s.feed(raw[:8]) + s.feed(raw[8:])
    assert lines == [raw[:-1]]


@given(
    st.lists(st.binary(max_size=40).filter(lambda b: b"\n" not in b), max_size=8),
    st.lists(st.integers(min_value=0), max_size=10),
)
def test_chunking_never_changes_the_lines(payloads: list[bytes], cuts: list[int]) -> None:
    stream = b"".join(p + b"\n" for p in payloads)
    whole = LineSplitter().feed(stream)
    pieces = LineSplitter()
    chunked: list[bytes] = []
    for chunk in split_at(stream, cuts):
        chunked += pieces.feed(chunk)
    expected = [p[:-1] if p.endswith(b"\r") else p for p in payloads]
    assert whole == chunked == expected


# ---- SSEParser ---------------------------------------------------------


def test_sse_single_event() -> None:
    assert SSEParser().feed(b"data: hello\n\n") == [SSEEvent("message", "hello")]


def test_sse_named_event_with_id_and_multiline_data() -> None:
    raw = b"id: 7\nevent: message\ndata: a\ndata: b\n\n"
    assert SSEParser().feed(raw) == [SSEEvent("message", "a\nb", "7")]


def test_sse_comments_and_keepalives_are_ignored() -> None:
    assert SSEParser().feed(b":\n\n: keep-alive\n\ndata: x\n\n") == [SSEEvent("message", "x")]


def test_sse_crlf_and_cr_endings() -> None:
    assert SSEParser().feed(b"data: a\r\n\r\ndata: b\r\r") == [
        SSEEvent("message", "a"),
        SSEEvent("message", "b"),
    ]


def test_sse_crlf_split_between_chunks() -> None:
    p = SSEParser()
    events = p.feed(b"data: a\r") + p.feed(b"\n\r") + p.feed(b"\n")
    assert events == [SSEEvent("message", "a")]


def test_sse_event_split_across_chunks() -> None:
    p = SSEParser()
    assert p.feed(b"data: par") == []
    assert p.feed(b"tial\n") == []
    assert p.feed(b"\n") == [SSEEvent("message", "partial")]


@given(st.lists(st.integers(min_value=0), max_size=12))
def test_sse_chunking_never_changes_events(cuts: list[int]) -> None:
    stream = b"data: one\n\n:ka\n\nevent: x\ndata: two\ndata: 2b\n\nid: 9\ndata: three\n\n"
    whole = SSEParser().feed(stream)
    parser = SSEParser()
    chunked: list[SSEEvent] = []
    for chunk in split_at(stream, cuts):
        chunked += parser.feed(chunk)
    assert whole == chunked
    assert len(whole) == 3
