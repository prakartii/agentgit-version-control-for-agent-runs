"""``agentgit record``: run the recording proxy.

stdio mode wraps a server command::

    agentgit record --session demo -- npx some-mcp-server --flag

HTTP mode listens locally and forwards to a running server::

    agentgit record --upstream http://localhost:9000/mcp --port 8765

In stdio mode our stdout carries only protocol bytes, so every message meant
for a person goes to stderr.
"""

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import create_engine

from agentgit_cli.context import DEFAULT_DB
from agentgit_core import __version__
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore
from agentgit_index.service import IndexService
from agentgit_proxy.http_proxy import create_app, serve
from agentgit_proxy.redaction import Redactor
from agentgit_proxy.sink import RecordingSession, RunResult
from agentgit_proxy.stdio_proxy import (
    process_stdin_chunks,
    process_stdout_write,
    run_stdio_proxy,
)

SHORT_ID = 12


def _say(message: str) -> None:
    typer.echo(f"agentgit: {message}", err=True)


def _build_session(
    server: str, session_id: str | None, transport: str, redact: bool
) -> tuple[RecordingSession, Repository, IndexService]:
    home = Path(os.environ.get("AGENTGIT_HOME", ".agentgit"))
    repo = Repository(FilesystemStore(home / "objects"))
    # Creating the engine does not connect. If Postgres is down, recording still
    # works and the run is simply left unindexed.
    engine = create_engine(
        os.environ.get("DATABASE_URL", DEFAULT_DB), connect_args={"connect_timeout": 3}
    )
    index = IndexService(engine, repo)
    metadata = {"server": server, "transport": transport, "proxy_version": __version__}
    if session_id:
        metadata["session_id"] = session_id
    # With redaction off, an empty rule set makes the redactor a no-op.
    redactor = Redactor() if redact else Redactor(rules=[], sensitive_keys=[])
    return RecordingSession(repo, redactor=redactor, index=index, metadata=metadata), repo, index


def _report(result: RunResult, repo: Repository, index: IndexService, ref: str | None) -> None:
    if result.run_id is None:
        _say("ERROR: the run could not be stored; nothing was recorded")
        return
    run = repo.get_run(result.run_id)
    _say(f"recorded run {result.run_id[:SHORT_ID]} ({len(run.steps)} steps, {run.status})")
    if result.dropped_steps:
        _say(f"WARNING: {result.dropped_steps} exchange(s) could not be stored")
    if run.redacted:
        _say("note: some recorded content was redacted")
    if not result.indexed:
        _say("WARNING: the run is stored but not indexed (is Postgres running?)")
    elif ref:
        try:
            index.set_ref(ref, result.run_id)
            _say(f"ref '{ref}' -> {result.run_id[:SHORT_ID]}")
        except Exception as exc:
            _say(f"WARNING: could not set ref '{ref}': {exc}")


def record(
    command: Annotated[
        list[str] | None,
        typer.Argument(help="Server command to wrap (stdio mode). Put it after `--`."),
    ] = None,
    upstream: Annotated[
        str | None, typer.Option(help="URL of a running Streamable HTTP server (HTTP mode).")
    ] = None,
    host: Annotated[str, typer.Option(help="HTTP mode: address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP mode: port to listen on.")] = 8765,
    server_name: Annotated[
        str | None, typer.Option("--server-name", help="Label stored on the run.")
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session",
            envvar="AGENTGIT_SESSION",
            help="Session id shared by the proxies of one agent session.",
        ),
    ] = None,
    ref: Annotated[str | None, typer.Option(help="Point this ref at the recorded run.")] = None,
    redact: Annotated[bool, typer.Option(help="Redact secrets in the recording.")] = True,
) -> None:
    """Record an MCP session between an agent and a server."""
    if bool(command) == bool(upstream):
        _say("give either a server command after `--` (stdio) or --upstream URL (HTTP)")
        raise typer.Exit(2)

    if command:
        label = server_name or Path(command[0]).stem
        session, repo, index = _build_session(label, session_id, "stdio", redact)
        outcome = asyncio.run(
            run_stdio_proxy(command, process_stdin_chunks(), process_stdout_write, session)
        )
        _report(outcome.run, repo, index, ref)
        raise typer.Exit(outcome.exit_code)

    assert upstream is not None
    label = server_name or upstream
    session, repo, index = _build_session(label, session_id, "http", redact)
    app = create_app(upstream, session)
    _say(f"listening on http://{host}:{port}/mcp -> {upstream} (Ctrl+C to stop and save the run)")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(app, host, port))
    result = app.state.proxy.result
    if result is None:
        _say("ERROR: the proxy stopped before the run could be saved")
        raise typer.Exit(1)
    _report(result, repo, index, ref)


def register(app: typer.Typer) -> None:
    app.command("record")(record)
