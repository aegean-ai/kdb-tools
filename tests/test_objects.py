"""Instance actions: media rows, objects in bound tables, and their values kept in their declared types."""

import hashlib
import json
from datetime import date, datetime, timezone

import pytest
from conftest import domain, highlight, klass, make_pdf, make_xlsx, prop, term

from kdb_tools.actions import Conflict, Failed
from kdb_tools.actions.adopt import AdoptBundle
from kdb_tools.actions.objects import DeleteObjects, RegisterMedia, UpsertObjects
from kdb_tools.actions.types import BindTable, RevertAction
from kdb_tools.lake import Lake, LakeConfig, LocalStore, object_key
from kdb_tools.media import MediaRef

CORE = "https://kdb.aegean.ai/core/"
RESULT = CORE + "classes/result"
PICK = CORE + "tasks/pick"
T1 = "https://data.aegean.ai/vla/team-1/"
T2 = "https://data.aegean.ai/vla/team-2/"
QUOTE = "OpenVLA reaches a success rate of 76.5 percent."
PROPS = {  # column: (property slug, datatype, required)
    "title": ("title", "string", True), "evidence": ("evidence", "media_ref_list", False),
    "score": ("score", "double", False), "episodes": ("episodes", "integer", False), "ok": ("ok", "boolean", False),
    "published": ("published", "date", False), "run_at": ("run-at", "timestamp", False),
    "tags": ("tags", "string_list", False), "task": ("task", "iri", False), "related": ("related", "iri_list", False),
    "meta": ("meta", "json", False), "aliases": ("aliases", "string_list", False),
}
BINDING = {"table": "results", "key": "iri", "title": "title",
           "columns": {c: CORE + "properties/" + slug for c, (slug, _, _) in PROPS.items()}}


def make_ctx(root, tmp_path, team="team-1"):
    from kdb_tools.actions import Context

    base = f"https://data.aegean.ai/vla/{team}/"
    cfg = LakeConfig(catalog=tmp_path / "meta.ducklake", data_path=str(tmp_path / "data") + "/", team=team, iri_base=base)
    lake = Lake.open(cfg)
    lake.ensure_system_tables()
    return Context(root=root, lake=lake, store=LocalStore(tmp_path / "data"), actor="a1", model=None, team=team,
                   iri_base=base)


def setup_kdb(bundle, tmp_path, team="team-1"):
    domain(bundle, "core", CORE)
    for slug, datatype, required in PROPS.values():
        prop(bundle, "core", slug, datatype, required=required)
    klass(bundle, "core", "result", table="results", columns={c: slug for c, (slug, _, _) in PROPS.items()})
    bundle.add(*term("Pick", domain="core", kind="tasks", iri=PICK))
    c = make_ctx(bundle.root, tmp_path, team)
    AdoptBundle().submit(c, {})
    BindTable().submit(c, {"class_iri": RESULT, "binding": BINDING})
    return c


@pytest.fixture
def ctx(bundle, tmp_path):
    c = setup_kdb(bundle, tmp_path)
    yield c
    c.lake.close()


@pytest.fixture
def pdfs(tmp_path):
    """A paper with the quote on page 1 and its highlighted copy, outside the bundle."""
    pdf = tmp_path / "src" / "kim2024.pdf"
    boxes = make_pdf(pdf, [[(72, 700, QUOTE), (72, 500, "Another sentence entirely.")], [(72, 700, "Appendix")]])
    marked = tmp_path / "src" / "kim2024-highlighted.pdf"
    highlight(pdf, marked, [(1, boxes[QUOTE])])
    return pdf, marked, boxes


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def register(ctx, path, annotates=None):
    params = {"path": str(path), "origin": "https://arxiv.org/abs/2406.09246"}
    if annotates:
        params["annotates"] = annotates
    return RegisterMedia().submit(ctx, params)


def log_count(ctx):
    return ctx.lake.con.execute("SELECT count(*) FROM kdb.action_log").fetchone()[0]


def media_rows(ctx):
    return ctx.lake.con.execute("SELECT sha256, media_type, bytes, object_key, name, origin, annotates, team, "
                                "registered_by FROM kdb.media ORDER BY sha256").fetchall()


def row(ctx, iri):
    cur = ctx.lake.con.execute("SELECT * FROM results WHERE iri = ?", [iri])
    names = [d[0] for d in cur.description]
    found = cur.fetchone()
    return None if found is None else dict(zip(names, found))


def result(iri=T1 + "results/openvla-libero", **extra):
    return {"iri": iri, "title": "OpenVLA on LIBERO", **extra}


# --- RegisterMedia ---

def test_register_media_inserts_the_row_and_stores_the_bytes(ctx, pdfs):
    pdf, _, _ = pdfs
    register(ctx, pdf)
    h = sha(pdf)
    assert media_rows(ctx) == [(h, "application/pdf", pdf.stat().st_size, object_key(h), "kim2024.pdf",
                                "https://arxiv.org/abs/2406.09246", None, "team-1", "a1")]
    assert ctx.store.get(object_key(h)) == pdf.read_bytes()


def test_registering_the_same_pdf_twice_leaves_one_row_and_one_log_row(ctx, pdfs):
    pdf, _, _ = pdfs
    n = log_count(ctx)
    assert register(ctx, pdf) is not None
    assert register(ctx, pdf) is None  # a no-op logs nothing, so it has no action id
    assert len(media_rows(ctx)) == 1
    assert log_count(ctx) == n + 1


def test_bytes_are_stored_only_after_the_commit_and_a_rerun_repairs_a_failed_put(ctx, pdfs, monkeypatch):
    pdf, _, _ = pdfs
    seen = []
    real_put = LocalStore.put

    def failing_put(self, sha256, data):
        seen.append(ctx.lake.con.execute("SELECT count(*) FROM kdb.media WHERE sha256 = ?", [sha256]).fetchone()[0])
        raise OSError("the store is down")

    monkeypatch.setattr(LocalStore, "put", failing_put)
    with pytest.raises(OSError):
        register(ctx, pdf)
    assert seen == [1]  # the row was committed before the put was tried
    assert not ctx.store.exists(object_key(sha(pdf)))
    n = log_count(ctx)
    monkeypatch.setattr(LocalStore, "put", real_put)
    register(ctx, pdf)
    assert ctx.store.get(object_key(sha(pdf))) == pdf.read_bytes()
    assert log_count(ctx) == n and len(media_rows(ctx)) == 1


def test_a_failed_register_stores_nothing(ctx, pdfs):
    pdf, _, _ = pdfs
    with pytest.raises(Failed) as e:
        register(ctx, pdf, annotates="0" * 64)
    assert e.value.messages == [f"annotates {'0' * 64} is not a registered sha256; register the original first"]
    assert media_rows(ctx) == [] and not ctx.store.exists(object_key(sha(pdf)))


def test_an_annotated_copy_is_a_media_row_that_annotates_the_original(ctx, pdfs):
    pdf, marked, _ = pdfs
    register(ctx, pdf)
    register(ctx, marked, annotates=sha(pdf))
    assert ctx.lake.con.execute("SELECT annotates FROM kdb.media WHERE sha256 = ?", [sha(marked)]).fetchone()[0] == sha(pdf)


def test_register_a_missing_file_fails(ctx, tmp_path):
    with pytest.raises(Failed) as e:
        register(ctx, tmp_path / "nope.pdf")
    assert e.value.messages == [f"{tmp_path / 'nope.pdf'} is not a file"]


# --- UpsertObjects ---

def test_upsert_inserts_a_row_with_type_and_team(ctx, pdfs):
    pdf, _, boxes = pdfs
    register(ctx, pdf)
    ref = {"sha256": sha(pdf), "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE}
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(evidence=[ref], task=PICK, related=[PICK])]})
    got = row(ctx, T1 + "results/openvla-libero")
    assert got["type"] == RESULT and got["team"] == "team-1" and got["title"] == "OpenVLA on LIBERO"
    assert json.loads(got["evidence"]) == [ref]


def test_upsert_logs_before_and_after(ctx):
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(score=0.5)]})
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(score=0.75)]})
    edits = json.loads(ctx.lake.con.execute('SELECT row_edits FROM kdb.action_log ORDER BY "at" DESC LIMIT 1').fetchone()[0])
    assert [(e["table"], e["iri"], e["op"]) for e in edits] == [("results", T1 + "results/openvla-libero", "update")]
    assert edits[0]["before"]["score"] == 0.5 and edits[0]["after"]["score"] == 0.75


def test_upsert_of_unchanged_rows_is_a_no_op_that_logs_nothing(ctx):
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(score=0.5)]})
    n = log_count(ctx)
    plan = UpsertObjects().dry_run(ctx, {"class_iri": RESULT, "rows": [result(score=0.5)]})
    assert plan.row_edits == []
    assert UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(score=0.5)]}) is None
    assert UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(score=0.5)]}, plan) is None
    assert log_count(ctx) == n


def test_a_stale_empty_plan_conflicts(ctx):
    raw = {"class_iri": RESULT, "rows": [result(score=0.5)]}
    UpsertObjects().submit(ctx, raw)
    plan = UpsertObjects().dry_run(ctx, raw)
    assert plan.row_edits == []
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(score=0.75)]})  # someone else changes it
    n = log_count(ctx)
    with pytest.raises(Conflict):
        UpsertObjects().submit(ctx, raw, plan)
    assert row(ctx, T1 + "results/openvla-libero")["score"] == 0.75 and log_count(ctx) == n


def test_an_update_keeps_the_columns_it_leaves_out(ctx, pdfs):
    pdf, _, boxes = pdfs
    register(ctx, pdf)
    ref = {"sha256": sha(pdf), "page": 1, "bbox": boxes[QUOTE], "quote": QUOTE}
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(evidence=[ref])]})
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [{"iri": T1 + "results/openvla-libero",
                                                                 "aliases": ["openvla-7b"]}]})
    got = row(ctx, T1 + "results/openvla-libero")
    assert got["aliases"] == ["openvla-7b"] and got["title"] == "OpenVLA on LIBERO"
    assert json.loads(got["evidence"]) == [ref]


def test_an_explicit_null_clears_an_optional_column(ctx):
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(score=0.5)]})
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [{"iri": T1 + "results/openvla-libero", "score": None}]})
    got = row(ctx, T1 + "results/openvla-libero")
    assert got["score"] is None and got["title"] == "OpenVLA on LIBERO"


def test_an_update_that_nulls_a_required_property_fails(ctx):
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result()]})
    with pytest.raises(Failed) as e:
        UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [{"iri": T1 + "results/openvla-libero", "title": None}]})
    assert e.value.messages == [f"row {T1}results/openvla-libero: required property {CORE}properties/title "
                                "(column title) has no value"]
    assert row(ctx, T1 + "results/openvla-libero")["title"] == "OpenVLA on LIBERO"


def test_foreign_team_iri_refused(bundle, tmp_path):
    c = setup_kdb(bundle, tmp_path, team="team-2")
    try:
        with pytest.raises(Failed) as e:
            UpsertObjects().submit(c, {"class_iri": RESULT, "rows": [result(iri=T1 + "results/openvla-libero")]})
        message = "\n".join(e.value.messages)
        assert T1 + "results/openvla-libero" in message and T2 in message
        assert row(c, T1 + "results/openvla-libero") is None
    finally:
        c.lake.close()


def test_a_required_property_missing_fails_with_its_iri(ctx):
    with pytest.raises(Failed) as e:
        UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [{"iri": T1 + "results/x", "score": 1.0}]})
    assert any(CORE + "properties/title" in m for m in e.value.messages)


def test_an_unregistered_media_ref_is_refused(ctx):
    ref = {"sha256": "a" * 64, "page": 1, "quote": QUOTE}
    with pytest.raises(Failed) as e:
        UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(evidence=[ref])]})
    assert e.value.messages == [f"row {T1}results/openvla-libero: evidence cites {'a' * 64}, which is not registered "
                                "in kdb.media; register the file with RegisterMedia first"]


def test_an_iri_into_the_ontology_must_name_a_term(ctx):
    with pytest.raises(Failed) as e:
        UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(related=[PICK, CORE + "tasks/nope"])]})
    assert e.value.messages == [f"row {T1}results/openvla-libero: related names {CORE}tasks/nope, which is not a Term "
                                "in this bundle"]


@pytest.mark.parametrize("column, value", [
    ("episodes", "many"), ("episodes", True), ("score", "high"), ("ok", "yes"), ("published", "last week"),
    ("run_at", "2026-10-25T12:00:00"), ("tags", "pick"), ("tags", [1]), ("task", 3), ("evidence", [{"sha256": "x"}]),
    ("nope", 1), ("type", CORE + "classes/other"), ("team", "team-9"),
    ("episodes", 2 ** 63), ("episodes", -2 ** 63 - 1), ("score", float("nan")), ("score", float("inf")),
    ("score", 10 ** 400), ("meta", {"x": float("nan")}), ("meta", [float("-inf")]),
    ("evidence", [{"sha256": "a" * 64, "bbox": [0, 0, float("inf"), 1]}]),
    ("evidence", [{"sha256": "a" * 64, "t": [float("nan")]}]),
])
def test_a_wrong_value_fails_and_writes_nothing(ctx, column, value):
    with pytest.raises(Failed) as e:
        UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(**{column: value})]})
    assert all(m.startswith(f"row {T1}results/openvla-libero: ") for m in e.value.messages), e.value.messages
    assert row(ctx, T1 + "results/openvla-libero") is None


@pytest.mark.parametrize("raw", [
    {"class_iri": RESULT, "rows": "not a list"},
    {"class_iri": RESULT, "rows": [["not", "a", "dict"]]},
    {"class_iri": RESULT, "rows": [{"title": "no iri"}]},
    {"class_iri": CORE + "classes/none", "rows": []},
    {"class_iri": RESULT, "rows": [result(), result()]},
])
def test_malformed_params_fail_with_messages(ctx, raw):
    with pytest.raises(Failed) as e:
        UpsertObjects().submit(ctx, raw)
    assert e.value.messages


def test_values_are_stored_in_their_declared_types(ctx):
    at = datetime(2026, 10, 25, 12, 30, 1, 250000, tzinfo=timezone.utc)
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(
        score=1, episodes=970000, ok=True, published="2024-06-13", run_at="2026-10-25T12:30:01.25Z",
        tags=["libero", "sim"], meta={"seed": 3})]})
    types = dict(ctx.lake.con.execute(
        "SELECT 'score', typeof(score) FROM results UNION ALL SELECT 'run_at', typeof(run_at) FROM results "
        "UNION ALL SELECT 'published', typeof(published) FROM results UNION ALL SELECT 'tags', typeof(tags) FROM results"
    ).fetchall())
    assert types == {"score": "DOUBLE", "run_at": "TIMESTAMP WITH TIME ZONE", "published": "DATE", "tags": "VARCHAR[]"}
    got = row(ctx, T1 + "results/openvla-libero")
    assert got["score"] == 1.0 and got["published"] == date(2024, 6, 13) and got["run_at"] == at
    assert got["tags"] == ["libero", "sim"] and json.loads(got["meta"]) == {"seed": 3}


def test_revert_of_an_update_restores_list_timestamp_date_and_json_values(ctx):
    first = dict(tags=["libero", "sim"], run_at="2026-10-25T12:30:00Z", published="2024-06-13", meta={"seed": 3})
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(**first)]})
    before = row(ctx, T1 + "results/openvla-libero")
    aid = UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(
        tags=["real"], run_at="2026-10-26T08:00:00+02:00", published="2024-07-01", meta={"seed": 4})]})
    assert row(ctx, T1 + "results/openvla-libero")["tags"] == ["real"]
    RevertAction().submit(ctx, {"action_id": aid})
    after = row(ctx, T1 + "results/openvla-libero")
    assert after == before
    assert isinstance(after["run_at"], datetime) and after["tags"] == ["libero", "sim"]


def test_logged_row_values_decode_to_their_python_types(ctx):
    from kdb_tools.actions.base import load_row_edits

    aid = UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(
        run_at="2026-10-25T12:30:00Z", published="2024-06-13", tags=["a"], score=0.5, episodes=3, ok=False)]})
    text = ctx.lake.con.execute("SELECT row_edits FROM kdb.action_log WHERE action_id = ?", [aid]).fetchone()[0]
    after = load_row_edits(text)[0]["after"]
    assert after["run_at"] == datetime(2026, 10, 25, 12, 30, tzinfo=timezone.utc)
    assert after["published"] == date(2024, 6, 13)
    assert (after["tags"], after["score"], after["episodes"], after["ok"]) == (["a"], 0.5, 3, False)


# --- DeleteObjects ---

def test_delete_objects_removes_rows_and_a_revert_puts_them_back_typed(ctx):
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(run_at="2026-10-25T12:30:00Z", tags=["a"])]})
    before = row(ctx, T1 + "results/openvla-libero")
    aid = DeleteObjects().submit(ctx, {"class_iri": RESULT, "iris": [T1 + "results/openvla-libero"],
                                       "reason": "a duplicate of another result"})
    assert row(ctx, T1 + "results/openvla-libero") is None
    RevertAction().submit(ctx, {"action_id": aid})
    assert row(ctx, T1 + "results/openvla-libero") == before


def test_delete_needs_a_reason_and_an_existing_row_of_this_team(ctx):
    with pytest.raises(Failed) as e:
        DeleteObjects().submit(ctx, {"class_iri": RESULT, "iris": [T1 + "results/none", T2 + "results/x"], "reason": " "})
    assert e.value.messages == [
        "give the reason for the deletion",
        f"{T1}results/none is not a row of results",
        f"{T2}results/x is not under this team's iri_base {T1} (team team-1)",
    ]


# --- MediaRef ---

def test_media_ref_checks_its_fields_and_renders_a_fragment():
    ref = MediaRef(sha256="b" * 64, page=3, bbox=[120, 40, 520, 180], quote="x")
    assert ref.fragment() == "#page=3&xywh=120,40,400,140"
    assert MediaRef(sha256="b" * 64, t=[41, 48.5]).fragment() == "#t=41,48.5"
    assert MediaRef(sha256="b" * 64, sheet="DH", cell="B4").fragment() == "#sheet=DH&cell=B4"
    assert MediaRef(sha256="b" * 64, xywh=[1, 2, 3, 4]).fragment() == "#xywh=1,2,3,4"
    for bad in ({"sha256": "B" * 64}, {"sha256": "b" * 63}, {"sha256": "b" * 64, "bbox": [1, 2, 3]},
                {"sha256": "b" * 64, "colour": "red"}, {"sha256": "b" * 64, "t": [1, 2, 3]},
                {"sha256": "b" * 64, "bbox": [0, 0, float("nan"), 1]}):
        with pytest.raises(ValueError):
            MediaRef.model_validate(bad)


# --- evidence that okf-ground could not check is refused ---

@pytest.fixture
def files(ctx, pdfs, tmp_path):
    """A registered PDF, spreadsheet and text file: {kind: sha256}."""
    pdf, _, _ = pdfs
    book = tmp_path / "src" / "results.xlsx"
    make_xlsx(book, {"LIBERO": [["model", "success"], ["OpenVLA", 76.5]]})
    text = tmp_path / "src" / "notes.txt"
    text.write_text("OpenVLA reaches a success rate of 76.5 percent.\n")
    for path in (pdf, book, text):
        register(ctx, path)
    return {"pdf": sha(pdf), "sheet": sha(book), "text": sha(text)}


@pytest.mark.parametrize("kind, ref, why", [
    ("pdf", {"page": 1, "quote": QUOTE}, "a quote or value in a PDF needs a page and a bbox"),
    ("pdf", {"bbox": [72, 697, 300, 710], "value": "76.5"}, "a quote or value in a PDF needs a page and a bbox"),
    ("pdf", {"cell": "B2", "value": "76.5"}, "a cell needs a spreadsheet, and this file is a PDF"),
    ("sheet", {"sheet": "LIBERO", "cell": "B2"}, "a spreadsheet citation needs both a cell and a value"),
    ("sheet", {"sheet": "LIBERO", "value": "76.5"}, "a spreadsheet citation needs both a cell and a value"),
    ("sheet", {"sheet": "LIBERO", "cell": "B2", "value": "76.5", "quote": "x"},
     "a spreadsheet citation gives a cell and a value, not a quote"),
    ("sheet", {"sheet": "LIBERO", "cell": "b2", "value": "76.5"}, "cell 'b2' is not an A1 cell reference such as B4"),
    ("text", {"quote": QUOTE}, "a quote, value or cell needs a PDF or a spreadsheet, and this file is text/plain"),
])
def test_evidence_that_cannot_be_grounded_is_refused(ctx, files, kind, ref, why):
    with pytest.raises(Failed) as e:
        UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(evidence=[{"sha256": files[kind], **ref}])]})
    assert e.value.messages == [f"row {T1}results/openvla-libero: evidence[0] cannot be grounded: {why}"]


def test_a_reference_that_cites_no_passage_is_accepted(ctx, files):
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(evidence=[{"sha256": files["text"]}])]})
    assert row(ctx, T1 + "results/openvla-libero") is not None


@pytest.mark.parametrize("bad", [{"bbox": [72, 697, float("inf"), 710]}, {"t": [float("nan")]}])
def test_a_non_finite_media_ref_number_is_a_failed_message(ctx, files, bad):
    ref = {"sha256": files["pdf"], "page": 1, "bbox": [72, 697, 300, 710], "quote": QUOTE, **bad}
    with pytest.raises(Failed) as e:
        UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(evidence=[ref])]})
    assert len(e.value.messages) == 1 and "which is not a list of MediaRefs" in e.value.messages[0]


def test_register_media_turns_an_unreadable_or_unusable_path_into_failed(ctx, tmp_path):
    locked = tmp_path / "locked.pdf"
    locked.write_bytes(b"%PDF-1.4 locked")
    locked.chmod(0)
    try:
        with pytest.raises(Failed) as e:
            RegisterMedia().submit(ctx, {"path": str(locked), "origin": "x"})
        assert e.value.messages[0].startswith(f"{locked} could not be read: ")
    finally:
        locked.chmod(0o600)
    with pytest.raises(Failed) as e:
        RegisterMedia().submit(ctx, {"path": "bad\x00name.pdf", "origin": "x"})
    assert e.value.messages == ["'bad\\x00name.pdf' is not a usable path"]


def test_a_revert_refuses_a_row_changed_outside_any_action(ctx):
    aid = UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(title="OpenVLA")]})
    ctx.lake.con.execute("UPDATE results SET title = 'edited in SQL' WHERE iri = ?", [T1 + "results/openvla-libero"])
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": aid})
    assert e.value.messages == [f"row {T1}results/openvla-libero of results changed since {aid} wrote it"]
    assert row(ctx, T1 + "results/openvla-libero")["title"] == "edited in SQL"


def test_a_revert_of_a_delete_refuses_a_row_put_back_outside_any_action(ctx):
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [result(title="OpenVLA")]})
    aid = DeleteObjects().submit(ctx, {"class_iri": RESULT, "iris": [T1 + "results/openvla-libero"], "reason": "dup"})
    ctx.lake.con.execute("INSERT INTO results (iri, title) VALUES (?, 'back')", [T1 + "results/openvla-libero"])
    with pytest.raises(Failed) as e:
        RevertAction().submit(ctx, {"action_id": aid})
    assert e.value.messages == [f"row {T1}results/openvla-libero of results changed since {aid} wrote it"]
