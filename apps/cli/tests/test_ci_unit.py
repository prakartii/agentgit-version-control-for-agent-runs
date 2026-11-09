import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agentgit_cli.ci.report import junit_xml, markdown
from agentgit_cli.ci.runner import ScenarioResult, expand_command
from agentgit_cli.ci.scenario import ScenarioError, find_scenarios, load_scenario

GOOD = 'name = "x"\nbundle = "b.json"\ncommand = ["agent", "--mcp", "{server}"]\n'


def test_server_token_expands_to_several_arguments() -> None:
    out = expand_command(["agent", "{server}", "--flag"], ["py", "-m", "replay"])
    assert out == ["agent", "py", "-m", "replay", "--flag"]


def test_load_and_find(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "a.scenario.toml"
    f.write_text(GOOD)
    (tmp_path / "ignored.toml").write_text(GOOD)
    assert find_scenarios([tmp_path]) == [f]
    scenario, workdir = load_scenario(f)
    assert scenario.expect_exit == 0 and not scenario.allow_divergence and workdir == f.parent


@pytest.mark.parametrize(
    "text",
    [
        GOOD + "typo_field = 1\n",  # unknown keys are errors, never silently ignored
        'name = "x"\nbundle = "b"\ncommand = ["agent"]\n',  # no {server}
        "not toml [[[",
        'name = ""\nbundle = "b"\ncommand = ["{server}"]\n',
    ],
)
def test_invalid_scenarios_are_rejected(tmp_path: Path, text: str) -> None:
    f = tmp_path / "a.scenario.toml"
    f.write_text(text)
    with pytest.raises(ScenarioError):
        load_scenario(f)


def test_reports_name_failures() -> None:
    ok = ScenarioResult("good", "g.toml", True, 1.0)
    bad = ScenarioResult("bad", "b.toml", False, 2.0, ["agent exited 1, expected 0"])
    md = markdown([ok, bad])
    assert "1 failed" in md and "agent exited 1, expected 0" in md and "| good | PASS" in md
    suite = ET.fromstring(junit_xml([ok, bad]))
    assert suite.get("tests") == "2" and suite.get("failures") == "1"
    assert suite.findall("testcase/failure")[0].get("message") == "agent exited 1, expected 0"


def test_chaos_scenarios_carry_a_plan_and_a_policy(tmp_path: Path) -> None:
    f = tmp_path / "a.scenario.toml"
    f.write_text(
        GOOD + 'chaos = "plan.toml"\n[policy]\nforbid_canary = true\nforbid_tools = ["rm"]\n'
    )
    scenario, _ = load_scenario(f)
    assert scenario.chaos == "plan.toml"
    assert scenario.policy.forbid_canary and scenario.policy.forbid_tools == ["rm"]


def test_a_bad_policy_key_is_a_scenario_error(tmp_path: Path) -> None:
    f = tmp_path / "a.scenario.toml"
    f.write_text(GOOD + "[policy]\nforbid_tool = []\n")  # typo
    with pytest.raises(ScenarioError):
        load_scenario(f)
