from agentgit_core.objects import Run, Step, StepOutcome, StepTiming
from agentgit_core.repo import Repository
from agentgit_core.stats import compare, run_stats
from agentgit_core.store import MemoryStore


def make_run(repo: Repository, calls: list[tuple[str, StepOutcome, int]]) -> str:
    """calls: (tool, outcome, duration_ms)."""
    steps, timings = [], []
    for i, (tool, outcome, ms) in enumerate(calls):
        steps.append(
            repo.put_step(
                Step(
                    method="tools/call",
                    tool_name=tool,
                    outcome=outcome,
                    arguments=repo.put_blob(f'{{"i": {i}}}'.encode()),
                )
            )
        )
        timings.append(StepTiming(started_at_ms=i, duration_ms=ms))
    return repo.put_run(Run(steps=tuple(steps), timings=tuple(timings)))


def test_stats_group_calls_and_time_by_tool() -> None:
    repo = Repository(MemoryStore())
    run_id = make_run(repo, [("read", "ok", 10), ("read", "ok", 30), ("write", "tool_error", 5)])
    stats = run_stats(repo, run_id)
    assert (stats.steps, stats.total_ms, stats.errors) == (3, 45, 1)
    read = stats.by_tool["read"]
    assert (read.calls, read.total_ms, read.max_ms, read.mean_ms) == (2, 40, 30, 20.0)
    assert stats.by_tool["write"].errors == 1


def test_a_run_without_timings_still_counts_steps() -> None:
    repo = Repository(MemoryStore())
    step = repo.put_step(Step(method="tools/call", tool_name="t"))
    stats = run_stats(repo, repo.put_run(Run(steps=(step,))))
    assert (stats.steps, stats.timed_steps, stats.total_ms) == (1, 0, 0)


def test_compare_is_b_minus_a_including_tools_in_one_run_only() -> None:
    repo = Repository(MemoryStore())
    a = run_stats(repo, make_run(repo, [("read", "ok", 10)]))
    b = run_stats(repo, make_run(repo, [("read", "ok", 25), ("write", "ok", 7)]))
    delta = compare(a, b)
    assert (delta.steps, delta.total_ms) == (1, 22)
    assert delta.by_tool_ms == {"read": 15, "write": 7}
    assert delta.by_tool_calls == {"read": 0, "write": 1}
