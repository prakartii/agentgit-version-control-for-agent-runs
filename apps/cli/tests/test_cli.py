from typer.testing import CliRunner

from agentgit_cli.main import app


def test_version_command_prints_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.0.0"


def seed_run() -> str:
    """Record one run into the CLI's configured store and index."""
    from agentgit_cli.context import load_services
    from agentgit_core.objects import Run, Step, StepTiming

    services = load_services()
    result = services.repo.put_blob(b"file contents")
    step = services.repo.put_step(
        Step(method="tools/call", tool_name="read_file", result=result)
    )
    run_id = services.repo.put_run(
        Run(
            steps=(step,),
            timings=(StepTiming(started_at_ms=1, duration_ms=7),),
            metadata={"agent": "demo"},
        )
    )
    services.index.set_ref("main", run_id)
    return run_id


def test_log_lists_runs(cli_env: str) -> None:
    run_id = seed_run()
    result = CliRunner().invoke(app, ["log"])
    assert result.exit_code == 0
    assert run_id[:12] in result.stdout
    assert "agent=demo" in result.stdout


def test_show_by_ref_and_prefix(cli_env: str) -> None:
    run_id = seed_run()
    for target in ("main", run_id[:8]):
        result = CliRunner().invoke(app, ["show", target])
        assert result.exit_code == 0
        assert f"run {run_id}" in result.stdout
        assert "tools/call read_file ok 7ms" in result.stdout


def test_show_unknown_target_fails(cli_env: str) -> None:
    result = CliRunner().invoke(app, ["show", "nope"])
    assert result.exit_code == 1


def test_show_and_log_flag_incomplete_runs(cli_env: str) -> None:
    from agentgit_cli.context import load_services
    from agentgit_core.objects import Run, Step

    services = load_services()
    step = services.repo.put_step(Step(method="tools/call", tool_name="t", outcome="no_response"))
    run_id = services.repo.put_run(
        Run(steps=(step,), status="aborted", redacted=True, dropped_steps=2)
    )
    services.index.set_ref("broken", run_id)

    shown = CliRunner().invoke(app, ["show", "broken"]).stdout
    assert "status: aborted" in shown
    assert "2 exchange(s) were not recorded" in shown
    assert "redacted" in shown
    assert "no_response" in shown

    listed = CliRunner().invoke(app, ["log"]).stdout
    assert "[ABORTED]" in listed and "[2 DROPPED]" in listed and "[REDACTED]" in listed
