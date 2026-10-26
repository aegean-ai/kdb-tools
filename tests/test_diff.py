import json
import shutil
import subprocess

from conftest import GOOD_BODY, concept, page

from kdb_tools.diff import diff, main

STAMP = {"by": "team-1-agent/qwen3-32b", "at": "2026-11-02T18:00:00Z"}
NEW_STAMP = {"by": "team-1-agent/qwen3-32b", "at": "2026-11-20T09:00:00Z"}


def snapshot(bundle, tmp_path, name):
    dst = tmp_path / name
    shutil.copytree(bundle.root, dst)
    return dst


def rules(report):
    return sorted((f.rule, f.level, f.path) for f in report.findings)


def test_identical_bundles_have_no_changes(bundle, tmp_path):
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    report = diff(old, bundle.root)
    assert report.extra["added"] == [] and report.extra["changed"] == [] and report.extra["removed"] == []
    assert report.extra["unchanged"] == 1 and rules(report) == []


def test_added_removed_and_changed_pages_are_listed(bundle, tmp_path):
    bundle.add("models/openvla.md", concept())
    bundle.add("models/octo.md", concept(title="Octo"))
    old = snapshot(bundle, tmp_path, "old")
    (bundle.root / "models" / "octo.md").unlink()
    bundle.add("models/pi0.md", concept(title="π0"))
    bundle.add("models/openvla.md", concept(generated=NEW_STAMP), GOOD_BODY + "\nA new claim.[^kim2024-p3-s2]\n")
    report = diff(old, bundle.root)
    assert report.extra["added"] == ["models/pi0.md"]
    assert report.extra["removed"] == ["models/octo.md"]
    [changed] = report.extra["changed"]
    assert changed["path"] == "models/openvla.md" and changed["body"] is True
    assert changed["frontmatter"] == ["generated"]
    assert rules(report) == []


def test_a_frontmatter_only_change_without_a_new_stamp_is_a_warning(bundle, tmp_path):
    # OKF 5.2: generated.at marks the content's last meaningful change, and a new contested_by is one
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    bundle.add("models/openvla.md", concept(contested_by={"kim2024-p3-s2": ["x"]}))
    report = diff(old, bundle.root)
    [changed] = report.extra["changed"]
    assert changed["body"] is False and changed["frontmatter"] == ["contested_by"]
    assert rules(report) == [("diff.stale-stamp", "warning", "models/openvla.md")]


def test_a_frontmatter_only_change_with_a_new_stamp_is_clean(bundle, tmp_path):
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    bundle.add("models/openvla.md", concept(contested_by={"kim2024-p3-s2": ["x"]}, generated=NEW_STAMP))
    assert rules(diff(old, bundle.root)) == []


def test_restamping_after_only_a_verification_is_an_error(bundle, tmp_path):
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    bundle.add("models/openvla.md", concept(verified={"by": "human:ta", "at": "2026-11-03T10:00:00Z"}, generated=NEW_STAMP))
    assert rules(diff(old, bundle.root)) == [("diff.stamp", "error", "models/openvla.md")]


def test_restamping_a_page_whose_body_did_not_change_is_an_error(bundle, tmp_path):
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    bundle.add("models/openvla.md", concept(generated=NEW_STAMP))
    assert rules(diff(old, bundle.root)) == [("diff.stamp", "error", "models/openvla.md")]


def test_changing_the_body_without_a_new_stamp_is_a_warning(bundle, tmp_path):
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    bundle.add("models/openvla.md", concept(), GOOD_BODY + "\nA new claim.[^kim2024-p3-s2]\n")
    assert rules(diff(old, bundle.root)) == [("diff.stale-stamp", "warning", "models/openvla.md")]


def test_rewriting_a_page_without_changing_its_content_is_an_error(bundle, tmp_path):
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    fm = concept()
    reordered = {k: fm[k] for k in reversed(list(fm))}
    bundle.write("models/openvla.md", page(reordered, GOOD_BODY))
    report = diff(old, bundle.root)
    assert rules(report) == [("diff.rewrite", "error", "models/openvla.md")]
    assert report.extra["changed"][0]["frontmatter"] == [] and report.extra["changed"][0]["body"] is False


def test_reserved_files_and_papers_are_listed_separately(bundle, tmp_path):
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    bundle.write("log.md", (bundle.root / "log.md").read_text() + "\n## 2026-11-20\n* **Update**: Ingested LIBERO-Plus.\n")
    (bundle.root / "papers" / "fei2025.pdf").write_bytes(b"%PDF-1.4\n")
    files = diff(old, bundle.root).extra["files"]
    assert files == {"added": ["papers/fei2025.pdf"], "changed": ["log.md"], "removed": []}


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_two_git_revisions_of_a_bundle_in_a_subdirectory_can_be_compared(bundle, tmp_path):
    repo = bundle.root.parent
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    bundle.add("models/openvla.md", concept())
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "build")
    git(repo, "tag", "submission-1")
    bundle.add("models/pi0.md", concept(title="π0"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ingest")
    report = diff(bundle.root, git_revs=("submission-1", "HEAD"))
    assert report.extra["added"] == ["models/pi0.md"]
    assert main([str(bundle.root), "--git", "submission-1", "HEAD", "--json"]) == 0


def test_main_takes_two_directories_and_prints_json(bundle, tmp_path, capsys):
    bundle.add("models/openvla.md", concept())
    old = snapshot(bundle, tmp_path, "old")
    bundle.add("models/openvla.md", concept(generated=NEW_STAMP))
    assert main([str(old), str(bundle.root), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["tool"] == "okf-diff" and out["changed"][0]["path"] == "models/openvla.md"
    assert main([str(old), str(tmp_path / "nowhere")]) == 2


# --- the lakehouse between two snapshots ---

import pytest  # noqa: E402
from test_objects import PICK, RESULT, T1, register, setup_kdb  # noqa: E402,F401
from test_objects import pdfs  # noqa: E402,F401

from kdb_tools.actions.objects import DeleteObjects, UpsertObjects  # noqa: E402
from kdb_tools.diff import lake_diff  # noqa: E402

LIRI = T1 + "results/openvla-libero"


@pytest.fixture
def lctx(bundle, tmp_path):
    (tmp_path / "lake").mkdir()
    c = setup_kdb(bundle, tmp_path / "lake")
    yield c
    c.lake.close()


def lcfg(tmp_path):
    from kdb_tools.lake import LakeConfig
    return LakeConfig(catalog=tmp_path / "lake" / "meta.ducklake", data_path=str(tmp_path / "lake" / "data") + "/",
                      team="team-1", iri_base="https://data.aegean.ai/vla/team-1/")


def upsert(ctx, *rows):
    return UpsertObjects().submit(ctx, {"class_iri": RESULT, "rows": list(rows)})


def ld(ctx, tmp_path, a, b):
    ctx.lake.close()
    return lake_diff(lcfg(tmp_path), ctx.root, a, b)


def test_lake_diff_lists_added_changed_and_removed_rows_with_their_action_ids(lctx, tmp_path):
    first = upsert(lctx, {"iri": LIRI, "title": "OpenVLA"}, {"iri": LIRI + "-2", "title": "Octo"})
    s1 = lctx.lake.snapshot()
    second = upsert(lctx, {"iri": LIRI, "title": "OpenVLA on LIBERO"}, {"iri": LIRI + "-3", "title": "Pi0"})
    third = DeleteObjects().submit(lctx, {"class_iri": RESULT, "iris": [LIRI + "-2"], "reason": "duplicate"})
    s2 = lctx.lake.snapshot()
    report = ld(lctx, tmp_path, s1, s2)
    t = report.extra["tables"]["results"]
    assert t["added"] == [{"iri": LIRI + "-3", "actions": [second]}]
    assert t["changed"] == [{"iri": LIRI, "actions": [second]}]
    assert t["removed"] == [{"iri": LIRI + "-2", "actions": [third]}]
    assert report.extra["actions"] == [second, third] and first not in report.extra["actions"]
    assert rules(report) == [("diff.row-added", "warning", "results"), ("diff.row-changed", "warning", "results"),
                             ("diff.row-removed", "warning", "results")]
    assert report.errors == 0


def test_lake_diff_includes_media_and_a_table_bound_after_the_first_snapshot(lctx, pdfs, tmp_path):
    s0 = lctx.lake.snapshot()
    pdf, _, _ = pdfs
    register(lctx, pdf)
    report = ld(lctx, tmp_path, s0, s0 + 1)
    [added] = report.extra["tables"]["kdb.media"]["added"]
    assert len(added["actions"]) == 1 and report.errors == 0


def test_a_row_changed_outside_an_action_is_unlogged(lctx, tmp_path):
    upsert(lctx, {"iri": LIRI, "title": "OpenVLA"})
    s1 = lctx.lake.snapshot()
    lctx.lake.con.execute("UPDATE results SET title = 'sneaky' WHERE iri = ?", [LIRI])
    lctx.lake.con.execute("DELETE FROM kdb.media")  # nothing to delete: no change
    report = ld(lctx, tmp_path, s1, s1 + 1)
    assert rules(report) == [("diff.row-changed", "warning", "results"), ("diff.unlogged", "error", "results")]


def test_a_hand_edit_of_a_row_a_logged_action_also_touched_is_unlogged(lctx, tmp_path):
    s0 = lctx.lake.snapshot()
    x = upsert(lctx, {"iri": LIRI, "title": "OpenVLA"})
    lctx.lake.con.execute("UPDATE results SET title = 'sneaky' WHERE iri = ?", [LIRI])
    report = ld(lctx, tmp_path, s0, lctx.lake.snapshot())
    assert [(f.rule, f.level) for f in report.findings if f.rule == "diff.unlogged"] == [("diff.unlogged", "error")]
    assert LIRI in [f for f in report.findings if f.rule == "diff.unlogged"][0].message
    assert report.extra["tables"]["results"]["added"] == [{"iri": LIRI, "actions": [x]}]


def test_lake_diff_of_equal_snapshots_is_empty(lctx, tmp_path):
    upsert(lctx, {"iri": LIRI, "title": "OpenVLA"})
    s = lctx.lake.snapshot()
    assert rules(ld(lctx, tmp_path, s, s)) == []


def test_lake_diff_cli(lctx, tmp_path, capsys):
    upsert(lctx, {"iri": LIRI, "title": "OpenVLA"})
    s1 = lctx.lake.snapshot()
    upsert(lctx, {"iri": LIRI, "title": "Changed"})
    s2 = lctx.lake.snapshot()
    lctx.lake.close()
    cfg = lcfg(tmp_path)
    toml = tmp_path / "kdb.toml"
    toml.write_text(f'[lake]\ncatalog = "{cfg.catalog}"\ndata_path = "{cfg.data_path}"\nteam = "team-1"\niri_base = "{cfg.iri_base}"\n')
    base = [str(lctx.root), "--lake-config", str(toml)]
    assert main(base + ["--snapshots", str(s1), str(s2), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tables"]["results"]["changed"][0]["iri"] == LIRI
    assert main(base + ["--snapshots", "0", "9999"]) == 2
    assert "snapshot 9999 does not exist" in capsys.readouterr().err
    assert main([str(lctx.root), "--snapshots", "1", "2"]) == 2
    assert main(base + ["--snapshots", "1", "2", "--git", "a", "b"]) == 2
    assert main(["--lake-config", str(toml), "--snapshots", "1", "2", str(tmp_path / "nope")]) == 2
    assert main(base + ["--snapshots", "0", "1"]) == 0  # before the system tables existed


def test_a_bind_table_that_adds_a_column_is_credited_with_the_row_changes(lctx, tmp_path):
    from test_objects import BINDING, CORE

    from kdb_tools.actions.types import AddProperty, BindTable

    upsert(lctx, {"iri": LIRI, "title": "OpenVLA"})
    s1 = lctx.lake.snapshot()
    AddProperty().submit(lctx, {"domain": "core", "slug": "notes", "title": "Notes", "datatype": "string",
                                "description": "Notes."})
    binding = {**BINDING, "columns": {**BINDING["columns"], "notes": CORE + "properties/notes"}}
    bound = BindTable().submit(lctx, {"class_iri": RESULT, "binding": binding})
    report = ld(lctx, tmp_path, s1, lctx.lake.snapshot())
    assert [f for f in report.findings if f.rule == "diff.unlogged"] == []
    assert report.extra["tables"]["results"]["changed"] == [{"iri": LIRI, "actions": [bound]}]


def test_a_table_named_after_a_sql_keyword_reads_at_a_snapshot(lctx):
    from kdb_tools.diff import _rows_at
    lctx.lake.con.execute('CREATE TABLE "select" (iri VARCHAR, title VARCHAR)')
    lctx.lake.con.execute("""INSERT INTO "select" VALUES ('x', 'a')""")
    s = lctx.lake.snapshot()
    assert _rows_at(lctx.lake, "select", s, {"select"}) == {"x": {"iri": "x", "title": "a"}}
