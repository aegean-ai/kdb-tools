"""action.unlogged: a page edited outside a logged action fails validation; AdoptBundle brings a bundle in."""

import stat

import pytest

from kdb_tools.actions import Context
from kdb_tools.actions.adopt import AdoptBundle
from kdb_tools.lake import Lake, LakeConfig, LocalStore
from kdb_tools.validate import main, validate

from pydantic import BaseModel

from kdb_tools.actions import ActionType, Conflict, Failed, PageEdit, register


class PageParams(BaseModel):
    path: str
    text: str


@register
class WritePage(ActionType[PageParams]):
    name = "WritePageForUnloggedTests"
    version = 1
    params = PageParams

    def edits(self, ctx, p):
        return [PageEdit(p.path, ctx.read(p.path), p.text)], [], []


@pytest.fixture
def lake_ctx(bundle, tmp_path):
    bundle.add("models/openvla.md", {"type": "Model", "title": "OpenVLA", "generated": {"by": "a", "at": "2026-11-02T00:00:00Z"}})
    cfg = LakeConfig(catalog=tmp_path / "meta.ducklake", data_path=str(tmp_path / "data") + "/", team="t",
                     iri_base="https://x.test/t/")
    lake = Lake.open(cfg)
    lake.ensure_system_tables()
    yield Context(root=bundle.root, lake=lake, store=LocalStore(tmp_path / "data"), actor="a", model=None,
                  team="t", iri_base=cfg.iri_base)
    lake.close()


def unlogged(ctx):
    return sorted(f.path for f in validate(ctx.root, profiles=[], lake=ctx.lake).findings if f.rule == "action.unlogged")


def test_unadopted_bundle_is_all_unlogged(lake_ctx):
    assert unlogged(lake_ctx) == ["index.md", "log.md", "models/openvla.md"]


def test_adopted_bundle_is_clean_and_files_untouched(lake_ctx):
    root = lake_ctx.root
    (root / "models/openvla.md").chmod(0o640)
    before = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino) for p in root.rglob("*.md")}
    AdoptBundle().submit(lake_ctx, {})
    assert unlogged(lake_ctx) == []
    assert {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino) for p in root.rglob("*.md")} == before
    assert stat.S_IMODE((root / "models/openvla.md").stat().st_mode) == 0o640


def test_hand_edit_whitespace_is_unlogged(lake_ctx):
    AdoptBundle().submit(lake_ctx, {})
    with open(lake_ctx.root / "models/openvla.md", "a") as f:
        f.write(" ")
    assert unlogged(lake_ctx) == ["models/openvla.md"]


def test_page_created_by_hand_is_unlogged(lake_ctx):
    AdoptBundle().submit(lake_ctx, {})
    (lake_ctx.root / "extra.md").write_text("# x\n")
    assert unlogged(lake_ctx) == ["extra.md"]


def test_page_deleted_by_hand_is_unlogged(lake_ctx):
    AdoptBundle().submit(lake_ctx, {})
    (lake_ctx.root / "models/openvla.md").unlink()
    assert unlogged(lake_ctx) == ["models/openvla.md"]


def test_later_action_supersedes_adoption_and_pages_filter(lake_ctx):
    AdoptBundle().submit(lake_ctx, {})
    WritePage().submit(lake_ctx, {"path": "n.md", "text": "one"})
    assert unlogged(lake_ctx) == []
    (lake_ctx.root / "n.md").write_text("changed")
    assert unlogged(lake_ctx) == ["n.md"]
    r = validate(lake_ctx.root, profiles=[], pages=["index.md"], lake=lake_ctx.lake)
    assert not [f for f in r.findings if f.rule == "action.unlogged"]


def test_invalid_utf8_page_cannot_be_adopted(lake_ctx):
    (lake_ctx.root / "bad.md").write_bytes(b"\xff\xfe")
    with pytest.raises(Failed):
        AdoptBundle().dry_run(lake_ctx, {})


def test_no_lake_library_call_adds_nothing(bundle):
    assert not [f for f in validate(bundle.root, profiles=[]).findings if f.rule.startswith("action.")]


def test_no_lake_is_one_warning(bundle, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kdb.toml").write_text("[agent]\n")  # found, but no [lake]
    main([str(bundle.root), "--json"])
    out = capsys.readouterr().out
    assert out.count('"action.no-lake"') == 1 and '"action.unlogged"' not in out


def test_cli_with_lake_config(lake_ctx, tmp_path, capsys):
    AdoptBundle().submit(lake_ctx, {})
    toml = tmp_path / "kdb.toml"
    toml.write_text(f'[lake]\ncatalog = "{tmp_path / "meta.ducklake"}"\ndata_path = "{tmp_path / "data"}/"\n'
                    'team = "t"\niri_base = "https://x.test/t/"\n')
    main([str(lake_ctx.root), "--lake-config", str(toml), "--json"])
    out = capsys.readouterr().out
    assert "action.no-lake" not in out and "action.unlogged" not in out


def test_unopenable_catalog_is_a_tool_failure(bundle, tmp_path):
    toml = tmp_path / "kdb.toml"
    toml.write_text(f'[lake]\ncatalog = "{tmp_path}/nodir/x/meta.ducklake"\ndata_path = "{tmp_path}/d/"\n'
                    'team = "t"\niri_base = "https://x.test/t/"\n')
    assert main([str(bundle.root), "--lake-config", str(toml)]) == 2


def test_second_adopt_is_refused(lake_ctx):
    AdoptBundle().submit(lake_ctx, {})
    with pytest.raises(Failed, match="already adopted"):
        AdoptBundle().submit(lake_ctx, {})


@register
class WriteBlind(ActionType[PageParams]):
    name = "WriteBlindForUnloggedTests"
    version = 1
    params = PageParams

    def edits(self, ctx, p):
        return [PageEdit(p.path, "something else" if ctx.read(p.path) is not None else None, p.text)], [], []


def test_wrong_before_is_a_conflict_and_changes_nothing(lake_ctx):
    path = lake_ctx.root / "h.md"
    path.write_text("hand written\n")
    n = lake_ctx.lake.con.execute("SELECT count(*) FROM kdb.action_log").fetchone()[0]
    for action in (WriteBlind(), WritePage()):
        if action.name == "WritePageForUnloggedTests":  # before None on an existing file
            action = _NoneBefore()
        with pytest.raises(Conflict, match="h.md"):
            action.submit(lake_ctx, {"path": "h.md", "text": "x"})
    assert path.read_text() == "hand written\n"
    assert lake_ctx.lake.con.execute("SELECT count(*) FROM kdb.action_log").fetchone()[0] == n


@register
class _NoneBefore(ActionType[PageParams]):
    name = "NoneBeforeForUnloggedTests"
    version = 1
    params = PageParams

    def edits(self, ctx, p):
        return [PageEdit(p.path, None, p.text)], [], []
