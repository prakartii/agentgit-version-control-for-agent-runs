"""``agentgit fork``, ``diff`` and ``checkout``.

These work on the object store directly, so they do not need Postgres. Run
ids and unique prefixes resolve from the store; a fork is additionally added
to the index when it is reachable, and is still usable by id if it is not.
"""

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import create_engine

from agentgit_cli.context import DEFAULT_DB
from agentgit_cli.replay import resolve_run_id
from agentgit_core.diff import Change, RunDiff, diff_runs
from agentgit_core.fork import ForkError, fork_run, parse_json_value
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore, ObjectNotFoundError
from agentgit_index.service import IndexService

SHORT_ID = 12
NOT_FOUND_EXIT_CODE = 2
MAX_VALUE_CHARS = 100


def _say(message: str) -> None:
    typer.echo(f"agentgit: {message}", err=True)


def _repo() -> tuple[Path, Repository]:
    home = Path(os.environ.get("AGENTGIT_HOME", ".agentgit"))
    return home, Repository(FilesystemStore(home / "objects"))


def _resolve(home: Path, repo: Repository, target: str) -> str:
    run_id = resolve_run_id(home, target, repo)
    if run_id is None:
        _say(f"no run matches '{target}'")
        raise typer.Exit(NOT_FOUND_EXIT_CODE)
    return run_id


def _index_best_effort(repo: Repository, run_id: str, ref: str | None) -> None:
    """Add the new run to the index if Postgres is up; never fail the command."""
    try:
        engine = create_engine(
            os.environ.get("DATABASE_URL", DEFAULT_DB), connect_args={"connect_timeout": 3}
        )
        index = IndexService(engine, repo)
        index.index_run(run_id)
        if ref:
            index.set_ref(ref, run_id)
    except Exception as e:
        _say(f"not added to the index ({type(e).__name__}); the run is in the object store")


def fork_command(
    target: Annotated[str, typer.Argument(help="Run id, unique id prefix, or ref name.")],
    at: Annotated[int, typer.Option("--at", help="Step position where the fork diverges.")],
    result: Annotated[str | None, typer.Option(help="New result for that step, as JSON.")] = None,
    result_file: Annotated[
        Path | None, typer.Option(help="Read the new result (JSON) from this file.")
    ] = None,
    ref: Annotated[str | None, typer.Option(help="Also point this ref name at the fork.")] = None,
) -> None:
    """Fork a run at a step, optionally replacing that step's tool result.

    Without --result/--result-file the fork is the history before step AT. The
    steps after the fork point are dropped: they were produced in a world where
    the original result was true.
    """
    if result is not None and result_file is not None:
        _say("give --result or --result-file, not both")
        raise typer.Exit(2)
    home, repo = _repo()
    source = _resolve(home, repo, target)
    try:
        text = result_file.read_text(encoding="utf-8") if result_file else result
        edit = text is not None
        fork_id = fork_run(
            repo,
            source,
            at,
            new_result=parse_json_value(text) if text is not None else None,
            edit=edit,
        )
    except (ForkError, OSError, ObjectNotFoundError) as e:
        _say(str(e))
        raise typer.Exit(2) from None
    _index_best_effort(repo, fork_id, ref)
    typer.echo(fork_id)
    _say(f"forked {source[:SHORT_ID]} at step {at} -> {fork_id[:SHORT_ID]}")


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 1] + "…"


def _label(step_tool: str | None, method: str) -> str:
    return f"{method} {step_tool}" if step_tool else method


def _render_change(change: Change) -> list[str]:
    return [
        f"      {change.path}",
        f"        - {_short(change.before)}",
        f"        + {_short(change.after)}",
    ]


def render_diff(diff: RunDiff) -> str:
    lines = [f"--- {diff.a_id[:SHORT_ID]}", f"+++ {diff.b_id[:SHORT_ID]}"]
    if diff.identical:
        lines.append(
            f"identical ({diff.common_prefix} shared steps)" if diff.common_prefix else "identical"
        )
        return "\n".join(lines)
    lines.append(
        f"{diff.common_prefix} leading step(s) are the same objects; "
        f"first difference at step {diff.first_divergence}"
    )
    for s in diff.steps[diff.common_prefix :]:
        if s.op == "equal":
            lines.append(f"  = [{s.a_index}/{s.b_index}] same step")
        elif s.op == "delete" and s.a:
            lines.append(f"  - [{s.a_index}] {_label(s.a.tool_name, s.a.method)} (only in A)")
        elif s.op == "insert" and s.b:
            lines.append(f"  + [{s.b_index}] {_label(s.b.tool_name, s.b.method)} (only in B)")
        elif s.op == "replace" and s.b:
            lines.append(f"  ~ [{s.a_index}/{s.b_index}] {_label(s.b.tool_name, s.b.method)}")
            for change in s.changes:
                lines.extend(_render_change(change))
    return "\n".join(lines)


def diff_command(
    a: Annotated[str, typer.Argument(help="First run (id, prefix or ref).")],
    b: Annotated[str, typer.Argument(help="Second run (id, prefix or ref).")],
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Compare two runs. Exit code 1 if they differ, 0 if identical."""
    home, repo = _repo()
    try:
        diff = diff_runs(repo, _resolve(home, repo, a), _resolve(home, repo, b))
    except ObjectNotFoundError as e:
        _say(f"object missing from the store: {e}")
        raise typer.Exit(2) from None
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "a": diff.a_id,
                    "b": diff.b_id,
                    "identical": diff.identical,
                    "common_prefix": diff.common_prefix,
                    "first_divergence": diff.first_divergence,
                    "steps": [
                        {
                            "op": s.op,
                            "a_index": s.a_index,
                            "b_index": s.b_index,
                            "changes": [
                                {"path": c.path, "before": c.before, "after": c.after}
                                for c in s.changes
                            ],
                        }
                        for s in diff.steps
                        if s.op != "equal"
                    ],
                },
                indent=2,
            )
        )
    else:
        typer.echo(render_diff(diff))
    raise typer.Exit(0 if diff.identical else 1)


def _safe(name: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "step")[:40]


def checkout_command(
    target: Annotated[str, typer.Argument(help="Run id, unique id prefix, or ref name.")],
    to: Annotated[Path, typer.Option("--to", help="Directory to write the run into.")],
) -> None:
    """Write a run out as readable JSON files (run.json and steps/NNN-*.json).

    This is a read-only export for inspection and for preparing a fork edit
    (copy a step's result into a file and pass it to ``fork --result-file``).
    """
    home, repo = _repo()
    run_id = _resolve(home, repo, target)
    run = repo.get_run(run_id)
    (to / "steps").mkdir(parents=True, exist_ok=True)
    for position, step_id in enumerate(run.steps):
        step = repo.get_step(step_id)
        body: dict[str, Any] = {"step_id": step_id, **step.model_dump(mode="json")}
        for name in ("arguments", "result", "events"):
            blob = getattr(step, name)
            body[name] = json.loads(repo.get_blob(blob)) if blob else None
        path = to / "steps" / f"{position:03d}-{_safe(step.tool_name or step.method)}.json"
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    header = {
        "run_id": run_id,
        "parents": list(run.parents),
        "fork_step": run.fork_step,
        "status": run.status,
        "redacted": run.redacted,
        "dropped_steps": run.dropped_steps,
        "metadata": run.metadata,
        "steps": len(run.steps),
    }
    (to / "run.json").write_text(json.dumps(header, indent=2), encoding="utf-8")
    _say(f"wrote {len(run.steps)} step(s) to {to}")


def register(app: typer.Typer) -> None:
    app.command("fork")(fork_command)
    app.command("diff")(diff_command)
    app.command("checkout")(checkout_command)
