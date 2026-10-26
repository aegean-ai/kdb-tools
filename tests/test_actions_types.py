"""The type actions: every change to a kDB's types goes through them, and each leaves okf-validate clean."""

import json
from datetime import timezone

import pytest
import yaml
from conftest import domain, interface, klass, prop

from pydantic import BaseModel

from kdb_tools.actions import REGISTRY, ActionType, Context, Failed, RowEdit, register
from kdb_tools.actions.adopt import AdoptBundle
from kdb_tools.actions.types import (
    AddConstraint, AddProperty, BindTable, DeprecateClass, ImplementInterface, PromoteClass, ProposeClass,
    ReviseConstraint, RevertAction, SeedOntology,
)
from kdb_tools.lake import Lake, LakeConfig, LocalStore
from kdb_tools.seed import CORE_SEEDS, main as seed_main
from kdb_tools.validate import validate

CORE = "https://kdb.aegean.ai/core/"
ROD = CORE + "classes/rod"
BAR = CORE + "classes/bar"
LENGTH = CORE + "properties/length"
TITLE = CORE + "properties/title"
ACTIVE = f"{ROD} is active; deprecate it and propose a replacement"


def types(bundle, rod_status="experimental"):
    domain(bundle, "core", CORE)
    prop(bundle, "core", "title", "string")
    prop(bundle, "core", "length", "double", unit="unit:M")
    prop(bundle, "core", "label", "string")
    interface(bundle, "core", "lengthy", properties=[("length", True)])
    klass(bundle, "core", "rod", table="rods", columns={"title": "title", "length_m": "length"}, status=rod_status)
    klass(bundle, "core", "bar", table="bars", columns={"title": "title"})


def make_ctx(root, tmp_path, actor="a1"):
    cfg = LakeConfig(catalog=tmp_path / "meta.ducklake", data_path=str(tmp_path / "data") + "/", team="t",
                     iri_base="https://data.aegean.ai/vla/t/")
    lake = Lake.open(cfg)
    lake.ensure_system_tables()
    return Context(root=root, lake=lake, store=LocalStore(tmp_path / "data"), actor=actor, model=None, team="t",
                   iri_base=cfg.iri_base)


@pytest.fixture
def ctx(bundle, tmp_path):
    types(bundle)
    c = make_ctx(bundle.root, tmp_path)
    AdoptBundle().submit(c, {})
    yield c
    c.lake.close()


def findings(ctx):
    return [(f.rule, f.path, f.message) for f in validate(ctx.root, profiles=[], lake=ctx.lake).findings]


def fm(ctx, rel):
    return yaml.safe_load(ctx.read(rel).split("\n---\n", 1)[0][4:])


def log_rows(ctx):
    cur = ctx.lake.con.execute('SELECT * FROM kdb.action_log ORDER BY "at", action_id')
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def columns(ctx, table):
    return ctx.lake.con.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_catalog = 'lake' "
        "AND table_schema = 'main' AND table_name = ? ORDER BY ordinal_position", [table]).fetchall()


def propose(ctx, slug="beam", **extra):
    return ProposeClass().submit(ctx, {"domain": "core", "slug": slug, "title": slug.title(),
                                       "description": f"A {slug} is a long rigid part.", **extra})


def test_all_registered():
    for name in ("ProposeClass", "AddProperty", "ImplementInterface", "AddConstraint", "ReviseConstraint",
                 "BindTable", "PromoteClass", "DeprecateClass", "SeedOntology", "RevertAction"):
        assert name in REGISTRY


def test_fixture_is_clean(ctx):
    assert findings(ctx) == []


def test_propose_class(ctx):
    propose(ctx, implements=[CORE + "interfaces/lengthy"], subclass_of=[BAR])
    page = fm(ctx, "ontology/core/classes/beam.md")
    assert (page["type"], page["iri"], page["status"]) == ("Class", CORE + "classes/beam", "experimental")
    assert page["subclass_of"] == [BAR] and page["implements"] == [CORE + "interfaces/lengthy"]
    assert "[Beam](classes/beam.md)" in ctx.read("ontology/core/index.md")
    assert findings(ctx) == []


def test_every_action_logs_one_line_under_todays_heading(ctx):
    propose(ctx)
    [row] = [r for r in log_rows(ctx) if r["action_type"] == "ProposeClass"]
    today = row["action_id"][:8]
    heading = f"## {today[:4]}-{today[4:6]}-{today[6:]}"
    log = ctx.read("log.md")
    assert log.rstrip("\n").splitlines()[-2:] == [heading, f"* **ProposeClass** (a1): proposed class {CORE}classes/beam"]


def test_propose_class_twice_fails(ctx):
    propose(ctx)
    with pytest.raises(Failed) as e:
        propose(ctx)
    assert any("already" in m for m in e.value.messages)


def test_propose_class_needs_the_domain(ctx):
    with pytest.raises(Failed) as e:
        ProposeClass().submit(ctx, {"domain": "nope", "slug": "x", "title": "X", "description": "An x."})
    assert e.value.messages == ["ontology/nope/ontology.md does not exist; seed the nope ontology first"]


def test_propose_class_with_a_bad_binding_fails_with_messages(ctx):
    with pytest.raises(Failed) as e:
        propose(ctx, binding={"table": "beams; DROP", "columns": {"title": "https://nowhere/x"}})
    assert len(e.value.messages) >= 2 and not (ctx.root / "ontology/core/classes/beam.md").exists()


def test_add_property(ctx):
    AddProperty().submit(ctx, {"domain": "core", "slug": "mass", "title": "Mass", "datatype": "double",
                               "unit": "unit:KiloGM", "description": "The mass of a body."})
    page = fm(ctx, "ontology/core/properties/mass.md")
    assert (page["datatype"], page["unit"], page["required"]) == ("double", "unit:KiloGM", False)
    assert findings(ctx) == []


def test_add_property_with_a_unit_on_a_string_fails(ctx):
    with pytest.raises(Failed) as e:
        AddProperty().submit(ctx, {"domain": "core", "slug": "x", "title": "X", "datatype": "string",
                                   "unit": "unit:M", "description": "An x."})
    assert e.value.messages == ["a unit needs a numeric datatype, not 'string'"]


def test_implement_interface(ctx):
    ImplementInterface().submit(ctx, {"class_iri": BAR, "interface_iri": CORE + "interfaces/lengthy",
                                      "column_map": {"length_m": LENGTH}})
    page = fm(ctx, "ontology/core/classes/bar.md")
    assert page["implements"] == [CORE + "interfaces/lengthy"]
    assert page["binding"]["columns"] == {"title": TITLE, "length_m": LENGTH}
    assert findings(ctx) == []


def test_implement_interface_needs_its_required_properties(ctx):
    with pytest.raises(Failed) as e:
        ImplementInterface().submit(ctx, {"class_iri": BAR, "interface_iri": CORE + "interfaces/lengthy",
                                          "column_map": {}})
    assert e.value.messages == [f"required property '{LENGTH}' of interface '{CORE}interfaces/lengthy' "
                                "is not mapped by a column"]


CONSTRAINT = {"property": LENGTH, "nominal": 0.305, "tolerance": 0.0005, "unit": "unit:M",
              "evidence": ["https://data.aegean.ai/solution/observation/1"],
              "tolerance_evidence": ["https://data.aegean.ai/solution/action/1"]}


def test_add_constraint(ctx):
    AddConstraint().submit(ctx, {"class_iri": ROD, "constraint": CONSTRAINT})
    assert fm(ctx, "ontology/core/classes/rod.md")["constraints"] == [{**CONSTRAINT, "version": 1}]
    assert findings(ctx) == []


def test_add_constraint_with_faults_fails(ctx):
    with pytest.raises(Failed) as e:
        AddConstraint().submit(ctx, {"class_iri": ROD, "constraint": {**CONSTRAINT, "unit": "unit:KiloGM"}})
    assert e.value.messages == ["unit 'unit:KiloGM' is a mass, but the property's unit is a length"]


def test_add_constraint_twice_for_one_property_fails(ctx):
    AddConstraint().submit(ctx, {"class_iri": ROD, "constraint": CONSTRAINT})
    with pytest.raises(Failed) as e:
        AddConstraint().submit(ctx, {"class_iri": ROD, "constraint": {**CONSTRAINT, "nominal": 0.3}})
    assert "ReviseConstraint" in e.value.messages[0]


def test_revise_constraint(ctx):
    AddConstraint().submit(ctx, {"class_iri": ROD, "constraint": CONSTRAINT})
    ReviseConstraint().submit(ctx, {"class_iri": ROD, "property": LENGTH, "reason": "a new measurement",
                                    "constraint": {**CONSTRAINT, "nominal": 0.3051}})
    page = fm(ctx, "ontology/core/classes/rod.md")
    assert page["constraints"] == [{**CONSTRAINT, "nominal": 0.3051, "version": 2}]
    assert page["superseded"] == [{**CONSTRAINT, "version": 1, "reason": "a new measurement"}]
    assert findings(ctx) == []


def test_bind_table_creates_the_table(ctx):
    BindTable().submit(ctx, {"class_iri": ROD, "binding": {"table": "rods", "key": "iri", "title": "title",
                                                           "columns": {"title": TITLE, "length_m": LENGTH}}})
    assert columns(ctx, "rods") == [("iri", "VARCHAR"), ("type", "VARCHAR"), ("team", "VARCHAR"),
                                    ("title", "VARCHAR"), ("length_m", "DOUBLE")]
    assert findings(ctx) == []


def test_renaming_an_active_class_table_fails(bundle, tmp_path):
    types(bundle, rod_status="active")
    c = make_ctx(bundle.root, tmp_path)
    AdoptBundle().submit(c, {})
    with pytest.raises(Failed) as e:
        BindTable().submit(c, {"class_iri": ROD, "binding": {"table": "rods2", "columns": {
            "title": TITLE, "length_m": LENGTH}}})
    assert e.value.messages == [ACTIVE]
    c.lake.close()


def bind(ctx, cols, **extra):
    return BindTable().submit(ctx, {"class_iri": ROD, "binding": {"table": "rods", "columns": cols}, **extra})


def test_retyping_a_column_with_rows_needs_a_migration(ctx):
    bind(ctx, {"title": TITLE, "length_m": CORE + "properties/label"})
    ctx.lake.con.execute("INSERT INTO rods(iri, title, length_m) VALUES ('https://data.aegean.ai/vla/t/rod/1', 'r1', '0.5')")
    with pytest.raises(Failed) as e:
        bind(ctx, {"title": TITLE, "length_m": LENGTH})
    assert e.value.messages == ["column length_m of rods has 1 rows and changes type from VARCHAR to DOUBLE; "
                                "give migration cast (or drop)"]
    bind(ctx, {"title": TITLE, "length_m": LENGTH}, migration="cast")
    assert ("length_m", "DOUBLE") in columns(ctx, "rods")
    assert ctx.lake.con.execute("SELECT iri, title, length_m FROM rods").fetchall() == [
        ("https://data.aegean.ai/vla/t/rod/1", "r1", 0.5)]
    assert findings(ctx) == []


def test_a_cast_that_cannot_convert_fails_before_anything_runs(ctx):
    bind(ctx, {"title": TITLE, "length_m": CORE + "properties/label"})
    ctx.lake.con.execute("INSERT INTO rods(iri, title, length_m) VALUES ('https://x/1', 'r1', 'long')")
    with pytest.raises(Failed) as e:
        bind(ctx, {"title": TITLE, "length_m": LENGTH}, migration="cast")
    assert e.value.messages == ["1 values of column length_m of rods do not cast to DOUBLE"]


def test_dropping_a_column_with_rows_needs_drop_or_move(ctx):
    bind(ctx, {"title": TITLE, "length_m": LENGTH})
    ctx.lake.con.execute("INSERT INTO rods(iri, title, length_m) VALUES ('https://x/1', 'r1', 0.5)")
    with pytest.raises(Failed) as e:
        bind(ctx, {"title": TITLE, "len": LENGTH})
    assert e.value.messages == ["column length_m of rods has 1 rows and is not in the binding; "
                                "give migration drop, or move it to a new column"]
    bind(ctx, {"title": TITLE, "len": LENGTH}, migration="move", move={"length_m": "len"})
    assert ctx.lake.con.execute("SELECT len FROM rods").fetchall() == [(0.5,)]
    assert findings(ctx) == []


def test_promote_and_deprecate(ctx):
    PromoteClass().submit(ctx, {"class_iri": ROD})
    assert fm(ctx, "ontology/core/classes/rod.md")["status"] == "active"
    DeprecateClass().submit(ctx, {"class_iri": ROD, "reason": "split in two", "deadline": "2027-01-31",
                                  "replacement": BAR})
    page = fm(ctx, "ontology/core/classes/rod.md")
    assert page["status"] == "deprecated"
    assert page["deprecated"] == {"reason": "split in two", "deadline": "2027-01-31", "replacement": BAR}
    assert findings(ctx) == []


def test_promote_twice_fails(ctx):
    PromoteClass().submit(ctx, {"class_iri": ROD})
    with pytest.raises(Failed):
        PromoteClass().submit(ctx, {"class_iri": ROD})


def test_unknown_class_fails(ctx):
    with pytest.raises(Failed) as e:
        PromoteClass().submit(ctx, {"class_iri": CORE + "classes/nope"})
    assert e.value.messages == [f"{CORE}classes/nope is not a Class in this bundle"]


def test_revert_by_another_actor_fails(ctx, tmp_path):
    aid = PromoteClass().submit(ctx, {"class_iri": ROD})
    ctx.actor = "someone-else"
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": aid})
    assert e.value.messages == [f"{aid} was made by a1; only a1 can revert it"]


def test_revert_restores_the_page_byte_for_byte(ctx):
    rel = "ontology/core/classes/rod.md"
    before = (ctx.root / rel).read_bytes()
    aid = AddConstraint().submit(ctx, {"class_iri": ROD, "constraint": CONSTRAINT})
    rid = RevertAction().submit(ctx, {"action_id": aid})
    assert (ctx.root / rel).read_bytes() == before
    rows = {r["action_id"]: r for r in log_rows(ctx)}
    assert rows[aid]["reverted_by"] == rid and rows[rid]["reverts"] == aid
    assert findings(ctx) == []


def test_revert_of_a_created_page_deletes_it(ctx):
    aid = propose(ctx)
    RevertAction().submit(ctx, {"action_id": aid})
    assert not (ctx.root / "ontology/core/classes/beam.md").exists()
    assert not (ctx.root / "ontology/core/index.md").exists()  # ProposeClass made it, so the revert removes it
    assert findings(ctx) == []


def test_revert_blocked_by_a_later_edit(ctx):
    aid = PromoteClass().submit(ctx, {"class_iri": ROD})
    later = AddConstraint().submit(ctx, {"class_iri": ROD, "constraint": CONSTRAINT})
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": aid})
    assert e.value.messages == [f"the later action {later} (AddConstraint) also edited ontology/core/classes/rod.md; "
                                "revert it first"]


def test_revert_twice_fails(ctx):
    aid = AddConstraint().submit(ctx, {"class_iri": ROD, "constraint": CONSTRAINT})
    rid = RevertAction().submit(ctx, {"action_id": aid})
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": aid})
    assert e.value.messages == [f"{aid} is already reverted by {rid}"]


def test_revert_of_adopt_bundle_is_refused(ctx):
    [adopt] = [r["action_id"] for r in log_rows(ctx) if r["action_type"] == "AdoptBundle"]
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": adopt})
    assert "AdoptBundle" in e.value.messages[0] and "cannot be reverted" in e.value.messages[0]


def test_revert_of_bind_table_is_refused(ctx):
    aid = bind(ctx, {"title": TITLE, "length_m": LENGTH})
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": aid})
    assert "BindTable" in e.value.messages[0]


def test_revert_of_an_unknown_action_fails(ctx):
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": "nope"})
    assert e.value.messages == ["no logged action has the id nope"]


def core_spec():
    return yaml.safe_load((CORE_SEEDS / "core.yaml").read_text())


@pytest.fixture
def fresh(bundle, tmp_path):
    c = make_ctx(bundle.root, tmp_path)
    AdoptBundle().submit(c, {})
    yield c
    c.lake.close()


def test_seed_ontology_and_rerun_logs_no_page_edits(fresh):
    SeedOntology().submit(fresh, {"spec": core_spec()})
    assert fm(fresh, "ontology/core/properties/mass.md")["unit"] == "unit:KiloGM"
    assert findings(fresh) == []
    SeedOntology().submit(fresh, {"spec": core_spec()})
    last = log_rows(fresh)[-1]
    assert last["action_type"] == "SeedOntology" and json.loads(last["page_edits"]) == []
    assert findings(fresh) == []


def test_seed_keeps_what_actions_added(fresh):
    SeedOntology().submit(fresh, {"spec": core_spec()})
    joint = CORE + "classes/revolute-joint"
    DeprecateClass().submit(fresh, {"class_iri": joint, "reason": "renamed", "deadline": "2027-01-31",
                                    "replacement": CORE + "classes/continuous-joint"})
    ProposeClass().submit(fresh, {"domain": "core", "slug": "beam", "title": "Beam", "description": "A beam."})
    ImplementInterface().submit(fresh, {"class_iri": CORE + "classes/continuous-joint",
                                        "interface_iri": CORE + "interfaces/observation", "column_map": {}})
    SeedOntology().submit(fresh, {"spec": core_spec()})
    assert json.loads(log_rows(fresh)[-1]["page_edits"]) == []
    assert fm(fresh, "ontology/core/classes/revolute-joint.md")["status"] == "deprecated"
    assert fm(fresh, "ontology/core/classes/continuous-joint.md")["implements"] == [
        CORE + "interfaces/joint", CORE + "interfaces/observation"]
    assert findings(fresh) == []


def pages(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.md"))}


def test_seed_cannot_change_an_active_class_iri(fresh):
    SeedOntology().submit(fresh, {"spec": core_spec()})
    before, rows = pages(fresh.root), len(log_rows(fresh))
    moved = {**core_spec(), "base_iri": "https://kdb.aegean.ai/core2/"}
    with pytest.raises(Failed) as e:
        SeedOntology().submit(fresh, {"spec": moved})
    assert f"{CORE}classes/revolute-joint is active; deprecate it and propose a replacement" in e.value.messages
    assert pages(fresh.root) == before and len(log_rows(fresh)) == rows
    assert findings(fresh) == []


def test_revert_of_a_seed_that_made_active_classes_is_refused(fresh):
    aid = SeedOntology().submit(fresh, {"spec": core_spec()})
    with pytest.raises(Failed) as e:
        RevertAction().submit(fresh, {"action_id": aid})
    assert f"{CORE}classes/revolute-joint is active; deprecate it and propose a replacement" in e.value.messages
    assert (fresh.root / "ontology/core/classes/revolute-joint.md").exists()


def test_revert_of_a_promotion_is_refused(ctx):
    aid = PromoteClass().submit(ctx, {"class_iri": ROD})
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": aid})
    assert e.value.messages == [ACTIVE]
    assert fm(ctx, "ontology/core/classes/rod.md")["status"] == "active"


def test_seed_ontology_with_a_malformed_spec_fails(fresh):
    with pytest.raises(Failed) as e:
        SeedOntology().submit(fresh, {"spec": {"terms": "no domain"}})
    assert e.value.messages[0].startswith("the seed spec is malformed")


def test_okf_seed_refuses_without_a_lake(bundle, capsys):
    assert seed_main([str(bundle.root), "--core"]) == 2
    assert "[lake]" in capsys.readouterr().err
    assert not (bundle.root / "ontology").exists()


def test_okf_seed_runs_as_the_seed_action(bundle, tmp_path):
    (tmp_path / "kdb.toml").write_text(f'[lake]\ncatalog = "{tmp_path / "meta.ducklake"}"\n'
                                       f'data_path = "{tmp_path / "data"}/"\nteam = "t"\n'
                                       'iri_base = "https://data.aegean.ai/vla/t/"\n')
    c = make_ctx(bundle.root, tmp_path)
    AdoptBundle().submit(c, {})
    c.lake.close()
    assert seed_main([str(bundle.root), "--core"]) == 0
    c = make_ctx(bundle.root, tmp_path)
    assert [r["action_type"] for r in log_rows(c)] == ["AdoptBundle", "SeedOntology"]
    assert findings(c) == []
    c.lake.close()


def test_log_heading_uses_the_action_time_in_utc(ctx):
    plan = PromoteClass().dry_run(ctx, {"class_iri": ROD})
    day = plan.now.astimezone(timezone.utc).date().isoformat()
    [log] = [e for e in plan.page_edits if e.path == "log.md"]
    assert f"## {day}" in log.after


class NoteParams(BaseModel):
    text: str


class NoteRow(ActionType[NoteParams]):
    """A toy action with a row edit, so the revert of row edits is covered."""

    name = "NoteRowForRevertTests"
    version = 1
    params = NoteParams

    def edits(self, ctx, p):
        iri = ctx.iri_base + "note/1"
        row = ctx.lake.con.execute("SELECT text FROM notes WHERE iri = ?", [iri]).fetchone()
        op, before = ("update", {"iri": iri, "text": row[0]}) if row else ("insert", None)
        return [], [RowEdit("notes", iri, op, before, {"iri": iri, "text": p.text})], []


@pytest.fixture
def note_row():
    """Register the toy action for one test only, so REGISTRY lists only real actions afterwards."""
    register(NoteRow)
    yield NoteRow
    REGISTRY.pop(NoteRow.name, None)


def test_revert_runs_row_edits_backwards(ctx, note_row):
    ctx.lake.con.execute("CREATE TABLE notes(iri VARCHAR, text VARCHAR)")
    first = NoteRow().submit(ctx, {"text": "one"})
    second = NoteRow().submit(ctx, {"text": "two"})
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": first})
    assert e.value.messages == [f"the later action {second} (NoteRowForRevertTests) also edited "
                                f"row {ctx.iri_base}note/1 of notes; revert it first"]
    RevertAction().submit(ctx, {"action_id": second})
    assert ctx.lake.con.execute("SELECT text FROM notes").fetchall() == [("one",)]
    RevertAction().submit(ctx, {"action_id": first})
    assert ctx.lake.con.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
