import typer

from agentgit_core import __version__

app = typer.Typer(help="Version control for AI agent runs.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the AgentGit version."""
    typer.echo(__version__)
