"""Scenario files: what to replay, how to start the agent, what counts as a pass.

A scenario is a small TOML file next to a committed bundle (see core/bundle.py)::

    name = "coding agent fixes add()"
    bundle = "fix-add.bundle.json"
    command = ["{python}", "-m", "my_agent", "--mcp", "{server}"]
    expect_exit = 0
    stdout_contains = ["Fixed"]

``{server}`` expands to the replay server command (several arguments) and
``{python}`` to the current interpreter. The command runs with the scenario's
directory as its working directory, so relative paths (such as a committed
model-response cache) resolve next to the file.
"""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SERVER_TOKEN = "{server}"
PYTHON_TOKEN = "{python}"


class ScenarioError(ValueError):
    """A scenario file is unreadable or invalid."""


class Scenario(BaseModel):
    # extra="forbid": a typo such as `expect_exit_code` must fail loudly, not be ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    bundle: str  # path to the bundle, relative to this file
    command: list[str] = Field(min_length=1)
    expect_exit: int = 0
    # Replay divergence fails the scenario unless explicitly allowed.
    allow_divergence: bool = False
    stdout_contains: list[str] = []
    timeout_seconds: float = Field(default=120.0, gt=0)
    env: dict[str, str] = {}

    def has_server_token(self) -> bool:
        return SERVER_TOKEN in self.command


def load_scenario(path: Path) -> tuple[Scenario, Path]:
    """Parse one scenario file. Returns it with its directory (the working dir)."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        scenario = Scenario.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as e:
        raise ScenarioError(f"{path}: {e}") from None
    if not scenario.has_server_token():
        raise ScenarioError(
            f"{path}: command must contain {SERVER_TOKEN} (where the MCP server goes)"
        )
    return scenario, path.parent


def find_scenarios(targets: list[Path]) -> list[Path]:
    """Expand files and directories into scenario files (``*.scenario.toml``), sorted."""
    found: list[Path] = []
    for target in targets:
        if target.is_dir():
            found.extend(sorted(target.rglob("*.scenario.toml")))
        elif target.is_file():
            found.append(target)
        else:
            raise ScenarioError(f"{target}: no such file or directory")
    return found
