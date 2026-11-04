"""A tiny in-memory support-ticket server."""

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agentgit_demo_servers._run import run

mcp = MCPServer("demo-tickets")

TICKETS: dict[str, dict[str, str]] = {
    "T-100": {
        "subject": "Cannot log in",
        "body": "Password reset email never arrives.",
        "status": "open",
    },
    "T-101": {
        "subject": "Invoice is wrong",
        "body": "I was charged twice in March.",
        "status": "open",
    },
    "T-102": {
        "subject": "Feature request",
        "body": "Please add dark mode.",
        "status": "closed",
    },
}


@mcp.tool()
def get_ticket(ticket_id: str) -> dict[str, str]:
    """Get one ticket by id."""
    if ticket_id not in TICKETS:
        raise ToolError(f"no such ticket: {ticket_id}")
    return {"id": ticket_id, **TICKETS[ticket_id]}


@mcp.tool()
def search_tickets(query: str) -> list[str]:
    """Return ids of tickets whose subject or body contains the query."""
    needle = query.lower()
    return sorted(
        tid
        for tid, t in TICKETS.items()
        if needle in t["subject"].lower() or needle in t["body"].lower()
    )


@mcp.tool()
def update_ticket(ticket_id: str, status: str, note: str = "") -> dict[str, str]:
    """Set a ticket's status (open, pending or closed) with an optional note."""
    if ticket_id not in TICKETS:
        raise ToolError(f"no such ticket: {ticket_id}")
    if status not in {"open", "pending", "closed"}:
        raise ToolError(f"invalid status: {status}")
    TICKETS[ticket_id]["status"] = status
    if note:
        TICKETS[ticket_id]["note"] = note
    return {"id": ticket_id, **TICKETS[ticket_id]}


def main() -> None:
    run(mcp)


if __name__ == "__main__":
    main()
