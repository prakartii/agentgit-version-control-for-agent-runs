import typer

from agentgit_cli.context import load_services
from agentgit_core import __version__
from agentgit_core.store import ObjectNotFoundError

app = typer.Typer(help="Version control for AI agent runs.", no_args_is_help=True)

SHORT_ID = 12


@app.callback()
def main() -> None:
    """Root group. Without a callback Typer collapses a single command into the root."""


@app.command()
def version() -> None:
    """Print the AgentGit version."""
    typer.echo(__version__)


def _completeness_flags(status: str, redacted: bool, dropped: int) -> str:
    """Make incomplete or altered recordings visible instead of implied."""
    flags = []
    if status != "complete":
        flags.append(f"[{status.upper()}]")
    if dropped:
        flags.append(f"[{dropped} DROPPED]")
    if redacted:
        flags.append("[REDACTED]")
    return " ".join(flags)


@app.command()
def log(limit: int = typer.Option(20, "--limit", "-n", help="Max runs to show.")) -> None:
    """List recorded runs, newest first."""
    for run in load_services().index.list_runs(limit):
        parents = ",".join(p[:SHORT_ID] for p in run.parents) or "-"
        meta = " ".join(f"{k}={v}" for k, v in sorted(run.metadata.items()))
        flags = _completeness_flags(run.status, run.redacted, run.dropped_steps)
        line = f"{run.id[:SHORT_ID]}  steps={run.step_count}  parents={parents}  {meta} {flags}"
        typer.echo(line.rstrip())


@app.command()
def show(target: str = typer.Argument(..., help="Run id, unique id prefix, or ref name.")) -> None:
    """Show one run and its steps."""
    services = load_services()
    try:
        run_id = services.index.resolve(target)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    if run_id is None:
        typer.echo(f"error: no run matches '{target}'", err=True)
        raise typer.Exit(1)
    try:
        run = services.repo.get_run(run_id)
    except ObjectNotFoundError:
        typer.echo(f"error: run {run_id} is indexed but missing from the object store", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"run {run_id}")
    if run.parents:
        typer.echo(f"forked from {', '.join(run.parents)} at step {run.fork_step}")
    for key, value in sorted(run.metadata.items()):
        typer.echo(f"{key}: {value}")
    typer.echo(f"status: {run.status}")
    if run.dropped_steps:
        typer.echo(f"warning: {run.dropped_steps} exchange(s) were not recorded (storage errors)")
    if run.redacted:
        typer.echo("note: some recorded content was redacted")
    typer.echo("")
    for position, step_id in enumerate(run.steps):
        step = services.repo.get_step(step_id)
        timing = run.timings[position] if run.timings else None
        took = f"{timing.duration_ms}ms" if timing else "-"
        typer.echo(
            f"  [{position}] {step.method} {step.tool_name or '-'} "
            f"{step.outcome} {took}"
        )
