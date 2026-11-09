"""``agentgit ci run`` and ``agentgit ci add``; ``agentgit bundle`` and ``unbundle``."""

import json
import os
import re
from pathlib import Path
from typing import Annotated

import typer

from agentgit_cli.ci.report import junit_xml, markdown
from agentgit_cli.ci.runner import ScenarioResult, run_scenario
from agentgit_cli.ci.scenario import SERVER_TOKEN, ScenarioError, find_scenarios, load_scenario
from agentgit_cli.replay import resolve_run_id
from agentgit_core.bundle import BundleError, dumps, export_bundle, import_bundle
from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore, ObjectNotFoundError

USAGE_EXIT = 2
ci_app = typer.Typer(help="Agent CI: replay saved scenarios and fail on behaviour changes.")


def _say(message: str) -> None:
    typer.echo(f"agentgit: {message}", err=True)


def _home() -> Path:
    return Path(os.environ.get("AGENTGIT_HOME", ".agentgit"))


def _resolve(target: str) -> tuple[Repository, str]:
    home = _home()
    repo = Repository(FilesystemStore(home / "objects"))
    run_id = resolve_run_id(home, target, repo)
    if run_id is None:
        _say(f"no run matches '{target}'")
        raise typer.Exit(USAGE_EXIT)
    return repo, run_id


@ci_app.command("run")
def run_command(
    targets: Annotated[list[Path], typer.Argument(help="Scenario files or directories.")],
    report_md: Annotated[Path | None, typer.Option(help="Write a Markdown summary here.")] = None,
    junit: Annotated[Path | None, typer.Option(help="Write JUnit XML here.")] = None,
) -> None:
    """Replay every scenario. Exit 0 if all pass, 1 if any fail, 2 on a bad scenario."""
    try:
        loaded = [(f, *load_scenario(f)) for f in find_scenarios(targets)]
    except ScenarioError as e:
        _say(str(e))
        raise typer.Exit(USAGE_EXIT) from None
    if not loaded:
        _say("no scenarios found (looking for *.scenario.toml)")
        raise typer.Exit(USAGE_EXIT)

    results: list[ScenarioResult] = []
    for file, scenario, workdir in loaded:
        result = run_scenario(scenario, file, workdir)
        _say(f"{'PASS' if result.passed else 'FAIL'}  {scenario.name}")
        for failure in result.failures:
            _say(f"      {failure}")
        results.append(result)

    summary = markdown(results)
    typer.echo(summary)
    if report_md:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(summary, encoding="utf-8")
    if junit:
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text(junit_xml(results), encoding="utf-8")
    raise typer.Exit(0 if all(r.passed for r in results) else 1)


@ci_app.command("add")
def add_command(
    target: Annotated[str, typer.Argument(help="Run id, unique prefix or ref to save.")],
    name: Annotated[str, typer.Option(help="Scenario name, e.g. 'support refund'.")],
    command: Annotated[
        list[str] | None,
        typer.Argument(help=f"Agent command after `--`; use {SERVER_TOKEN} for the MCP server."),
    ] = None,
    directory: Annotated[Path, typer.Option("--dir", help="Where to write the files.")] = Path(
        "scenarios"
    ),
) -> None:
    """Save a recorded run as a scenario (a bundle plus a .scenario.toml)."""
    if not command or SERVER_TOKEN not in command:
        _say(f"give the agent command after `--`, containing {SERVER_TOKEN}")
        raise typer.Exit(USAGE_EXIT)
    repo, run_id = _resolve(target)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "scenario"
    directory.mkdir(parents=True, exist_ok=True)
    bundle_file = directory / f"{slug}.bundle.json"
    bundle_file.write_text(dumps(export_bundle(repo, run_id)), encoding="utf-8")
    toml = (
        f"name = {json.dumps(name)}\n"
        f'bundle = "{slug}.bundle.json"\n'
        f"command = {json.dumps(command)}\n"
    )
    scenario_file = directory / f"{slug}.scenario.toml"
    scenario_file.write_text(toml, encoding="utf-8")
    _say(f"wrote {scenario_file} and {bundle_file.name} (run {run_id[:12]})")


def bundle_command(
    target: Annotated[str, typer.Argument(help="Run id, unique prefix or ref.")],
    output: Annotated[Path, typer.Option("-o", "--output", help="Bundle file to write.")],
) -> None:
    """Export a run (with its ancestors, steps and blobs) as one portable JSON file."""
    repo, run_id = _resolve(target)
    try:
        output.write_text(dumps(export_bundle(repo, run_id)), encoding="utf-8")
    except (ObjectNotFoundError, ValueError) as e:
        _say(f"cannot export: {e}")
        raise typer.Exit(USAGE_EXIT) from None
    _say(f"wrote {output} (run {run_id[:12]})")


def unbundle_command(
    bundle: Annotated[Path, typer.Argument(help="Bundle file to import.")],
) -> None:
    """Import a bundle into the object store. Prints the run id."""
    store = FilesystemStore(_home() / "objects")
    try:
        run_id = import_bundle(store, json.loads(bundle.read_text(encoding="utf-8")))
    except (OSError, ValueError, BundleError) as e:
        _say(f"cannot import {bundle}: {e}")
        raise typer.Exit(USAGE_EXIT) from None
    typer.echo(run_id)


def register(app: typer.Typer) -> None:
    app.add_typer(ci_app, name="ci")
    app.command("bundle")(bundle_command)
    app.command("unbundle")(unbundle_command)
