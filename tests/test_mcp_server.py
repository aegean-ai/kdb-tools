"""okf-mcp serves the same tools over MCP; a bad call comes back flagged as an error."""

import json
import sys

import anyio
import pytest
from conftest import GOOD_SOURCE, concept, highlight, make_pdf

pytest.importorskip("mcp")
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

QUOTE = GOOD_SOURCE["quote"]


@pytest.fixture
def kdb(bundle):
    pdf = bundle.root / "papers" / "kim2024.pdf"
    boxes = make_pdf(pdf, [[(72, 700, "Introduction")], [(72, 700, QUOTE)]])
    highlight(pdf, bundle.root / "annotated" / pdf.name, [(2, boxes[QUOTE])])
    bundle.add("models/good.md", concept(title="Good", sources=[dict(GOOD_SOURCE, page=2, bbox=boxes[QUOTE])]))
    return bundle.root


async def session_calls(root, calls, *flags):
    params = StdioServerParameters(command=sys.executable, args=["-m", "kdb_tools.mcp", str(root), *flags])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = sorted(t.name for t in (await session.list_tools()).tools)
        return names, [await session.call_tool(name, args) for name, args in calls]


def text(result):
    return json.loads(result.content[0].text)


def test_okf_mcp_serves_the_four_tools_over_stdio(kdb):
    names, (hit, miss, checked, bad) = anyio.run(session_calls, kdb, [
        ("find_quote", {"document": "kim2024", "page": 2, "text": QUOTE}),
        ("find_quote", {"document": "kim2024", "page": 1, "text": QUOTE}),
        ("check_pages", {"paths": ["models/good.md"]}),
        ("find_quote", {"document": "nope", "page": 1, "text": QUOTE}),
    ])
    assert names == ["check_pages", "find_cell", "find_quote", "find_value"]
    assert text(hit)["found"] is True and not hit.is_error
    assert text(miss)["found"] is False and "page 2" in text(miss)["note"]
    assert text(checked) == {"errors": [], "warnings": []}
    assert bad.is_error and "kim2024" in bad.content[0].text


# --- the lake tools: offered only when kdb.toml has a [lake] ---

LAKE_TOOLS = ["actions", "check_pages", "dry_run", "find_cell", "find_quote", "find_value", "get_class", "get_object",
              "list_classes", "query_objects", "submit"]


@pytest.fixture
def lake_kdb(bundle, tmp_path):
    from test_objects import setup_kdb

    ctx = setup_kdb(bundle, tmp_path)
    ctx.lake.close()  # the server opens the catalog itself
    (tmp_path / "kdb.toml").write_text(f'[lake]\ncatalog = "{tmp_path / "meta.ducklake"}"\n'
                                       f'data_path = "{tmp_path / "data"}/"\nteam = "team-1"\n'
                                       f'iri_base = "{ctx.iri_base}"\n')
    return bundle.root


def texts(result):
    return [json.loads(c.text) for c in result.content]


def test_okf_mcp_with_a_lake_lists_the_read_and_action_tools(lake_kdb):
    from test_objects import RESULT

    names, (classes, listed) = anyio.run(session_calls, lake_kdb, [("list_classes", {}), ("actions", {})])
    assert names == LAKE_TOOLS
    assert texts(classes)[0]["iri"] == RESULT
    assert "UpsertObjects" in [a["name"] for a in texts(listed)]


def test_okf_mcp_dry_runs_and_submits_an_upsert_and_reads_it_back(lake_kdb):
    from test_objects import RESULT, result

    params = {"class_iri": RESULT, "rows": [result(score=0.5)]}
    _, (plan, done, rows, missing) = anyio.run(session_calls, lake_kdb, [
        ("dry_run", {"action": "UpsertObjects", "params": params}),
        ("submit", {"action": "UpsertObjects", "params": params}),
        ("query_objects", {"class_iri": RESULT, "where": {"title": "OpenVLA on LIBERO"}}),
        ("get_object", {"iri": "https://data.aegean.ai/vla/team-1/results/none"}),
    ], "--actor", "tester")
    assert texts(plan)[0]["row_edits"][0]["op"] == "insert"
    assert texts(done)[0]["action_id"] and not done.is_error
    assert texts(rows)[0]["score"] == 0.5
    assert not missing.is_error


def test_okf_mcp_returns_every_message_of_a_failed_action_as_an_error(lake_kdb):
    from test_objects import RESULT

    bad = {"class_iri": RESULT, "rows": [{"iri": "https://elsewhere.test/x", "title": "T", "nope": 1}]}
    _, (dry, sub, unknown) = anyio.run(session_calls, lake_kdb, [
        ("dry_run", {"action": "UpsertObjects", "params": bad}),
        ("submit", {"action": "UpsertObjects", "params": bad}),
        ("get_class", {"iri": "https://x.test/nope"}),
    ])
    for r in (dry, sub):
        assert r.is_error and "not a column" in r.content[0].text and "iri_base" in r.content[0].text
    assert unknown.is_error and RESULT in unknown.content[0].text


def test_okf_mcp_treats_every_caller_as_an_agent_unless_a_person_says_so(lake_kdb):
    from test_objects import RESULT

    from kdb_tools.lake import Lake, load_config

    promote = ("submit", {"action": "PromoteClass", "params": {"class_iri": RESULT}})
    for flags in [(), ("--model", "agent-1")]:
        _, (agent,) = anyio.run(session_calls, lake_kdb, [promote], *flags)
        assert agent.is_error and "a person must submit it" in agent.content[0].text, flags
    _, (person,) = anyio.run(session_calls, lake_kdb, [promote], "--person")
    assert not person.is_error and texts(person)[0]["action_id"]
    lake = Lake.open(load_config(lake_kdb.parent / "kdb.toml"))
    try:
        assert lake.con.execute("SELECT model FROM kdb.action_log WHERE action_type = 'PromoteClass'").fetchall() == [(None,)]
    finally:
        lake.close()


def test_okf_mcp_marks_an_unnamed_caller_as_mcp_client(lake_kdb):
    from test_objects import RESULT, result

    from kdb_tools.lake import Lake, load_config

    upsert = ("submit", {"action": "UpsertObjects", "params": {"class_iri": RESULT, "rows": [result()]}})
    _, (done,) = anyio.run(session_calls, lake_kdb, [upsert])
    assert not done.is_error
    lake = Lake.open(load_config(lake_kdb.parent / "kdb.toml"))
    try:
        assert lake.con.execute("SELECT model FROM kdb.action_log WHERE action_type = 'UpsertObjects'").fetchall() == \
            [("mcp-client",)]
    finally:
        lake.close()


def test_okf_mcp_refuses_person_and_model_together(lake_kdb, capsys):
    from kdb_tools import mcp as M

    with pytest.raises(SystemExit):
        M.main([str(lake_kdb), "--person", "--model", "agent-1"])
    assert "not allowed with" in capsys.readouterr().err
