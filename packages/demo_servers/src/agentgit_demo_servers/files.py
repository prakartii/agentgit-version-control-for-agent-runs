"""A tiny sandboxed file server: read, write and list files under one root.

The root comes from the DEMO_FILES_ROOT environment variable. Paths that
escape it are rejected, so an agent cannot read or write elsewhere.
"""

import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agentgit_demo_servers._run import run

mcp = MCPServer("demo-files")


def _root() -> Path:
    return Path(os.environ.get("DEMO_FILES_ROOT", ".")).resolve()


def _safe(path: str) -> Path:
    root = _root()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"path escapes the sandbox: {path}")
    return target


@mcp.tool()
def read_file(path: str) -> str:
    """Read a UTF-8 text file."""
    return _safe(path).read_text(encoding="utf-8")


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write a UTF-8 text file, creating parent directories."""
    target = _safe(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} characters to {path}"


@mcp.tool()
def list_files(path: str = ".") -> list[str]:
    """List file names directly inside a directory."""
    return sorted(p.name for p in _safe(path).iterdir())


def main() -> None:
    run(mcp)


if __name__ == "__main__":
    main()
