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


@app.command()
def log(limit: int = typer.Option(20, "--limit", "-n", help="Max runs to show.")) -> None:
    """List recorded runs, newest first."""
    for run in load_services().index.list_runs(limit):
        parents = ",".join(p[:SHORT_ID] for p in run.parents) or "-"
        meta = " ".join(f"{k}={v}" for k, v in sorted(run.metadata.items()))
        line = f"{run.id[:SHORT_ID]}  steps={run.step_count}  parents={parents}  {meta}"
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
    typer.echo("")
    for step_id in run.steps:
        step = services.repo.get_step(step_id)
        status = "error" if step.is_error else "ok"
        typer.echo(
            f"  [{step.index}] {step.kind} {step.tool_name or '-'} {status} {step.duration_ms}ms"
        )
