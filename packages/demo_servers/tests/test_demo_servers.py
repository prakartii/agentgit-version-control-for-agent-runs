"""Exercise the demo servers in memory with the real SDK client."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from agentgit_demo_servers import files, tickets


def call(server: Any, tool: str, **arguments: Any) -> Any:
    async def go() -> Any:
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEMO_FILES_ROOT", str(tmp_path))
    return tmp_path


def test_files_write_read_list_roundtrip(sandbox: Path) -> None:
    call(files.mcp, "write_file", path="notes/a.txt", content="hello")
    assert (sandbox / "notes" / "a.txt").read_text() == "hello"
    assert call(files.mcp, "read_file", path="notes/a.txt").structured_content == {
        "result": "hello"
    }
    listing = call(files.mcp, "list_files", path="notes")
    assert listing.structured_content == {"result": ["a.txt"]}


def test_files_reject_paths_outside_the_sandbox() -> None:
    result = call(files.mcp, "read_file", path="../outside.txt")
    assert result.is_error is True
    assert "escapes the sandbox" in result.content[0].text


def test_tickets_get_search_update() -> None:
    assert call(tickets.mcp, "get_ticket", ticket_id="T-100").structured_content["subject"] == (
        "Cannot log in"
    )
    assert call(tickets.mcp, "search_tickets", query="charged").structured_content == {
        "result": ["T-101"]
    }
    updated = call(tickets.mcp, "update_ticket", ticket_id="T-100", status="pending", note="n")
    assert updated.structured_content["status"] == "pending"


def test_tickets_report_bad_input_as_tool_errors() -> None:
    missing = call(tickets.mcp, "get_ticket", ticket_id="nope")
    assert missing.is_error is True and "no such ticket: nope" in missing.content[0].text
    weird = call(tickets.mcp, "update_ticket", ticket_id="T-100", status="weird")
    assert weird.is_error is True and "invalid status" in weird.content[0].text
