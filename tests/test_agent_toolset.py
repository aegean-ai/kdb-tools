"""kdb_tools.agent: the checks as Pydantic AI tools, with results a model can act on."""

import json

import pytest
from conftest import GOOD_SOURCE, concept, highlight, make_pdf

pytest.importorskip("pydantic_ai")
from pydantic_ai import ModelRetry  # noqa: E402

from kdb_tools import agent as A  # noqa: E402

QUOTE = GOOD_SOURCE["quote"]


@pytest.fixture
def tools(bundle):
    pdf = bundle.root / "papers" / "kim2024.pdf"
    boxes = make_pdf(pdf, [[(72, 700, "Introduction")], [(72, 700, QUOTE), (300, 600, "76.5")]])
    highlight(pdf, bundle.root / "annotated" / pdf.name, [(2, boxes[QUOTE])])
    bundle.add("models/good.md", concept(title="Good", sources=[dict(GOOD_SOURCE, page=2, bbox=boxes[QUOTE])]))
    bundle.add("models/bad.md", concept(title="Bad", generated={"by": "x"}, sources=[dict(GOOD_SOURCE, page=2, bbox=boxes[QUOTE])]))
    ts = A.toolset(bundle.root, profiles=[])
    return {name: t.function for name, t in ts.tools.items()}, boxes


def test_the_toolset_has_the_four_tools(tools):
    assert sorted(tools[0]) == ["check_pages", "find_cell", "find_quote", "find_value"]


def test_find_quote_gives_the_bbox_or_says_where_the_text_is(tools):
    fns, boxes = tools
    hit = fns["find_quote"](document="kim2024", page=2, text=QUOTE)
    assert hit.found and hit.boxes == [pytest.approx(boxes[QUOTE], abs=1)]
    miss = fns["find_quote"](document="kim2024", page=1, text=QUOTE)
    assert not miss.found and "page 2" in miss.note


def test_find_value_finds_a_table_number(tools):
    fns, _ = tools
    assert fns["find_value"](document="kim2024", page=2, value="76.5").found


def test_an_unknown_document_is_sent_back_with_the_documents_there_are(tools):
    fns, _ = tools
    with pytest.raises(ModelRetry, match="kim2024"):
        fns["find_quote"](document="nope", page=1, text=QUOTE)


def test_check_pages_reports_only_the_pages_asked_about(tools):
    fns, _ = tools
    assert fns["check_pages"](paths=["models/good.md"]).errors == []
    bad = fns["check_pages"](paths=["models/bad.md"])
    assert [e.rule for e in bad.errors] == ["project.generated"] and bad.errors[0].path == "models/bad.md"


def test_find_cell_reads_a_spreadsheet(bundle):
    from conftest import make_xlsx

    make_xlsx(bundle.root / "documents" / "ar4-dh.xlsx", {"DH": [["joint", "d (mm)"], ["J1", 169.77]]})
    fns = {name: t.function for name, t in A.toolset(bundle.root, profiles=[]).tools.items()}
    assert fns["find_cell"](document="ar4-dh", value="169.77").cells == ["DH!B2"]
    with pytest.raises(ModelRetry, match="DH"):
        fns["find_cell"](document="ar4-dh", value="169.77", sheet="Joints")


# --- the type layer and the actions, for an agent with a lake ---

import dataclasses  # noqa: E402

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402
from test_objects import CORE, RESULT, T1, result, row, setup_kdb  # noqa: E402

from kdb_tools.actions import Failed  # noqa: E402

ACTIVE_MESSAGE = "this action touches an active class; a person must submit it"
LAKE_TOOLS = ["actions", "check_pages", "dry_run", "find_cell", "find_quote", "find_value", "get_class", "get_object",
              "list_classes", "query_objects", "submit"]


@pytest.fixture
def person(bundle, tmp_path):
    c = setup_kdb(bundle, tmp_path)
    yield c
    c.lake.close()


@pytest.fixture
def agent_ctx(person):
    return dataclasses.replace(person, model="agent-1", actor="ingest-agent")


def lake_tools(ctx):
    return {name: t.function for name, t in A.toolset(ctx.root, profiles=[], ctx=ctx).tools.items()}


def test_a_lake_context_adds_the_read_and_action_tools(agent_ctx):
    assert sorted(lake_tools(agent_ctx)) == LAKE_TOOLS


def test_a_scripted_agent_dry_runs_then_submits_and_the_row_exists(agent_ctx):
    rows = [result(score=0.5)]
    params = {"class_iri": RESULT, "rows": rows}
    steps = [("dry_run", {"action": "UpsertObjects", "params": params}),
             ("submit", {"action": "UpsertObjects", "params": params})]
    seen = []

    def respond(messages, info):
        done = sum(1 for m in messages for p in m.parts if p.part_kind == "tool-return")
        seen.extend(p.content for m in messages for p in m.parts if p.part_kind == "tool-return")
        if done < len(steps):
            name, args = steps[done]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(respond), toolsets=[A.toolset(agent_ctx.root, profiles=[], ctx=agent_ctx)])
    assert agent.run_sync("add the result").output == "done"
    assert seen[0]["row_edits"][0]["op"] == "insert"
    assert seen[1]["action_id"]
    assert row(agent_ctx, rows[0]["iri"])["score"] == 0.5


def test_a_failing_action_comes_back_as_a_retry_with_every_message(agent_ctx):
    fns = lake_tools(agent_ctx)
    bad = {"class_iri": RESULT, "rows": [{"iri": "https://elsewhere.test/x", "title": "T", "nope": 1}]}
    for call in ("dry_run", "submit"):
        with pytest.raises(ModelRetry) as e:
            fns[call](action="UpsertObjects", params=bad)
        assert "not a column" in str(e.value) and "iri_base" in str(e.value)
    with pytest.raises(ModelRetry, match="UpsertObjects"):
        fns["dry_run"](action="Nope", params={})


def test_a_retry_prompt_reaches_the_scripted_model(agent_ctx):
    seen = []

    def respond(messages, info):
        # a retry comes back as a RetryPromptPart, or in later releases as a ToolReturnPart
        seen.extend(str(p.content) for m in messages for p in m.parts if isinstance(p, (RetryPromptPart, ToolReturnPart)))
        if not seen:
            return ModelResponse(parts=[ToolCallPart("dry_run", {"action": "UpsertObjects", "params": {"class_iri": "x"}})])
        return ModelResponse(parts=[TextPart("ok")])

    Agent(FunctionModel(respond), toolsets=[A.toolset(agent_ctx.root, profiles=[], ctx=agent_ctx)]).run_sync("go")
    assert seen and "rows" in seen[0]


def test_an_agent_cannot_promote_a_class_but_a_person_can(agent_ctx, person):
    with pytest.raises(ModelRetry) as e:
        lake_tools(agent_ctx)["submit"](action="PromoteClass", params={"class_iri": RESULT})
    assert str(e.value) == ACTIVE_MESSAGE
    assert lake_tools(person)["submit"](action="PromoteClass", params={"class_iri": RESULT})["action_id"]
    assert lake_tools(person)["get_class"](iri=RESULT)["status"] == "active"


def test_an_agent_writes_rows_of_an_active_class(agent_ctx, person):
    lake_tools(person)["submit"](action="PromoteClass", params={"class_iri": RESULT})
    params = {"class_iri": RESULT, "rows": [result()]}
    assert lake_tools(agent_ctx)["submit"](action="UpsertObjects", params=params)["action_id"]
    assert row(agent_ctx, params["rows"][0]["iri"]) is not None


def test_an_agent_cannot_edit_the_page_of_an_active_class(agent_ctx, person):
    fns, mine = lake_tools(agent_ctx), lake_tools(person)
    mine["submit"](action="PromoteClass", params={"class_iri": RESULT})
    mine["submit"](action="ProposeClass", params={"domain": "core", "slug": "score", "title": "Score",
                                                 "description": "A score."})
    params = {"class_iri": RESULT, "reason": "replaced", "deadline": "2027-01-01", "replacement": CORE + "classes/score"}
    with pytest.raises(ModelRetry) as e:
        fns["submit"](action="DeprecateClass", params=params)
    assert str(e.value) == ACTIVE_MESSAGE
    assert mine["get_class"](iri=RESULT)["status"] == "active"


def test_reads_come_back_as_json(agent_ctx):
    fns = lake_tools(agent_ctx)
    fns["submit"](action="UpsertObjects", params={"class_iri": RESULT, "rows": [
        result(score=0.5, published="2026-01-02", run_at="2026-01-02T03:04:05+00:00", tags=["a", "b"], meta={"k": [1]}),
        result(iri=T1 + "results/other", title="Other")]})
    json.dumps(fns["query_objects"](class_iri=RESULT))  # must serialize
    (hit,) = fns["query_objects"](class_iri=RESULT, where={"title": "OpenVLA on LIBERO"})
    assert hit["published"] == "2026-01-02" and hit["run_at"].startswith("2026-01-02T03:04:05")
    assert hit["tags"] == ["a", "b"] and hit["meta"] == {"k": [1]} and hit["type"] == RESULT
    assert len(fns["query_objects"](class_iri=RESULT, limit=1)) == 1
    assert fns["get_object"](iri=T1 + "results/other")["title"] == "Other"
    assert fns["get_object"](iri=T1 + "results/none") is None
    assert [c["iri"] for c in fns["list_classes"]()] == [RESULT]
    info = fns["get_class"](iri=RESULT)
    assert info["status"] == "experimental" and info["binding"]["table"] == "results"
    assert info["properties"]["score"]["datatype"] == "double"
    with pytest.raises(ModelRetry, match="columns are"):
        fns["query_objects"](class_iri=RESULT, where={"nope": "1"})


def test_an_unknown_class_names_the_known_ones(agent_ctx):
    for call in ("get_class", "query_objects"):
        kwargs = {"iri" if call == "get_class" else "class_iri": "https://x.test/nope"}
        with pytest.raises(ModelRetry, match=RESULT):
            lake_tools(agent_ctx)[call](**kwargs)


def test_actions_list_the_registered_types_with_their_schema(agent_ctx):
    listed = {a["name"]: a for a in lake_tools(agent_ctx)["actions"]()}
    assert {"UpsertObjects", "PromoteClass", "AdoptBundle", "BindTable"} <= set(listed)
    assert listed["PromoteClass"]["params"]["properties"]["class_iri"]["type"] == "string"
    json.dumps(listed)


def test_check_pages_with_a_lake_sees_unlogged_pages(agent_ctx):
    (agent_ctx.root / "models").mkdir(exist_ok=True)
    (agent_ctx.root / "models" / "sneaky.md").write_text("---\ntype: Model\ntitle: Sneaky\n---\nText.\n")
    bad = lake_tools(agent_ctx)["check_pages"](paths=["models/sneaky.md"])
    assert "action.unlogged" in [e.rule for e in bad.errors]


def test_an_agent_cannot_seed_active_classes(bundle, tmp_path):
    from test_actions_types import core_spec, make_ctx

    from kdb_tools.actions.adopt import AdoptBundle
    from kdb_tools.tools import KdbTools

    c = make_ctx(bundle.root, tmp_path)
    try:
        AdoptBundle().submit(c, {})
        agent = KdbTools(bundle.root, [], dataclasses.replace(c, model="agent-1", actor="agent"))
        with pytest.raises(Failed) as e:
            agent.submit("SeedOntology", {"spec": core_spec()})
        assert str(e.value) == ACTIVE_MESSAGE
        assert not (bundle.root / "ontology" / "core").exists()
    finally:
        c.lake.close()


def test_an_agent_cannot_register_a_file_outside_the_kdb(agent_ctx, tmp_path):
    from kdb_tools.tools import KdbTools

    outside = tmp_path / "elsewhere" / "secret.pdf"
    outside.parent.mkdir()
    outside.write_bytes(b"%PDF-1.4 private")
    agent = KdbTools(agent_ctx.root, [], agent_ctx)
    with pytest.raises(Failed) as e:
        agent.submit("RegisterMedia", {"path": str(outside), "origin": "x"})
    assert str(e.value) == f"an agent registers files inside the kDB only; {outside} is outside {agent_ctx.root}"
    assert agent_ctx.lake.con.execute("SELECT count(*) FROM kdb.media").fetchone()[0] == 0
    inside = agent_ctx.root / "papers" / "kim2024.pdf"
    inside.parent.mkdir(exist_ok=True)
    inside.write_bytes(b"%PDF-1.4 public")
    assert agent.submit("RegisterMedia", {"path": "papers/kim2024.pdf", "origin": "x"})["action_id"]
