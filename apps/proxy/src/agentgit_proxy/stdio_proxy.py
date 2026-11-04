"""Transparent stdio proxy.

The agent launches *this* process instead of the MCP server. We launch the real
server as a child and pump bytes both ways. Raw chunks are forwarded as they
arrive, with no parsing in the way; a copy is fed to the correlator afterwards.
Nothing the tap does can alter or delay the forwarded stream, and tap errors
are swallowed.

Shutdown follows the spec: when the agent closes our stdin we close the
server's stdin, wait for it to exit, then terminate it if it does not.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from agentgit_core.objects import RunStatus
from agentgit_proxy.correlator import Correlator, Direction
from agentgit_proxy.framing import DEFAULT_MAX_LINE_BYTES, LineSplitter
from agentgit_proxy.sink import RecordingSession, RunResult

log = logging.getLogger("agentgit.proxy")

CHUNK_SIZE = 64 * 1024
SPAWN_FAILED_EXIT_CODE = 127


@dataclass(frozen=True)
class StdioProxyResult:
    exit_code: int
    run: RunResult


class _Tap:
    """Feeds a copy of the traffic to the correlator and sink. Never raises."""

    def __init__(self, correlator: Correlator, session: RecordingSession, max_line: int) -> None:
        self.correlator = correlator
        self.session = session
        self.splitters: dict[Direction, LineSplitter] = {
            "client": LineSplitter(max_line),
            "server": LineSplitter(max_line),
        }

    def feed(self, direction: Direction, chunk: bytes) -> None:
        try:
            for line in self.splitters[direction].feed(chunk):
                self._observe(direction, line)
        except Exception:
            log.exception("tap failed; forwarding is unaffected")

    def flush(self) -> None:
        """Observe a trailing message that had no newline."""
        for direction, splitter in self.splitters.items():
            try:
                tail = splitter.finish()
                if tail:
                    self._observe(direction, tail)
            except Exception:
                log.exception("tap flush failed")

    def _observe(self, direction: Direction, line: bytes) -> None:
        for exchange in self.correlator.observe(direction, line):
            self.session.submit(exchange)

    @property
    def oversized(self) -> int:
        return sum(s.oversized_lines for s in self.splitters.values())


async def run_stdio_proxy(
    command: Sequence[str],
    client_chunks: AsyncIterator[bytes],
    client_write: Callable[[bytes], Awaitable[None]],
    session: RecordingSession,
    *,
    grace_seconds: float = 5.0,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> StdioProxyResult:
    """Run ``command`` as the upstream server and proxy ``client_*`` to it."""
    correlator = Correlator()
    tap = _Tap(correlator, session, max_line_bytes)
    await session.start()

    try:
        proc = await asyncio.create_subprocess_exec(
            shutil.which(command[0]) or command[0],
            *command[1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # the server's stderr goes straight to ours, untouched
            env=None if env is None else dict(env),
            cwd=cwd,
        )
    except OSError as exc:
        print(f"agentgit: cannot start {command[0]!r}: {exc}", file=sys.stderr)
        run = await session.close("aborted", correlator.stats, {"spawn_error": str(exc)})
        return StdioProxyResult(SPAWN_FAILED_EXIT_CODE, run)

    assert proc.stdin is not None and proc.stdout is not None
    terminated_by_proxy = False
    interrupted = False

    async def client_to_server() -> None:
        stdin = proc.stdin
        assert stdin is not None
        try:
            async for chunk in client_chunks:
                try:
                    stdin.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the server is gone
                # Tap synchronously right after handing the bytes to the pipe, with no
                # await in between, so a response can never be seen before its request.
                tap.feed("client", chunk)
                try:
                    await stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    return
        finally:
            with contextlib.suppress(Exception):
                stdin.close()  # EOF on the server's stdin is the shutdown signal

    async def server_to_client() -> None:
        stdout = proc.stdout
        assert stdout is not None
        while chunk := await stdout.read(CHUNK_SIZE):
            try:
                await client_write(chunk)
            finally:
                tap.feed("server", chunk)

    c2s = asyncio.create_task(client_to_server(), name="client-to-server")
    s2c = asyncio.create_task(server_to_client(), name="server-to-client")
    server_ended_first = False

    try:
        done, _ = await asyncio.wait({c2s, s2c}, return_when=asyncio.FIRST_COMPLETED)
        if s2c in done and c2s not in done:
            server_ended_first = True
            c2s.cancel()
        terminated_by_proxy = await _wait_for_exit(proc, grace_seconds)
        # Let the server's last output reach the client.
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(s2c, grace_seconds)
    except asyncio.CancelledError:
        interrupted = True
        raise
    except Exception:
        interrupted = True
        log.exception("proxy loop failed")
    finally:
        for task in (c2s, s2c):
            if not task.done():
                task.cancel()
        if proc.returncode is None:
            terminated_by_proxy = await _terminate(proc, grace_seconds) or terminated_by_proxy
        tap.flush()
        unanswered = correlator.finish()  # requests still waiting for a response
        for exchange in unanswered:
            session.submit(exchange)
        exit_code = proc.returncode if proc.returncode is not None else 1
        status = _status(
            interrupted, bool(unanswered), server_ended_first, exit_code, terminated_by_proxy
        )
        extra = {
            "transport": "stdio",
            "exit_code": str(exit_code),
            "terminated_by_proxy": str(terminated_by_proxy).lower(),
        }
        if tap.oversized:
            extra["oversized_messages"] = str(tap.oversized)
        run = await asyncio.shield(session.close(status, correlator.stats, extra))

    return StdioProxyResult(exit_code, run)


def _status(
    interrupted: bool,
    has_unanswered: bool,
    server_ended_first: bool,
    exit_code: int,
    terminated_by_proxy: bool,
) -> RunStatus:
    """A run is complete only if the session ended in an orderly way."""
    if interrupted or has_unanswered:
        return "aborted"
    if server_ended_first and exit_code != 0 and not terminated_by_proxy:
        return "aborted"
    return "complete"


async def _wait_for_exit(proc: asyncio.subprocess.Process, grace: float) -> bool:
    """Wait for the server to exit on its own; terminate it if it will not.

    Returns True if we had to terminate it.
    """
    try:
        await asyncio.wait_for(proc.wait(), grace)
        return False
    except TimeoutError:
        return await _terminate(proc, grace)


async def _terminate(proc: asyncio.subprocess.Process, grace: float) -> bool:
    if proc.returncode is not None:
        return False
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
    return True


# ---- adapters for the real process stdin/stdout ------------------------------


async def process_stdin_chunks() -> AsyncIterator[bytes]:
    """Yield chunks from this process's stdin.

    Uses a reader thread because asyncio cannot reliably read stdin pipes on
    Windows. The thread is a daemon, so a blocked read never delays exit.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def reader() -> None:
        try:
            while chunk := os.read(sys.stdin.fileno(), CHUNK_SIZE):
                loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except OSError:
            pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, daemon=True, name="stdin-reader").start()
    while (chunk := await queue.get()) is not None:
        yield chunk


async def process_stdout_write(data: bytes) -> None:
    """Write to this process's stdout, unbuffered, off the event loop."""

    def write() -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(sys.stdout.fileno(), view) :]

    await asyncio.to_thread(write)
