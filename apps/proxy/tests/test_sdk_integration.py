"""Integration: the official SDK client, a real demo server, the real proxy process.

These tests pin down the behaviour the unit tests only approximate: an
unmodified client talking through `agentgit record` sees the same results as
talking to the server directly, and the recording matches what happened.
"""

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import Client, StdioServerParameters

from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore
from agentgit_proxy.http_proxy import create_app
from agentgit_proxy.sink import RecordingSession

FILES_MODULE = "agentgit_demo_servers.files"
PY = sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def stored_run(home: Path) -> tuple[Repository, Any]:
    root = home / "objects" / "run"
    ids = sorted(p.parent.name + p.name for p in root.rglob("*") if p.is_file())
    assert len(ids) == 1, f"expected exactly one run, found {len(ids)}"
    repo = Repository(FilesystemStore(home / "objects"))
    return repo, repo.get_run(ids[0])


def env_for(home: Path, files_root: Path) -> dict[str, str]:
    return {**os.environ, "AGENTGIT_HOME": str(home), "DEMO_FILES_ROOT": str(files_root)}


async def exercise(client: Client) -> dict[str, Any]:
    """The same calls for every transport; returns what the agent saw."""
    tools = await client.list_tools()
    wrote = await client.call_tool("write_file", {"path": "a.txt", "content": "hello"})
    read = await client.call_tool("read_file", {"path": "a.txt"})
    listing = await client.call_tool("list_files", {})
    failed = await client.call_tool("read_file", {"path": "../escape.txt"})
    return {
        "tools": sorted(t.name for t in tools.tools),
        "wrote": wrote.structured_content,
        "read": read.structured_content,
        "listing": listing.structured_content,
        "failed": (failed.is_error, failed.content[0].text),  # type: ignore[union-attr]
    }


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    home, files_root = tmp_path / "home", tmp_path / "files"
    files_root.mkdir()
    return home, files_root


def test_stdio_through_proxy_matches_direct_and_is_recorded(dirs: tuple[Path, Path]) -> None:
    home, files_root = dirs
    env = env_for(home, files_root)

    async def direct() -> dict[str, Any]:
        params = StdioServerParameters(command=PY, args=["-m", FILES_MODULE], env=env)
        async with Client(params) as client:
            return await exercise(client)

    async def proxied() -> dict[str, Any]:
        args = ["-m", "agentgit_cli", "record", "--no-index", "--session", "it", "--"]
        params = StdioServerParameters(command=PY, args=[*args, PY, "-m", FILES_MODULE], env=env)
        async with Client(params) as client:
            return await exercise(client)

    baseline = asyncio.run(direct())
    (files_root / "a.txt").unlink()  # reset server state between the two runs
    through_proxy = asyncio.run(proxied())

    assert through_proxy == baseline  # an unmodified client sees identical results
    assert baseline["failed"][0] is True

    repo, run = stored_run(home)
    steps = [repo.get_step(s) for s in run.steps]
    assert [s.method for s in steps][-4:] == ["tools/call"] * 4
    assert [s.tool_name for s in steps][-4:] == [
        "write_file",
        "read_file",
        "list_files",
        "read_file",
    ]
    assert [s.outcome for s in steps][-4:] == ["ok", "ok", "ok", "tool_error"]
    assert "tools/list" in [s.method for s in steps]
    assert run.status == "complete" and run.dropped_steps == 0
    assert run.metadata["protocol_version"] == "2026-07-28"
    assert run.metadata["session_id"] == "it" and run.metadata["transport"] == "stdio"
    assert all(t.duration_ms >= 0 for t in run.timings) and len(run.timings) == len(steps)


class HttpStack:
    """A demo server (subprocess, HTTP) with the recording proxy in front of it.

    The proxy runs in-process under real uvicorn in a thread: real sockets and a
    real graceful shutdown, without depending on OS signal delivery to a child
    process (unreliable through the Windows venv launcher).
    """

    def __init__(self, home: Path, files_root: Path) -> None:
        self.home = home
        server_port, proxy_port = free_port(), free_port()
        self.url = f"http://127.0.0.1:{proxy_port}/mcp"
        self._server = subprocess.Popen(
            [PY, "-m", FILES_MODULE, "--transport", "http", "--port", str(server_port)],
            env=env_for(home, files_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        repo = Repository(FilesystemStore(home / "objects"))
        session = RecordingSession(repo, metadata={"transport": "http"})
        self.app = create_app(f"http://127.0.0.1:{server_port}/mcp", session)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=proxy_port, log_level="warning")
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn.run, daemon=True)
        self._thread.start()
        self._wait_until_ready(server_port)

    def _wait_until_ready(self, server_port: int) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{server_port}/mcp", timeout=1)
                if self._uvicorn.started:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError("servers did not start")

    def stop(self) -> None:
        """Graceful shutdown: this is what makes the proxy save the run."""
        if self._thread.is_alive():
            self._uvicorn.should_exit = True
            self._thread.join(timeout=30)
        if self._server.poll() is None:
            self._server.kill()
            self._server.wait()


@pytest.fixture
def http_stack(dirs: tuple[Path, Path]) -> Iterator[HttpStack]:
    stack = HttpStack(*dirs)
    try:
        yield stack
    finally:
        stack.stop()


def test_http_through_proxy_is_recorded(http_stack: HttpStack) -> None:
    async def go() -> dict[str, Any]:
        async with Client(http_stack.url) as client:
            return await exercise(client)

    seen = asyncio.run(go())
    assert seen["tools"] == ["list_files", "read_file", "write_file"]
    assert seen["read"] == {"result": "hello"}
    assert seen["failed"][0] is True

    http_stack.stop()  # the run is saved on graceful shutdown
    assert http_stack.app.state.proxy.result.run_id is not None
    repo, run = stored_run(http_stack.home)
    steps = [repo.get_step(s) for s in run.steps]
    assert [s.tool_name for s in steps if s.method == "tools/call"] == [
        "write_file", "read_file", "list_files", "read_file",
    ]  # fmt: skip
    assert [s.outcome for s in steps if s.method == "tools/call"][-1] == "tool_error"
    assert run.status == "complete" and run.dropped_steps == 0
    assert run.metadata["transport"] == "http"
