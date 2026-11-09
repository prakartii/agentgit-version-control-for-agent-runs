"""``agentgit replay``: act as an MCP server that answers from a recorded run.

Use it where the real server command would go::

    agentgit replay <run id> --report replay.json

Nothing is executed against the real world: results come from the recording.
With ``--live`` and a server command after ``--``, requests the recording
cannot answer (what a forked run does next) go to that real server instead of
being reported as divergence. That part is live re-execution, not replay::

    agentgit replay <fork id> --live -- python -m my_server

When the agent hangs up, a summary goes to stderr (our stdout carries only
protocol messages) and the exit code says whether replay was faithful.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import create_engine

from agentgit_cli.context import DEFAULT_DB
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore
from agentgit_index.service import IndexService
from agentgit_proxy.stdio_proxy import process_stdin_chunks, process_stdout_write
from agentgit_replay.chaos import ChaosPlan, load_plan
from agentgit_replay.engine import ReplayEngine
from agentgit_replay.live import LiveUpstream
from agentgit_replay.server import serve_stdio

NOT_FOUND_EXIT_CODE = 2


def _say(message: str) -> None:
    typer.echo(f"agentgit: {message}", err=True)


def resolve_run_id(home: Path, target: str, repo: Repository) -> str | None:
    """Find a run by full id, unique id prefix, or ref name.

    Ids and prefixes are looked up directly in the object store, so replay works
    without Postgres. Ref names need the index and are tried last, best effort.
    """
    run_dir = home / "objects" / "run"
    if len(target) >= 2 and run_dir.is_dir():
        shard = run_dir / target[:2]
        matches = (
            [
                target[:2] + p.name
                for p in shard.iterdir()
                if (target[:2] + p.name).startswith(target)
            ]
            if shard.is_dir()
            else []
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            _say(f"'{target}' matches {len(matches)} runs; use a longer prefix")
            return None
    try:
        engine = create_engine(
            os.environ.get("DATABASE_URL", DEFAULT_DB), connect_args={"connect_timeout": 3}
        )
        return IndexService(engine, repo).resolve_ref(target)
    except Exception:
        return None


def replay_command(
    target: Annotated[str, typer.Argument(help="Run id, unique id prefix, or ref name.")],
    report: Annotated[Path | None, typer.Option(help="Write the JSON report here.")] = None,
    allow_reorder: Annotated[
        bool, typer.Option(help="Do not treat a different request order as divergence.")
    ] = False,
    live: Annotated[
        bool, typer.Option("--live", help="Send unrecorded requests to the server command.")
    ] = False,
    chaos: Annotated[
        Path | None, typer.Option(help="Damage served results per this chaos plan (TOML).")
    ] = None,
    command: Annotated[
        list[str] | None, typer.Argument(help="With --live: the real server command, after `--`.")
    ] = None,
) -> None:
    """Serve a recorded run as an MCP server (stdio) and report divergence."""
    if live != bool(command):
        _say("--live needs a server command after `--`, and a command needs --live")
        raise typer.Exit(NOT_FOUND_EXIT_CODE)
    home = Path(os.environ.get("AGENTGIT_HOME", ".agentgit"))
    repo = Repository(FilesystemStore(home / "objects"))
    run_id = resolve_run_id(home, target, repo)
    if run_id is None:
        _say(f"no run matches '{target}'")
        raise typer.Exit(NOT_FOUND_EXIT_CODE)

    plan: ChaosPlan | None = None
    if chaos is not None:
        try:
            plan = ChaosPlan(load_plan(chaos))
        except ValueError as e:
            _say(f"bad chaos plan: {e}")
            raise typer.Exit(NOT_FOUND_EXIT_CODE) from None
    engine = ReplayEngine(repo, run_id, allow_reorder=allow_reorder, live=live, chaos=plan)
    upstream = LiveUpstream(command) if live and command else None
    result = asyncio.run(
        serve_stdio(engine, process_stdin_chunks(), process_stdout_write, upstream=upstream)
    )
    _say(result.summary())
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    raise typer.Exit(0 if result.ok else 1)


def payloads_command() -> None:
    """List the built-in prompt-injection payloads (use as payload = "builtin:<name>")."""
    from agentgit_replay.payloads import BUILTIN, CANARY

    typer.echo(f"canary token: {CANARY}")
    for name, text in sorted(BUILTIN.items()):
        typer.echo("")
        typer.echo(f"builtin:{name}")
        typer.echo(f"  {text}")


def register(app: typer.Typer) -> None:
    app.command("replay")(replay_command)
    chaos_app = typer.Typer(help="Chaos and injection testing helpers.", no_args_is_help=True)
    chaos_app.command("payloads")(payloads_command)
    app.add_typer(chaos_app, name="chaos")
