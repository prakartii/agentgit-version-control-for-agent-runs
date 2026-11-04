"""Incremental parsers used to *observe* traffic.

The proxy forwards raw chunks as they arrive and feeds a copy to these
parsers. Parsing never delays or alters forwarding.
"""

from dataclasses import dataclass

DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024


class LineSplitter:
    """Splits a byte stream into newline-delimited lines (MCP stdio framing).

    Chunk boundaries are arbitrary: a line may arrive in many pieces, and a
    chunk may hold many lines. A line longer than ``max_line_bytes`` is not
    buffered; it is reported once as ``oversized`` and its remaining bytes
    are skipped, so memory stays bounded while forwarding is unaffected.
    """

    def __init__(self, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> None:
        self._max = max_line_bytes
        self._buf = bytearray()
        self._skipping = False  # inside an oversized line, discarding until "\n"
        self.oversized_lines = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        """Return the complete lines in ``chunk`` (without the terminator)."""
        lines: list[bytes] = []
        while chunk:
            newline = chunk.find(b"\n")
            if newline == -1:
                self._append(chunk)
                break
            self._append(chunk[:newline])
            if self._skipping:
                self._skipping = False
            else:
                lines.append(self._take())
            self._buf.clear()
            chunk = chunk[newline + 1 :]
        return lines

    def _append(self, part: bytes) -> None:
        if self._skipping:
            return
        if len(self._buf) + len(part) > self._max:
            self._buf.clear()
            self._skipping = True
            self.oversized_lines += 1
            return
        self._buf.extend(part)

    def _take(self) -> bytes:
        line = bytes(self._buf)
        # Tolerate CRLF from non-conforming peers; the spec says "\n".
        return line[:-1] if line.endswith(b"\r") else line

    def finish(self) -> bytes | None:
        """Return a trailing partial line (no terminator) at end of stream."""
        if self._skipping or not self._buf:
            return None
        line = self._take()
        self._buf.clear()
        return line


@dataclass(frozen=True)
class SSEEvent:
    event: str  # "message" when the stream gave no event name
    data: str
    id: str | None = None


class SSEParser:
    """Incremental Server-Sent Events parser (WHATWG rules).

    Handles ``\n``, ``\r\n`` and ``\r`` line endings, multi-line ``data``,
    comment lines (``:``) such as keep-alives, and events split across chunks.
    """

    def __init__(self) -> None:
        self._pending = b""
        self._event = ""
        self._data: list[str] = []
        self._id: str | None = None
        self._skip_lf = False  # the previous chunk ended in "\r"

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        if self._skip_lf and chunk.startswith(b"\n"):
            chunk = chunk[1:]
        self._skip_lf = False
        data = self._pending + chunk
        events: list[SSEEvent] = []
        start = 0
        i = 0
        while i < len(data):
            byte = data[i]
            if byte not in (0x0A, 0x0D):
                i += 1
                continue
            line = data[start:i]
            if byte == 0x0D:
                if i + 1 == len(data):
                    self._skip_lf = True  # a "\n" may open the next chunk
                elif data[i + 1] == 0x0A:
                    i += 1
            i += 1
            start = i
            event = self._line(line.decode("utf-8", errors="replace"))
            if event is not None:
                events.append(event)
        self._pending = data[start:]
        return events

    def _line(self, line: str) -> SSEEvent | None:
        if line == "":  # blank line dispatches the event
            if not self._data:
                self._event = ""
                return None
            event = SSEEvent(self._event or "message", "\n".join(self._data), self._id)
            self._event, self._data = "", []
            return event
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id":
            self._id = value
        return None
