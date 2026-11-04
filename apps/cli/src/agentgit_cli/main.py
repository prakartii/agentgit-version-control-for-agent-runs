import typer

from agentgit_core import __version__

app = typer.Typer(help="Version control for AI agent runs.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Root group. Without a callback Typer collapses a single command into the root."""


@app.command()
def version() -> None:
    """Print the AgentGit version."""
    typer.echo(__version__)
