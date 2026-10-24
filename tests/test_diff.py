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
