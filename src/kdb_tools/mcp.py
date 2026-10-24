"""okf-mcp: the kDB tools over MCP, for agents in any framework. Needs the mcp extra: kdb-tools[mcp].

    okf-mcp kdb                      # stdio, for a local agent
    claude mcp add kdb -- okf-mcp kdb

A call the agent should correct, such as an unknown document, comes back as an MCP error result
with a message that says how to fix it, never as a normal result with an error inside.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import profile as P
from .report import fail
from .tools import KdbTools, ToolInputError

TOOL = "okf-mcp"


def _located(call):
    """Run a lookup; an input the agent should correct becomes a ToolError, whose message MCP passes on.

    Any other exception is treated by the MCP SDK as a crash, and the model sees only its name."""
    try:
        return asdict(call())
    except ToolInputError as e:
        raise ToolError(str(e)) from e


def server(root: Path, profiles=None) -> MCPServer:
    kt = KdbTools(root, profiles)
    srv = MCPServer("kdb-tools", instructions=(
        "Tools for one kDB bundle. find_quote and find_value say where a sentence or a table value is in a PDF, "
        "and find_cell which spreadsheet cells hold a value, the way okf-ground looks for them. check_pages runs okf-validate and okf-ground on the pages you name."))

    @srv.tool()
    def find_quote(document: str, page: int, text: str) -> dict:
        """Find a sentence on a page of a PDF. document is the file name without .pdf, such as 2406.09246 or
        ar4-manual, and page counts from 1. Returns the bounding boxes where the sentence is, or a note saying
        which page it is on."""
        return _located(lambda: kt.find_quote(document, page, text))

    @srv.tool()
    def find_value(document: str, page: int, value: str) -> dict:
        """Find a table value, such as 76.5, on a page of a PDF. Returns its bounding boxes, or a note."""
        return _located(lambda: kt.find_value(document, page, value))

    @srv.tool()
    def find_cell(document: str, value: str, sheet: str | None = None) -> dict:
        """Find the cells of a spreadsheet (.xlsx or .csv) that hold a value, such as 169.77. document is the
        file name without its suffix, and sheet limits the search to one sheet. Returns cells such as DH!B4."""
        return _located(lambda: kt.find_cell(document, value, sheet))

    @srv.tool()
    def check_pages(paths: list[str]) -> dict:
        """Run okf-validate and okf-ground on kDB pages, such as models/openvla.md, and return their errors
        and warnings."""
        return asdict(kt.check_pages(paths))

    return srv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="the bundle root directory")
    parser.add_argument("--profile", help="check with only this installed profile's rules")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        return fail(TOOL, f"{args.bundle} is not a directory")
    profiles = P.installed(args.profile)
    if args.profile and not profiles:
        return fail(TOOL, f"no installed profile is called {args.profile}")
    server(args.bundle, profiles).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
