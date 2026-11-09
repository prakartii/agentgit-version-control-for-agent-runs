"""Render scenario results as Markdown (PR comments, job summaries) and JUnit XML."""

from xml.etree import ElementTree as ET

from agentgit_cli.ci.runner import ScenarioResult


def _replay_line(r: ScenarioResult) -> str:
    if r.replay is None:
        return "no replay report"
    rep = r.replay
    return (
        f"{rep['served']}/{rep['recorded_steps']} recorded steps served, "
        f"{len(rep['divergences'])} divergence(s)"
    )


def markdown(results: list[ScenarioResult]) -> str:
    failed = [r for r in results if not r.passed]
    head = "all passed" if not failed else f"{len(failed)} failed"
    lines = [
        f"## AgentGit CI: {head} ({len(results)} scenario(s))",
        "",
        "| Scenario | Result | Replay | Time |",
        "|---|---|---|---|",
    ]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"| {r.name} | {mark} | {_replay_line(r)} | {r.duration_s:.1f}s |")
    for r in failed:
        lines += ["", f"### {r.name}", ""]
        lines += [f"- {f}" for f in r.failures]
        for d in (r.replay or {}).get("divergences", [])[:10]:
            tool = f" {d['tool_name']}" if d.get("tool_name") else ""
            lines.append(f"  - `{d['kind']}`: {d['method']}{tool}")
        if r.output_tail.strip():
            lines += [
                "",
                "<details><summary>agent output</summary>",
                "",
                "```",
                r.output_tail.strip(),
                "```",
                "</details>",
            ]
    return "\n".join(lines) + "\n"


def junit_xml(results: list[ScenarioResult]) -> str:
    suite = ET.Element(
        "testsuite",
        name="agentgit-ci",
        tests=str(len(results)),
        failures=str(sum(not r.passed for r in results)),
        time=f"{sum(r.duration_s for r in results):.3f}",
    )
    for r in results:
        case = ET.SubElement(
            suite, "testcase", name=r.name, classname=r.path, time=f"{r.duration_s:.3f}"
        )
        if not r.passed:
            failure = ET.SubElement(case, "failure", message="; ".join(r.failures))
            failure.text = r.output_tail
    return ET.tostring(suite, encoding="unicode")
