"""okf-export and okf-restore: a lakehouse leaves as a folder of hashed files and comes back identical."""

import json

import pytest
from test_objects import RESULT, T1, pdfs, register, setup_kdb, sha  # noqa: F401  the paper fixture

from kdb_tools.actions.objects import UpsertObjects
from kdb_tools.export import ExportError, RestoreError, export, main, restore, restore_main
from kdb_tools.lake import Lake, LakeConfig, store_for

IRI = T1 + "results/openvla-libero"


@pytest.fixture
def ctx(bundle, tmp_path):
    (tmp_path / "lake").mkdir()
    c = setup_kdb(bundle, tmp_path / "lake")
    yield c
    c.lake.close()


def cfg_of(tmp_path):
    return LakeConfig(catalog=tmp_path / "lake" / "meta.ducklake", data_path=str(tmp_path / "lake" / "data") + "/",
                      team="team-1", iri_base="https://data.aegean.ai/vla/team-1/")


def filled(ctx, pdfs):
    """A lake with two result rows, a registered paper, and the rows flushed into parquet data files."""
    pdf, _, _ = pdfs
    register(ctx, pdf)
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [{"iri": IRI, "title": "OpenVLA on LIBERO", "score": 0.5},
                                                                {"iri": IRI + "-2", "title": "Octo", "score": 0.25}]})
    ctx.lake.con.execute("CALL ducklake_flush_inlined_data('lake')")
    return sha(pdf)


def read(cfg, query):
    lake = Lake.open(cfg)
    try:
        return lake.con.execute(query).fetchall()
    finally:
        lake.close()


def exported(ctx, pdfs, tmp_path):
    h = filled(ctx, pdfs)
    ctx.lake.close()
    return h, export(cfg_of(tmp_path), ctx.root, tmp_path / "drive")


def test_export_writes_catalog_data_media_and_a_manifest(ctx, pdfs, tmp_path):
    h, dest = exported(ctx, pdfs, tmp_path)
    manifest = json.loads((dest / "manifest.json").read_text())
    assert dest.name == f"team-1-{manifest['snapshot']}" and manifest["team"] == "team-1"
    names = set(manifest["files"])
    assert "catalog/meta.ducklake" in names and f"media/sha256/{h}" in names
    assert any(n.startswith("data/main/results/") and n.endswith(".parquet") for n in names)
    assert all(len(v) == 64 for v in manifest["files"].values())
    assert not list(dest.rglob("*.md"))  # the bundle is not exported


def test_restore_gives_the_same_rows_at_the_same_snapshot(ctx, pdfs, tmp_path):
    h, dest = exported(ctx, pdfs, tmp_path)
    want = read(cfg_of(tmp_path), "SELECT iri, title, score FROM results ORDER BY iri")
    snapshot = json.loads((dest / "manifest.json").read_text())["snapshot"]
    cfg = restore(dest, tmp_path / "grader")
    assert cfg.team == "team-1" and str(tmp_path / "grader") in cfg.data_path
    got = read(cfg, f"SELECT iri, title, score FROM results AT (VERSION => {snapshot}) ORDER BY iri")
    assert got == want and len(got) == 2
    assert read(cfg, "SELECT sha256 FROM kdb.media") == [(h,)]
    assert read(cfg, "SELECT action_id FROM kdb.action_log ORDER BY 1") == read(cfg_of(tmp_path), "SELECT action_id FROM kdb.action_log ORDER BY 1")
    lake = Lake.open(cfg)
    try:
        assert lake.snapshot() == snapshot
    finally:
        lake.close()
    assert store_for(cfg).get(f"media/sha256/{h}") == (tmp_path / "lake" / "data" / "media" / "sha256" / h).read_bytes()


def test_restore_reads_the_restored_files_not_the_originals(ctx, pdfs, tmp_path):
    _, dest = exported(ctx, pdfs, tmp_path)
    cfg = restore(dest, tmp_path / "grader")
    import shutil
    shutil.rmtree(tmp_path / "lake" / "data")
    assert read(cfg, "SELECT count(*) FROM results") == [(2,)]


def test_export_refuses_a_snapshot_other_than_the_latest(ctx, pdfs, tmp_path):
    filled(ctx, pdfs)
    old = ctx.lake.snapshot()
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [{"iri": IRI + "-3", "title": "Later"}]})
    latest = ctx.lake.snapshot()
    ctx.lake.close()
    with pytest.raises(ExportError, match=f"the latest is {latest}"):
        export(cfg_of(tmp_path), ctx.root, tmp_path / "drive", snapshot=old)
    assert not (tmp_path / "drive").exists()
    assert export(cfg_of(tmp_path), ctx.root, tmp_path / "drive", snapshot=latest).name == f"team-1-{latest}"


def test_a_restored_latest_export_reads_with_a_plain_select(ctx, pdfs, tmp_path):
    filled(ctx, pdfs)
    UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": [{"iri": IRI + "-3", "title": "Later"}]})
    want = read_same(ctx, ctx.lake.snapshot())
    ctx.lake.close()
    cfg = restore(export(cfg_of(tmp_path), ctx.root, tmp_path / "drive"), tmp_path / "grader")
    assert read(cfg, "SELECT iri FROM results ORDER BY iri") == want and len(want) == 3


def read_same(ctx, snapshot):
    return ctx.lake.con.execute(f"SELECT iri FROM results AT (VERSION => {snapshot}) ORDER BY iri").fetchall()


def test_restore_refuses_tampered_object(ctx, pdfs, tmp_path, monkeypatch):
    h, dest = exported(ctx, pdfs, tmp_path)
    obj = dest / "media" / "sha256" / h
    obj.write_bytes(bytes([obj.read_bytes()[0] ^ 1]) + obj.read_bytes()[1:])
    import duckdb
    opened = []
    real = duckdb.connect
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: opened.append(a) or real(*a, **k))
    with pytest.raises(RestoreError, match=f"media/sha256/{h}"):
        restore(dest, tmp_path / "grader")
    assert opened == [] and not (tmp_path / "grader").exists()


def test_restore_refuses_a_tampered_catalog(ctx, pdfs, tmp_path):
    _, dest = exported(ctx, pdfs, tmp_path)
    cat = dest / "catalog" / "meta.ducklake"
    cat.write_bytes(cat.read_bytes() + b"x")
    with pytest.raises(RestoreError, match="catalog/meta.ducklake"):
        restore(dest, tmp_path / "grader")


def test_restore_names_a_missing_file(ctx, pdfs, tmp_path):
    h, dest = exported(ctx, pdfs, tmp_path)
    (dest / "media" / "sha256" / h).unlink()
    with pytest.raises(RestoreError, match=f"media/sha256/{h}.*missing"):
        restore(dest, tmp_path / "grader")


def test_restore_without_a_manifest_or_with_a_bad_one(tmp_path):
    (tmp_path / "e").mkdir()
    with pytest.raises(RestoreError, match="manifest.json is missing"):
        restore(tmp_path / "e", tmp_path / "o")
    (tmp_path / "e" / "manifest.json").write_text("{not json")
    with pytest.raises(RestoreError, match="cannot be read"):
        restore(tmp_path / "e", tmp_path / "o")
    (tmp_path / "e" / "manifest.json").write_text(json.dumps({"team": "t", "snapshot": 1, "iri_base": "x/",
                                                              "files": {"../evil": "0" * 64}}))
    with pytest.raises(RestoreError, match="catalog"):
        restore(tmp_path / "e", tmp_path / "o")


def test_restore_refuses_a_path_that_leaves_the_export(tmp_path):
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / "manifest.json").write_text(json.dumps({"team": "t", "snapshot": 1, "iri_base": "x/", "files": {
        "catalog/c.ducklake": "0" * 64, "../evil": "0" * 64}}))
    with pytest.raises(RestoreError, match="../evil|not a path inside"):
        restore(tmp_path / "e", tmp_path / "o")


def test_export_errors_are_clear(ctx, pdfs, tmp_path):
    filled(ctx, pdfs)
    ctx.lake.close()
    cfg = cfg_of(tmp_path)
    with pytest.raises(ExportError, match="snapshot 999 does not exist"):
        export(cfg, ctx.root, tmp_path / "drive", snapshot=999)
    dest = export(cfg, ctx.root, tmp_path / "drive")
    with pytest.raises(ExportError, match="not empty"):
        export(cfg, ctx.root, tmp_path / "drive")
    with pytest.raises(RestoreError, match="not empty"):
        restore(dest, dest)


def toml(tmp_path):
    cfg = cfg_of(tmp_path)
    path = tmp_path / "kdb.toml"
    path.write_text(f'[lake]\ncatalog = "{cfg.catalog}"\ndata_path = "{cfg.data_path}"\nteam = "team-1"\n'
                    f'iri_base = "{cfg.iri_base}"\n')
    return path


def test_the_commands_print_errors_not_tracebacks(ctx, pdfs, tmp_path, capsys):
    filled(ctx, pdfs)
    ctx.lake.close()
    path = toml(tmp_path)
    assert main([str(ctx.root), str(tmp_path / "drive"), "--lake-config", str(path), "--snapshot", "999"]) == 2
    assert "snapshot 999 does not exist" in capsys.readouterr().err
    assert main([str(ctx.root), str(tmp_path / "drive"), "--lake-config", str(path)]) == 0
    [dest] = (tmp_path / "drive").iterdir()
    assert main([str(ctx.root), str(tmp_path / "drive"), "--lake-config", str(path)]) == 2
    assert "not empty" in capsys.readouterr().err
    assert restore_main([str(dest), str(tmp_path / "grader")]) == 0
    assert restore_main([str(dest), str(tmp_path / "grader")]) == 2
    (tmp_path / "x").mkdir()
    assert restore_main([str(tmp_path / "x"), str(tmp_path / "g2")]) == 2
    assert "manifest.json is missing" in capsys.readouterr().err
    assert main([str(ctx.root), str(tmp_path / "d2"), "--lake-config", str(tmp_path / "none.toml")]) == 2
