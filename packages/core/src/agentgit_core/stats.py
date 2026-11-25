"""Latency and outcome statistics for a run, and the difference between two runs.

Only what a recording contains can be measured. Steps carry wall-clock timings
(``Run.timings``), so latency is available per tool. Token counts and cost are
*not* stored in runs (model calls never pass through the MCP proxy); the agent
CLI reports those separately.

Timings of forked runs only cover the steps that were recorded live. Steps shared
with the source keep the source's timings.
"""

from dataclasses import dataclass, field

from agentgit_core.repo import Repository


@dataclass
class ToolStats:
    calls: int = 0
    errors: int = 0  # outcome other than "ok"
    total_ms: int = 0
    max_ms: int = 0

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


@dataclass
class RunStats:
    run_id: str
    steps: int
    timed_steps: int  # steps that have a timing; fewer than steps for old or partial runs
    total_ms: int  # sum of step durations (not wall-clock span; steps may overlap)
    errors: int
    by_tool: dict[str, ToolStats] = field(default_factory=dict)


def run_stats(repo: Repository, run_id: str) -> RunStats:
    run = repo.get_run(run_id)
    stats = RunStats(run_id, len(run.steps), len(run.timings), 0, 0)
    for position, step_id in enumerate(run.steps):
        step = repo.get_step(step_id)
        failed = step.outcome != "ok"
        stats.errors += failed
        # Non-tool requests (initialize, tools/list) are grouped under their method.
        tool = stats.by_tool.setdefault(step.tool_name or step.method, ToolStats())
        tool.calls += 1
        tool.errors += failed
        if position < len(run.timings):
            ms = run.timings[position].duration_ms
            stats.total_ms += ms
            tool.total_ms += ms
            tool.max_ms = max(tool.max_ms, ms)
    return stats


@dataclass(frozen=True)
class StatsDelta:
    """B minus A. Tools present in only one run have the other side at zero."""

    steps: int
    total_ms: int
    errors: int
    by_tool_ms: dict[str, int]
    by_tool_calls: dict[str, int]


def compare(a: RunStats, b: RunStats) -> StatsDelta:
    tools = sorted(set(a.by_tool) | set(b.by_tool))
    zero = ToolStats()
    return StatsDelta(
        steps=b.steps - a.steps,
        total_ms=b.total_ms - a.total_ms,
        errors=b.errors - a.errors,
        by_tool_ms={
            t: b.by_tool.get(t, zero).total_ms - a.by_tool.get(t, zero).total_ms for t in tools
        },
        by_tool_calls={
            t: b.by_tool.get(t, zero).calls - a.by_tool.get(t, zero).calls for t in tools
        },
    )
