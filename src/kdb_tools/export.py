"""okf-export and okf-restore: a lakehouse as one self-contained folder, and back.

A team submits by exporting into a folder that syncs to its shared Drive folder (kdb-tools never talks
to Drive). The export holds the catalog file, the data files the catalog references at one snapshot,
every media object in kdb.media, and manifest.json with the sha256 of each file. Restore checks every
hash first and opens nothing until all of them match, so a changed byte is found before it is read.

Small tables sit inside the catalog file (DuckLake inlines them) and have no data file. Only the
latest snapshot can be exported: the catalog copy would carry later snapshots and inlined rows whose
data files are not exported, so an older export would not be self-contained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .config import ConfigError
from .lake import Lake, LakeConfig, _env, load_config, store_for
from .report import fail

TOOL = "okf-export"
MANIFEST = "manifest.json"


class ExportError(Exception):
    pass


class RestoreError(Exception):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tables_at(lake: Lake, snapshot: int) -> list[tuple[str, str]]:
    """(schema, table) of every table that exists at a snapshot."""
    return lake.con.execute(
        "SELECT s.schema_name, t.table_name FROM __ducklake_metadata_lake.ducklake_table t "
        "JOIN __ducklake_metadata_lake.ducklake_schema s USING (schema_id) "
        "WHERE t.begin_snapshot <= ? AND (t.end_snapshot IS NULL OR t.end_snapshot > ?) "
        "AND s.begin_snapshot <= ? AND (s.end_snapshot IS NULL OR s.end_snapshot > ?)",
        [snapshot] * 4).fetchall()


def snapshot_ids(lake: Lake) -> list[int]:
    return [r[0] for r in lake.con.execute("SELECT snapshot_id FROM ducklake_snapshots('lake') ORDER BY 1").fetchall()]


def data_files(lake: Lake, snapshot: int) -> list[str]:
    """Every data and delete file the catalog references at a snapshot, as the paths DuckLake gives."""
    found = set()
    for schema, table in tables_at(lake, snapshot):
        for row in lake.con.execute("SELECT * FROM ducklake_list_files('lake', ?, schema => ?, snapshot_version => ?)",
                                    [table, schema, snapshot]).fetchall():
            found.update(p for p in (row[0], row[4]) if p)
    return sorted(found)


def _read(cfg: LakeConfig, path: str) -> bytes:
    """A data file's bytes. An s3:// path goes through boto3 (untested in this repository)."""
    if path.startswith("s3://"):
        from .lake import S3Store

        bucket, key = path[len("s3://"):].split("/", 1)
        return S3Store(cfg.s3_endpoint, bucket, _env(cfg.key_env), _env(cfg.secret_env)).get(key)
    return Path(path).read_bytes()


def export(cfg: LakeConfig, bundle: Path, out: Path, snapshot: int | None = None) -> Path:
    """Write out/<team>-<snapshot>/ and return it. The bundle is not exported: it is graded from git."""
    lake = Lake.open(cfg)
    try:
        latest = lake.snapshot()
        snapshot = latest if snapshot is None else snapshot
        if snapshot not in snapshot_ids(lake):
            raise ExportError(f"snapshot {snapshot} does not exist; the lake has snapshots 0 to {latest}")
        if snapshot != latest:
            raise ExportError(f"snapshot {snapshot} is not the latest; export the lake at the snapshot you submit; "
                              f"the latest is {latest}")
        dest = Path(out) / f"{cfg.team}-{snapshot}"
        if dest.exists() and any(dest.iterdir()):
            raise ExportError(f"{dest} is not empty; export into a fresh folder")
        files: dict[str, bytes] = {}
        prefix = cfg.data_path.rstrip("/") + "/"
        for path in data_files(lake, snapshot):
            if not path.startswith(prefix):
                raise ExportError(f"data file {path} is outside the data path {cfg.data_path}")
            try:
                files["data/" + path[len(prefix):]] = _read(cfg, path)
            except OSError as e:
                raise ExportError(f"cannot read data file {path}: {e}")
        store = store_for(cfg)
        for sha, key in lake.con.execute(f"SELECT sha256, object_key FROM kdb.media AT (VERSION => {int(snapshot)})").fetchall():
            try:
                data = store.get(key)
            except Exception as e:
                raise ExportError(f"cannot read media object {key}: {e}")
            if _sha(data) != sha:
                raise ExportError(f"media object {key} does not match its sha256 {sha}")
            files["media/" + key.removeprefix("media/")] = data
        lake.con.execute("CHECKPOINT __ducklake_metadata_lake")  # fold the write-ahead log into the file we copy
        files["catalog/" + Path(cfg.catalog).name] = Path(cfg.catalog).read_bytes()
    finally:
        lake.close()
    dest.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {"team": cfg.team, "snapshot": snapshot, "iri_base": cfg.iri_base,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": {rel: _sha(data) for rel, data in sorted(files.items())}}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return dest


def _manifest(src: Path) -> dict:
    try:
        manifest = json.loads((src / MANIFEST).read_text())
    except FileNotFoundError:
        raise RestoreError(f"{src / MANIFEST} is missing; this is not an export")
    except (OSError, ValueError) as e:
        raise RestoreError(f"{src / MANIFEST} cannot be read: {e}")
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()) \
            or not isinstance(manifest.get("team"), str) or not isinstance(manifest.get("snapshot"), int) \
            or not isinstance(manifest.get("iri_base"), str):
        raise RestoreError(f"{src / MANIFEST} is malformed: it needs team, snapshot, iri_base and files")
    return manifest


def restore(src: Path, into: Path) -> LakeConfig:
    """Check every hash in src's manifest, then copy the files into `into` and point the catalog at them.

    Raises RestoreError naming the first file that is missing or differs, before anything is opened.
    Media lands under data/media/ so that store_for(cfg) finds it, as it does in a live lake."""
    src, into = Path(src), Path(into)
    manifest = _manifest(src)
    catalogs = [rel for rel in manifest["files"] if rel.startswith("catalog/")]
    if len(catalogs) != 1:
        raise RestoreError("the manifest must list exactly one catalog file")
    for rel in manifest["files"]:
        if Path(rel).is_absolute() or ".." in Path(rel).parts or rel.split("/")[0] not in ("catalog", "data", "media"):
            raise RestoreError(f"{rel} is not a path inside an export")
    verified: dict[str, bytes] = {}
    for rel, want in manifest["files"].items():
        try:
            data = (src / rel).read_bytes()
        except OSError:
            raise RestoreError(f"{rel} is missing from the export")
        if _sha(data) != want:
            raise RestoreError(f"{rel} does not match its hash in the manifest; the export was changed after it was written")
        verified[rel] = data
    if into.exists() and any(into.iterdir()):
        raise RestoreError(f"{into} is not empty; restore into a fresh folder")
    for rel, data in verified.items():
        target = into / (rel if not rel.startswith("media/") else "data/" + rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (into / "data").mkdir(exist_ok=True)
    catalog = into / catalogs[0]
    data_path = str((into / "data").resolve()) + "/"
    con = duckdb.connect(str(catalog))  # the catalog is a plain DuckDB file; its data path is one metadata row
    try:
        con.execute("UPDATE ducklake_metadata SET value = ? WHERE key = 'data_path'", [data_path])
    finally:
        con.close()
    return LakeConfig(catalog=catalog, data_path=data_path, team=manifest["team"], iri_base=manifest["iri_base"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL, description="Write the lakehouse into a folder, with a manifest of hashes")
    parser.add_argument("bundle", type=Path, help="the bundle directory (it is not exported; the grader reads it from git)")
    parser.add_argument("out", type=Path, help="the folder to write into, such as your synced Drive folder")
    parser.add_argument("--lake-config", type=Path, metavar="PATH", default=Path("kdb.toml"), help="the kdb.toml that holds [lake]")
    parser.add_argument("--snapshot", type=int, help="must be the latest snapshot, which is the default")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.lake_config)
    except (OSError, ValueError, KeyError, ConfigError) as e:
        return fail(TOOL, f"cannot read the lake config {args.lake_config}: {e}")
    if cfg is None:
        return fail(TOOL, f"no [lake] table in {args.lake_config}")
    try:
        dest = export(cfg, args.bundle, args.out, args.snapshot)
    except (ExportError, ConfigError, duckdb.Error) as e:
        return fail(TOOL, str(e))
    print(f"exported to {dest}")
    return 0


def restore_main(argv: list[str] | None = None) -> int:
    tool = "okf-restore"
    parser = argparse.ArgumentParser(prog=tool, description="Check an export's hashes, then restore it into a folder")
    parser.add_argument("src", type=Path, help="the export folder, <team>-<snapshot>")
    parser.add_argument("into", type=Path, help="an empty folder to restore into")
    args = parser.parse_args(argv)
    try:
        cfg = restore(args.src, args.into)
    except (RestoreError, duckdb.Error) as e:
        return fail(tool, str(e))
    print(f"restored to {args.into}\ncatalog   {cfg.catalog}\ndata_path {cfg.data_path}\nteam      {cfg.team}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
