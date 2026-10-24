"""okf-diff: compare two versions of a kDB bundle and list every page that changed.

Give two bundle directories, or one bundle directory inside a git repository and two
revisions with --git. An incremental ingest should change only the pages the new paper
affects; every other page stays byte-identical and keeps its generated stamp. Following OKF
section 5.2, generated.at marks a page's last meaningful change: a change to the body or to any
frontmatter key other than the stamps themselves needs a new one.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import bundle as B
from .report import Report, fail

TOOL = "okf-diff"
STAMPS = {"generated", "verified"}  # record who wrote or checked a page; not its content


def files_of(root: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and ".git" not in rel.parts:
            out[rel.as_posix()] = path.read_bytes()
    return out


def compare(old: Path, new: Path) -> Report:
    report = Report(TOOL)
    old_files, new_files = files_of(old), files_of(new)
    old_pages = {p.rel: p for p in B.load(old).concepts}
    new_pages = {p.rel: p for p in B.load(new).concepts}

    added = sorted(set(new_pages) - set(old_pages))
    removed = sorted(set(old_pages) - set(new_pages))
    changed, unchanged = [], 0
    for rel in sorted(set(old_pages) & set(new_pages)):
        if old_files[rel] == new_files[rel]:
            unchanged += 1
            continue
        a, b = old_pages[rel], new_pages[rel]
        fa, fb = a.frontmatter or {}, b.frontmatter or {}
        keys = sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k))
        body = a.body != b.body
        changed.append({"path": rel, "body": body, "frontmatter": keys})
        content = body or any(k not in STAMPS for k in keys)
        if not body and not keys:
            report.error("diff.rewrite", rel, "the file was rewritten but its content did not change; leave it byte-identical")
        elif not content and "generated" in keys:
            report.error("diff.stamp", rel, "the content did not change, so the page keeps its old generated stamp")
        elif content and (fa.get("generated") or {}).get("at") == (fb.get("generated") or {}).get("at"):
            what = "the body" if body else "the frontmatter (" + ", ".join(k for k in keys if k not in STAMPS) + ")"
            report.warning("diff.stale-stamp", rel, f"{what} changed but generated.at did not")

    pages = set(old_pages) | set(new_pages)
    old_other = {k: v for k, v in old_files.items() if k not in pages}
    new_other = {k: v for k, v in new_files.items() if k not in pages}
    report.extra.update({
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "files": {
            "added": sorted(set(new_other) - set(old_other)),
            "changed": sorted(k for k in set(old_other) & set(new_other) if old_other[k] != new_other[k]),
            "removed": sorted(set(old_other) - set(new_other)),
        },
    })
    return report


def git(cwd: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True).stdout


def export(bundle_dir: Path, rev: str, into: Path) -> Path:
    """Write the bundle as it was at rev into a directory and return its root there."""
    top = Path(git(bundle_dir, "rev-parse", "--show-toplevel").decode().strip())
    prefix = git(bundle_dir, "rev-parse", "--show-prefix").decode().strip()
    archive = git(top, "archive", "--format=tar", rev, *([prefix] if prefix else []))
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(into, **safe)
    return into / prefix


def diff(old: Path, new: Path | None = None, git_revs: tuple[str, str] | None = None) -> Report:
    if git_revs is None:
        return compare(old, new)
    with tempfile.TemporaryDirectory() as tmp:
        a = export(old, git_revs[0], Path(tmp) / "old")
        b = export(old, git_revs[1], Path(tmp) / "new")
        report = compare(a, b)
    report.extra["revisions"] = list(git_revs)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    parser.add_argument("bundles", nargs="+", type=Path, help="OLD NEW directories, or one bundle directory with --git")
    parser.add_argument("--git", nargs=2, metavar=("OLD_REV", "NEW_REV"), help="compare two git revisions of one bundle")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    args = parser.parse_args(argv)
    if args.git:
        if len(args.bundles) != 1:
            return fail(TOOL, "with --git, give one bundle directory")
        if not args.bundles[0].is_dir():
            return fail(TOOL, f"{args.bundles[0]} is not a directory")
        try:
            report = diff(args.bundles[0], git_revs=tuple(args.git))
        except subprocess.CalledProcessError as exc:
            return fail(TOOL, f"git failed: {exc.stderr.decode().strip()}")
    else:
        if len(args.bundles) != 2:
            return fail(TOOL, "give two bundle directories, OLD and NEW")
        for d in args.bundles:
            if not d.is_dir():
                return fail(TOOL, f"{d} is not a directory")
        report = diff(*args.bundles)
    if args.json:
        report.print(True)
    else:
        x = report.extra
        for key in ("added", "removed"):
            for rel in x[key]:
                print(f"{key:9} {rel}")
        for c in x["changed"]:
            parts = (["body"] if c["body"] else []) + c["frontmatter"]
            print(f"changed   {c['path']} ({', '.join(parts) or 'formatting only'})")
        for key, rels in x["files"].items():
            for rel in rels:
                print(f"file {key:7} {rel}")
        print(f"{x['unchanged']} pages unchanged")
        report.print(False)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
