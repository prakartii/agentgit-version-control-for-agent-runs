"""``agentgit verify`` and ``agentgit gc``: check the object store, clean dead objects.

Both work on the filesystem store directly and need no database.
"""

import os
from pathlib import Path
from typing import Annotated

import typer

from agentgit_core.maintenance import find_unreferenced, verify
from agentgit_core.store import FilesystemStore

DAMAGED_EXIT_CODE = 1
DEFAULT_GRACE_S = 3600


def _say(message: str) -> None:
    typer.echo(message)


def _store() -> FilesystemStore:
    home = Path(os.environ.get("AGENTGIT_HOME", ".agentgit"))
    return FilesystemStore(home / "objects")


def verify_command() -> None:
    """Re-hash every object and check every reference resolves. Exit 1 if damaged."""
    report = verify(_store())
    _say(f"checked {report.checked} objects")
    for object_type, object_id in report.corrupt:
        _say(f"CORRUPT  {object_type} {object_id[:12]}: content does not match its id")
    for line in report.dangling:
        _say(f"MISSING  {line}")
    if report.ok:
        _say("store is intact")
    raise typer.Exit(0 if report.ok else DAMAGED_EXIT_CODE)


def gc_command(
    delete: Annotated[bool, typer.Option(help="Actually delete. Without it, only report.")] = False,
    grace_seconds: Annotated[
        int, typer.Option(help="Keep objects newer than this, so a live recording is safe.")
    ] = DEFAULT_GRACE_S,
) -> None:
    """Remove steps and blobs that no run uses. Runs are never removed."""
    store = _store()
    dead = find_unreferenced(store, grace_seconds)
    stale = store.stale_temp_files()
    for object_type, object_id in dead:
        _say(f"{'delete' if delete else 'would delete'}  {object_type} {object_id[:12]}")
    for path in stale:
        _say(f"{'delete' if delete else 'would delete'}  leftover temp file {path.name}")
    if delete:
        for object_type, object_id in dead:
            store.delete(object_type, object_id)
        for path in stale:
            path.unlink(missing_ok=True)
        _say(f"deleted {len(dead)} object(s) and {len(stale)} temp file(s)")
    else:
        _say(f"{len(dead)} object(s) and {len(stale)} temp file(s) found; use --delete to remove")


def register(app: typer.Typer) -> None:
    app.command("verify")(verify_command)
    app.command("gc")(gc_command)
