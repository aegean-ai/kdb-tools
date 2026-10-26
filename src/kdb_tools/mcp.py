"""okf-mcp: the kDB tools over MCP, for agents in any framework. Needs the mcp extra: kdb-tools[mcp].

    okf-mcp kdb                      # stdio, for a local agent
    claude mcp add kdb -- okf-mcp kdb
    okf-mcp kdb --person             # for you, when you drive the tools yourself

Every caller is treated as an agent: its actions are logged with the model given by --model, or
mcp-client when none is given, and an action that edits an active Class page is refused. --person
says that a person is calling, for a server you run for yourself.

A call the agent should correct, such as an unknown document, comes back as an MCP error result
with a message that says how to fix it, never as a normal result with an error inside.
"""

from __future__ import annotations

import argparse
import getpass
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import lake as L
from . import profile as P
from .actions import Context, Failed
from .report import fail
from .tools import KdbTools, ToolInputError
from .validate import find_config

TOOL = "okf-mcp"
CLIENT = "mcp-client"  # the model logged for a caller that names none: an MCP caller is an agent


def _located(call):
    """Run a lookup; an input the agent should correct becomes a ToolError, whose message MCP passes on.

    Any other exception is treated by the MCP SDK as a crash, and the model sees only its name."""
    try:
        out = call()
        return asdict(out) if hasattr(out, "__dataclass_fields__") else out
    except ToolInputError as e:
        raise ToolError(str(e)) from e
    except Failed as e:  # a failed or conflicting action: every message
        raise ToolError("\n".join(e.messages)) from e


def server(root: Path, profiles=None, ctx: Context | None = None) -> MCPServer:
    kt = KdbTools(root, profiles, ctx)
    srv = MCPServer("kdb-tools", instructions=(
        "Tools for one kDB bundle. find_quote and find_value say where a sentence or a table value is in a PDF, "
        "and find_cell which spreadsheet cells hold a value, the way okf-ground looks for them. check_pages runs okf-validate and okf-ground on the pages you name. "
        "With a lake, list_classes and get_class read the types, query_objects and get_object read rows, actions lists the "
        "action types, dry_run shows an action's plan, and submit applies it. Submit is the only way to change a page or a row. "
        "Callers are agents unless the server was started with --person, so a submit that edits an active Class page is "
        "refused: a person must submit it."))

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
        """Find the cells of a spreadsheet (.xlsx, .xlsm or .csv) that hold a value, such as 169.77. document is the
        file name without its suffix, and sheet limits the search to one sheet. Returns cells such as DH!B4."""
        return _located(lambda: kt.find_cell(document, value, sheet))

    @srv.tool()
    def check_pages(paths: list[str]) -> dict:
        """Run okf-validate and okf-ground on kDB pages, such as models/openvla.md, and return their errors
        and warnings."""
        return asdict(kt.check_pages(paths))

    if ctx is not None:
        _lake_tools(srv, kt)
    return srv


def _lake_tools(srv: MCPServer, kt: KdbTools) -> None:
    @srv.tool()
    def list_classes() -> list[dict]:
        """Every class in the kDB with its status (experimental, active or deprecated) and bound table."""
        return _located(kt.list_classes)

    @srv.tool()
    def get_class(iri: str) -> dict:
        """One class: status, supertypes, interfaces, binding, constraints and its properties by column."""
        return _located(lambda: kt.get_class(iri))

    @srv.tool()
    def query_objects(class_iri: str, where: dict[str, str] | None = None, limit: int = 50) -> list[dict]:
        """Rows of a class's table. where maps column names to values the column must equal, compared as text."""
        return _located(lambda: kt.query_objects(class_iri, where, limit))

    @srv.tool()
    def get_object(iri: str) -> dict | None:
        """The row with this iri, from whichever class's table holds it, or null."""
        return _located(lambda: kt.get_object(iri))

    @srv.tool()
    def actions() -> list[dict]:
        """Every action you can run, with its version and the JSON schema of its params."""
        return _located(kt.actions)

    @srv.tool()
    def dry_run(action: str, params: dict) -> dict:
        """The plan an action would carry out (pages and rows it would edit). It changes nothing."""
        return _located(lambda: kt.dry_run(action, params))

    @srv.tool()
    def submit(action: str, params: dict) -> dict:
        """Run an action. It is the only way to change a page or a row. An agent's action that edits an active Class
        page is refused: a person must submit it. Returns the action_id."""
        return _located(lambda: kt.submit(action, params))


def open_context(root: Path, actor: str, model: str | None, config: Path | None = None) -> Context | None:
    """A Context for the lake in the nearest kdb.toml, or None when it has no [lake]."""
    config = config or find_config(root)
    cfg = L.load_config(config) if config else None
    if cfg is None:
        return None
    lake = L.Lake.open(cfg)
    lake.ensure_system_tables()
    return Context(root=root, lake=lake, store=L.store_for(cfg), actor=actor, model=model, team=cfg.team,
                   iri_base=cfg.iri_base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="the bundle root directory")
    parser.add_argument("--profile", help="check with only this installed profile's rules")
    parser.add_argument("--actor", default=getpass.getuser(), help="who acts, recorded in the action log; default: the OS user")
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--model", default=CLIENT,
                     help=f"the agent's model id, recorded in the action log; default: {CLIENT}. An agent's submits "
                          "cannot edit an active Class page")
    who.add_argument("--person", action="store_true",
                     help="a person calls the tools, not an agent: for a server you run for yourself")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        return fail(TOOL, f"{args.bundle} is not a directory")
    profiles = P.installed(args.profile)
    if args.profile and not profiles:
        return fail(TOOL, f"no installed profile is called {args.profile}")
    try:
        ctx = open_context(args.bundle, args.actor, None if args.person else args.model)
    except Exception as e:  # a bad kdb.toml, a missing secret or an unreachable lake
        return fail(TOOL, f"cannot open the lake: {e}")
    try:
        server(args.bundle, profiles, ctx).run("stdio")
    finally:
        if ctx is not None:
            ctx.lake.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
