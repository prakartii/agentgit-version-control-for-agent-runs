"""Shared command line for the demo servers: stdio by default, or HTTP."""

import argparse

from mcp.server import MCPServer


def run(server: MCPServer) -> None:
    parser = argparse.ArgumentParser(description=f"{server.name} demo MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
