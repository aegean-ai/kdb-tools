"""okf-diff: compare two versions of a kDB bundle and list every page that changed.

Give two bundle directories, or one bundle directory inside a git repository and two
revisions with --git. An incremental ingest should change only the pages the new paper
affects; every other page stays byte-identical and keeps its generated stamp. Following OKF
section 5.2, generated.at marks a page's last meaningful change: a change to the body or to any
frontmatter key other than the stamps themselves needs a new one.

With --lake-config and --snapshots A B it compares the lakehouse instead: for each table bound by a
class of the bundle, and for kdb.media, the rows added, removed and changed between two snapshots,
and the action ids that made each change. A change that no logged action explains is diff.unlogged.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

import duckdb

from . import bundle as B
from . import lake as L
from . import onto as O
from .actions.base import KEYS, _ident, load_row_edits
from .config import ConfigError
from .export import snapshot_ids, tables_at
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


def _rows_at(lake: L.Lake, table: str, snapshot: int, exists: set[str]) -> dict[str, dict]:
    """A table's rows at a snapshot keyed by iri (or sha256), as column dicts; empty when it did not exist yet."""
    if table not in exists:
        return {}
    cur = lake.con.execute(f"SELECT * FROM {_ident(table)} AT (VERSION => {int(snapshot)})")
    names = [d[0] for d in cur.description]
    key = KEYS.get(table, "iri")
    return {r[names.index(key)]: dict(zip(names, r)) for r in cur.fetchall()}


def _bound_table(params) -> str | None:
    """The table a logged BindTable's params bind, or None."""
    try:
        p = json.loads(params) if isinstance(params, str) else params
    except ValueError:
        return None
    binding = p.get("binding") if isinstance(p, dict) else None
    return binding.get("table") if isinstance(binding, dict) else None


def lake_diff(cfg: L.LakeConfig, bundle: Path, a: int, b: int) -> Report:
    """Rows added, removed and changed in each bound table between snapshots a and b, with their action ids.

    Each action is one transaction, so its log row and its data rows commit in the same snapshot. The
    diff therefore walks the snapshots from a to b one step at a time and credits a row change only to
    the actions logged in that same step. A hand edit of a row that a logged action also touched
    earlier in the range is still unlogged. A BindTable that adds or changes columns of its table
    rewrites that table's rows without naming them, so it is credited with the table's row changes
    in its step."""
    report = Report(TOOL)
    ontology = O.load_ontology(B.load(bundle))
    tables = sorted({k.binding.table for k in ontology.classes.values() if k.binding} | {"kdb.media"})
    log = "kdb.action_log"  # absent from the earliest snapshots, before the system tables were made
    lake = L.Lake.open(cfg)
    try:
        ids = snapshot_ids(lake)
        for n in (a, b):
            if n not in ids:
                raise ValueError(f"snapshot {n} does not exist; the lake has snapshots {min(ids)} to {max(ids)}")
        cache: dict[int, tuple[dict[str, dict[str, dict]], dict[str, list], dict[str, str]]] = {}

        def state(n: int):
            """The rows of each bound table, the action_log edits (by action id), and the table each
            BindTable bound (by action id), at snapshot n."""
            if n not in cache:
                exists = {f"{x}.{t}" if x != "main" else t for x, t in tables_at(lake, n)}
                rows = {t: _rows_at(lake, t, n, exists) for t in tables}
                edits, binds = {}, {}
                if log in exists:
                    for action_id, kind, params, e in lake.con.execute(
                            f"SELECT action_id, action_type, params, row_edits FROM {log} AT (VERSION => {n})").fetchall():
                        edits[action_id] = load_row_edits(e)
                        if kind == "BindTable" and isinstance(table := _bound_table(params), str):
                            binds[action_id] = table
                cache[n] = (rows, edits, binds)
            return cache[n]

        steps = [n for n in ids if a <= n <= b]
        credited: dict[tuple[str, str], list[str]] = {}
        new_actions: list[str] = []
        for prev, cur in zip(steps, steps[1:]):
            (old, old_log, _), (new, new_log, binds) = state(prev), state(cur)
            made = [x for x in new_log if x not in old_log]
            new_actions += made
            named = {(e["table"], e["iri"]): x for x in made for e in new_log[x]}
            rebound: dict[str, list[str]] = {}
            for x in made:
                if x in binds:
                    rebound.setdefault(binds[x], []).append(x)
            for table in tables:
                touched = {k for k in set(old[table]) | set(new[table]) if old[table].get(k) != new[table].get(k)}
                for k in sorted(touched):
                    if (table, k) in named:
                        credited.setdefault((table, k), []).extend(x for x in made if any(
                            (e["table"], e["iri"]) == (table, k) for e in new_log[x]))
                    elif table in rebound:
                        credited.setdefault((table, k), []).extend(rebound[table])
                    else:
                        report.error("diff.unlogged", table, f"{k} changed in snapshot {cur} but no logged action edited it")
        first, last = state(a)[0], state(b)[0]
        changes: dict[str, dict] = {}
        for table in tables:
            old, new = first[table], last[table]
            found = {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
                     "changed": sorted(k for k in set(old) & set(new) if old[k] != new[k])}
            entry = {kind: [{"iri": k, "actions": credited.get((table, k), [])} for k in keys]
                     for kind, keys in found.items()}
            changes[table] = entry
            for kind, rule in (("added", "diff.row-added"), ("removed", "diff.row-removed"), ("changed", "diff.row-changed")):
                for item in entry[kind]:
                    ids_text = ", ".join(item["actions"])
                    report.warning(rule, table, f"{item['iri']} ({ids_text})" if ids_text else item["iri"])
        report.extra.update({"snapshots": [a, b], "tables": changes, "actions": new_actions})
    finally:
        lake.close()
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
    parser.add_argument("--lake-config", type=Path, metavar="PATH", help="the kdb.toml that holds [lake]; with --snapshots")
    parser.add_argument("--snapshots", nargs=2, type=int, metavar=("A", "B"), help="compare the lakehouse at two snapshots")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    args = parser.parse_args(argv)
    if args.snapshots or args.lake_config:
        if not (args.snapshots and args.lake_config):
            return fail(TOOL, "give --lake-config and --snapshots together")
        if args.git or len(args.bundles) != 1 or not args.bundles[0].is_dir():
            return fail(TOOL, "with --snapshots, give one bundle directory and no --git")
        try:
            cfg = L.load_config(args.lake_config)
        except (OSError, ValueError, KeyError, ConfigError) as e:
            return fail(TOOL, f"cannot read the lake config {args.lake_config}: {e}")
        if cfg is None:
            return fail(TOOL, f"no [lake] table in {args.lake_config}")
        try:
            report = lake_diff(cfg, args.bundles[0], *args.snapshots)
        except (ValueError, ConfigError, duckdb.Error) as e:
            return fail(TOOL, str(e))
        report.print(args.json)
        return report.exit_code()
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
