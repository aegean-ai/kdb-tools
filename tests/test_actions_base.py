"""The action machinery: dry run, preconditions, the optimistic check, one transaction, the log row."""

import hashlib
import json

import pytest
from pydantic import BaseModel

from kdb_tools.actions import REGISTRY, ActionType, Conflict, Context, Failed, PageEdit, RowEdit, register
from kdb_tools.lake import Lake, LakeConfig, LocalStore

IRI = "https://data.aegean.ai/vla/team-1/note/"


class NoteParams(BaseModel):
    path: str
    text: str


@register
class WriteNote(ActionType[NoteParams]):
    """A toy action: writes one page and inserts or updates one row of the notes table."""

    name = "WriteNote"
    version = 1
    params = NoteParams

    def preconditions(self, ctx, p):
        return [] if p.text else ["text is empty"]

    def edits(self, ctx, p):
        before = ctx.read(p.path)
        iri = IRI + p.path
        row = ctx.lake.con.execute("SELECT iri, text FROM notes WHERE iri = ?", [iri]).fetchone()
        old = {"iri": row[0], "text": row[1]} if row else None
        new = {"iri": iri, "text": p.text}
        op = "update" if row else "insert"
        return [PageEdit(p.path, before, f"# Note\n\n{p.text}\n")], [RowEdit("notes", iri, op, old, new)], ["src/a.pdf"]


@pytest.fixture
def ctx(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    cfg = LakeConfig(catalog=tmp_path / "meta.ducklake", data_path=str(tmp_path / "data") + "/", team="team-1",
                     iri_base="https://data.aegean.ai/vla/team-1/")
    lake = Lake.open(cfg)
    lake.ensure_system_tables()
    lake.con.execute("CREATE TABLE notes(iri VARCHAR, text VARCHAR)")
    yield Context(root=root, lake=lake, store=LocalStore(tmp_path / "data"), actor="a1", model=None, team="team-1",
                  iri_base=cfg.iri_base)
    lake.close()


def _log(ctx):
    cur = ctx.lake.con.execute("SELECT * FROM kdb.action_log ORDER BY \"at\"")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _notes(ctx):
    return ctx.lake.con.execute("SELECT iri, text FROM notes ORDER BY iri").fetchall()


def test_registered():
    assert REGISTRY["WriteNote"] is WriteNote


def test_dry_run_changes_nothing(ctx):
    s0 = ctx.lake.snapshot()
    plan = WriteNote().dry_run(ctx, {"path": "n.md", "text": "one"})
    assert not (ctx.root / "n.md").exists()
    assert ctx.lake.snapshot() == s0
    assert _log(ctx) == [] and _notes(ctx) == []
    assert plan.page_edits == [PageEdit("n.md", None, "# Note\n\none\n")]
    assert plan.row_edits[0].op == "insert"
    assert plan.snapshot == s0 and plan.page_hashes == {"n.md": None}
    assert plan.evidence == ["src/a.pdf"]
    stamp, digest = plan.action_id.rsplit("-", 1)
    assert len(digest) == 8 and int(digest, 16) >= 0
    assert len(stamp.split(".")[1].rstrip("Z")) == 6  # microseconds


def test_submit_logs_hashes(ctx):
    aid = WriteNote().submit(ctx, {"path": "n.md", "text": "one"})
    data = (ctx.root / "n.md").read_bytes()
    assert data == b"# Note\n\none\n"
    [row] = _log(ctx)
    assert row["action_id"] == aid
    assert (row["action_type"], row["action_version"], row["actor"], row["model"], row["team"]) == \
        ("WriteNote", 1, "a1", None, "team-1")
    assert json.loads(row["params"]) == {"path": "n.md", "text": "one"}
    assert aid.endswith("-" + hashlib.sha256(row["params"].encode()).hexdigest()[:8])
    assert json.loads(row["page_edits"]) == [{"path": "n.md", "before_sha256": None,
                                              "after_sha256": hashlib.sha256(data).hexdigest(), "before_text": None}]
    assert json.loads(row["row_edits"]) == [{"table": "notes", "iri": IRI + "n.md", "op": "insert", "before": None,
                                             "after": {"iri": IRI + "n.md", "text": "one"}}]
    assert json.loads(row["evidence"]) == ["src/a.pdf"]
    assert row["reverts"] is None and row["reverted_by"] is None
    assert _notes(ctx) == [(IRI + "n.md", "one")]


def test_second_edit_logs_before_text_and_updates_row(ctx):
    WriteNote().submit(ctx, {"path": "d/n.md", "text": "one"})
    WriteNote().submit(ctx, {"path": "d/n.md", "text": "two"})
    edit = json.loads(_log(ctx)[1]["page_edits"])[0]
    assert edit["before_text"] == "# Note\n\none\n"
    assert edit["before_sha256"] == hashlib.sha256(b"# Note\n\none\n").hexdigest()
    assert _notes(ctx) == [(IRI + "d/n.md", "two")]


def test_failed_precondition_message(ctx):
    with pytest.raises(Failed) as e:
        WriteNote().submit(ctx, {"path": "n.md", "text": ""})
    assert e.value.messages == ["text is empty"]
    assert not (ctx.root / "n.md").exists() and _log(ctx) == []


def test_invalid_params_fail(ctx):
    with pytest.raises(Failed) as e:
        WriteNote().dry_run(ctx, {"path": "n.md"})
    assert any("text" in m for m in e.value.messages)


@pytest.mark.parametrize("path", ["/etc/n.md", "../n.md", "a/../../n.md", "a\\n.md", ""])
def test_paths_outside_the_bundle_fail(ctx, path):
    with pytest.raises(Failed) as e:
        WriteNote().submit(ctx, {"path": path, "text": "one"})
    assert "bundle" in str(e.value)
    assert _log(ctx) == [] and _notes(ctx) == []


def test_second_submit_conflicts_and_changes_nothing(ctx):
    a = WriteNote().dry_run(ctx, {"path": "n.md", "text": "one"})
    b = WriteNote().dry_run(ctx, {"path": "n.md", "text": "two"})
    WriteNote().submit(ctx, {"path": "n.md", "text": "one"}, a)
    s1 = ctx.lake.snapshot()
    with pytest.raises(Conflict) as e:
        WriteNote().submit(ctx, {"path": "n.md", "text": "two"}, b)
    assert "n.md" in str(e.value)
    assert "snapshot" in str(e.value)
    assert (ctx.root / "n.md").read_text().endswith("one\n")
    assert ctx.lake.snapshot() == s1 and len(_log(ctx)) == 1 and _notes(ctx) == [(IRI + "n.md", "one")]


def test_hand_edit_after_dry_run_conflicts(ctx):
    WriteNote().submit(ctx, {"path": "n.md", "text": "one"})
    plan = WriteNote().dry_run(ctx, {"path": "n.md", "text": "two"})
    (ctx.root / "n.md").write_text("# Note\n\nhand\n")
    with pytest.raises(Conflict) as e:
        WriteNote().submit(ctx, {"path": "n.md", "text": "two"}, plan)
    assert "n.md" in str(e.value) and "snapshot" not in str(e.value)
    assert (ctx.root / "n.md").read_text() == "# Note\n\nhand\n"


def test_plan_for_other_params_is_refused(ctx):
    plan = WriteNote().dry_run(ctx, {"path": "n.md", "text": "one"})
    with pytest.raises(Failed):
        WriteNote().submit(ctx, {"path": "n.md", "text": "two"}, plan)
    assert not (ctx.root / "n.md").exists()


def test_submit_with_plan_logs_its_id(ctx):
    plan = WriteNote().dry_run(ctx, {"path": "n.md", "text": "one"})
    assert WriteNote().submit(ctx, {"path": "n.md", "text": "one"}, plan) == plan.action_id


class _FailingCommit:
    """Stands in for the DuckDB connection and raises on COMMIT."""

    def __init__(self, con):
        self.con = con

    def execute(self, sql, *args):
        if sql.strip().upper() == "COMMIT":
            raise RuntimeError("commit failed")
        return self.con.execute(sql, *args)


def test_failure_after_write_restores_pages(ctx, monkeypatch):
    WriteNote().submit(ctx, {"path": "n.md", "text": "one"})
    s1 = ctx.lake.snapshot()
    monkeypatch.setattr(ctx.lake, "con", _FailingCommit(ctx.lake.con))
    with pytest.raises(RuntimeError, match="commit failed"):
        WriteNote().submit(ctx, {"path": "n.md", "text": "two"})
    monkeypatch.undo()
    assert (ctx.root / "n.md").read_bytes() == b"# Note\n\none\n"
    assert ctx.lake.snapshot() == s1 and len(_log(ctx)) == 1 and _notes(ctx) == [(IRI + "n.md", "one")]
    assert [p.name for p in ctx.root.iterdir()] == ["n.md"]  # no temporary files left


def test_failure_after_write_removes_new_pages(ctx, monkeypatch):
    monkeypatch.setattr(ctx.lake, "con", _FailingCommit(ctx.lake.con))
    with pytest.raises(RuntimeError):
        WriteNote().submit(ctx, {"path": "n.md", "text": "one"})
    monkeypatch.undo()
    assert list(ctx.root.iterdir()) == [] and _log(ctx) == [] and _notes(ctx) == []


class DeleteNote(ActionType[NoteParams]):
    name = "DeleteNote"
    version = 1
    params = NoteParams

    def preconditions(self, ctx, p):
        return []

    def edits(self, ctx, p):
        iri = IRI + p.path
        return [PageEdit(p.path, ctx.read(p.path), None)], [RowEdit("notes", iri, "delete", {"iri": iri, "text": p.text}, None)], []


def test_delete_page_and_row(ctx):
    WriteNote().submit(ctx, {"path": "n.md", "text": "one"})
    DeleteNote().submit(ctx, {"path": "n.md", "text": "one"})
    assert not (ctx.root / "n.md").exists() and _notes(ctx) == []
    edit = json.loads(_log(ctx)[1]["page_edits"])[0]
    assert edit["after_sha256"] is None and edit["before_text"] == "# Note\n\none\n"


def test_update_of_missing_row_fails_and_rolls_back(ctx):
    class Bad(DeleteNote):
        name = "Bad"

        def edits(self, ctx, p):
            return [PageEdit(p.path, None, "x\n")], [RowEdit("notes", IRI + "gone", "update", {}, {"text": "x"})], []

    with pytest.raises(Failed) as e:
        Bad().submit(ctx, {"path": "n.md", "text": "one"})
    assert "gone" in str(e.value)
    assert not (ctx.root / "n.md").exists() and _log(ctx) == []


def test_symlink_out_of_the_bundle_fails(ctx, tmp_path):
    (tmp_path / "outside").mkdir()
    (ctx.root / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(Failed) as e:
        WriteNote().submit(ctx, {"path": "link/n.md", "text": "one"})
    assert "outside the bundle" in str(e.value)
    assert list((tmp_path / "outside").iterdir()) == []


def _mode(path):
    return path.stat().st_mode & 0o777


def test_written_pages_get_normal_modes(ctx, monkeypatch):
    import os
    old = os.umask(0o022)
    try:
        WriteNote().submit(ctx, {"path": "n.md", "text": "one"})
        assert _mode(ctx.root / "n.md") == 0o644  # 0o666 less the umask, as a plain write would give
        (ctx.root / "n.md").chmod(0o640)
        WriteNote().submit(ctx, {"path": "n.md", "text": "two"})
        assert _mode(ctx.root / "n.md") == 0o640  # an existing page keeps its mode
        monkeypatch.setattr(ctx.lake, "con", _FailingCommit(ctx.lake.con))
        with pytest.raises(RuntimeError):
            WriteNote().submit(ctx, {"path": "n.md", "text": "three"})
        monkeypatch.undo()
        assert _mode(ctx.root / "n.md") == 0o640  # and keeps it through a restore
        with pytest.raises(RuntimeError):
            monkeypatch.setattr(ctx.lake, "con", _FailingCommit(ctx.lake.con))
            DeleteNote().submit(ctx, {"path": "n.md", "text": "two"})
        monkeypatch.undo()
        assert (ctx.root / "n.md").read_text().endswith("two\n") and _mode(ctx.root / "n.md") == 0o640
    finally:
        os.umask(old)


class StampParams(BaseModel):
    path: str


class StampNote(ActionType[StampParams]):
    """A toy action whose page carries the time, read only from ctx.now."""

    name = "StampNote"
    version = 1
    params = StampParams

    def edits(self, ctx, p):
        return [PageEdit(p.path, ctx.read(p.path), f"generated: {ctx.now.isoformat()}\n")], [], []


def test_plan_carries_the_time(ctx):
    plan = StampNote().dry_run(ctx, {"path": "s.md"})
    assert plan.now.tzinfo is not None and plan.now.utcoffset().total_seconds() == 0
    StampNote().submit(ctx, {"path": "s.md"}, plan)
    assert (ctx.root / "s.md").read_text() == f"generated: {plan.now.isoformat()}\n"


def test_submit_without_plan_uses_a_fresh_time(ctx):
    plan = StampNote().dry_run(ctx, {"path": "s.md"})
    StampNote().submit(ctx, {"path": "s.md"})
    assert (ctx.root / "s.md").read_text() != f"generated: {plan.now.isoformat()}\n"


def test_a_page_that_is_not_utf8_fails_naming_it(ctx):
    (ctx.root / "models").mkdir(exist_ok=True)
    (ctx.root / "models" / "latin1.md").write_bytes("---\ntitle: Caf\xe9\n---\n".encode("latin-1"))
    with pytest.raises(Failed) as e:
        ctx.read("models/latin1.md")
    assert e.value.messages == ["models/latin1.md is not UTF-8 text; save it as UTF-8"]
