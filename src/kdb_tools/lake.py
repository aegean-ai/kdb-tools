"""The lakehouse: a DuckLake catalog for the kDB's structured data, plus an object store for media.

One DuckDB connection attaches the catalog as `lake`. Every write goes through `transaction()`, so a
failed action leaves no trace and each commit is one snapshot. Two system tables live in the `kdb`
schema: the action log and the media registry. Media bytes live in the object store, named by their
sha256, so a file is stored once. kdb.toml names the lake in a [lake] table and never holds a secret:
name the environment variable instead (key_env, secret_env).
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .config import ConfigError

COMPOSE = Path(__file__).parent / "data" / "compose.yml"  # RustFS for a team's own machine

SYSTEM_TABLES = [
    """CREATE TABLE IF NOT EXISTS kdb.action_log(action_id VARCHAR, action_type VARCHAR, action_version INTEGER,
       "at" TIMESTAMPTZ, actor VARCHAR, model VARCHAR, team VARCHAR, params JSON, page_edits JSON, row_edits JSON,
       evidence JSON, reverts VARCHAR, reverted_by VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS kdb.media(sha256 VARCHAR, media_type VARCHAR, bytes BIGINT, object_key VARCHAR,
       name VARCHAR, origin VARCHAR, annotates VARCHAR, team VARCHAR, registered_by VARCHAR)""",
]


@dataclass
class LakeConfig:
    catalog: Path
    data_path: str
    team: str
    iri_base: str  # must end with "/"
    s3_endpoint: str | None = None
    key_env: str | None = None
    secret_env: str | None = None
    relative_data: bool = False  # kdb.toml gave a local relative data_path, so the lake moves with its folder


REQUIRED = ("catalog", "data_path", "team", "iri_base")


def load_config(path: Path = Path("kdb.toml")) -> LakeConfig | None:
    """The [lake] table of kdb.toml, or None when there is none.

    A relative catalog or local data_path is taken from the folder that holds kdb.toml, not from the
    current directory, so every command finds the same lake wherever it runs."""
    raw = tomllib.loads(Path(path).read_text()).get("lake")
    if raw is None:
        return None
    for name, fix in (("key", "key_env"), ("secret", "secret_env")):
        if name in raw:
            raise ConfigError(f"do not write a {name} in the [lake] table of kdb.toml, where it can be committed; "
                              f"set {fix} to the name of the environment variable that holds it")
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        names = ", ".join(missing[:-1]) + " and " + missing[-1] if len(missing) > 1 else missing[0]
        raise ConfigError(f"the [lake] table of {path} needs {names}")
    here = Path(path).absolute().parent
    data_path = raw["data_path"]
    relative = "://" not in data_path and not Path(data_path).is_absolute()
    if relative:
        data_path = str(here / data_path).rstrip("/") + ("/" if data_path.endswith("/") else "")
    cfg = LakeConfig(catalog=here / raw["catalog"], data_path=data_path, team=raw["team"],
                     iri_base=raw["iri_base"], s3_endpoint=raw.get("s3_endpoint"), key_env=raw.get("key_env"),
                     secret_env=raw.get("secret_env"), relative_data=relative)
    if not cfg.iri_base.endswith("/"):
        raise ConfigError(f"[lake] iri_base must end with a slash, got {cfg.iri_base!r}")
    return cfg


def _sql(value) -> str:
    """A value as the text of a single-quoted SQL literal: each quote doubled."""
    return str(value).replace("'", "''")


def _env(name: str | None) -> str:
    value = os.environ.get(name) if name else None
    if not value:
        raise ConfigError(f"[lake] names the environment variable {name!r}, which is not set")
    return value


class Lake:
    def __init__(self, con: duckdb.DuckDBPyConnection, config: LakeConfig | None = None):
        self.con = con
        self.config = config  # the [lake] table it was opened from: its team and iri_base

    @classmethod
    def open(cls, cfg: LakeConfig) -> Lake:
        con = duckdb.connect()
        con.execute("INSTALL ducklake")
        con.execute("LOAD ducklake")
        if cfg.data_path.startswith("s3://"):
            con.execute("INSTALL httpfs")
            con.execute("LOAD httpfs")
            parts = [f"KEY_ID '{_sql(_env(cfg.key_env))}'", f"SECRET '{_sql(_env(cfg.secret_env))}'", "URL_STYLE 'path'"]
            if cfg.s3_endpoint:
                endpoint = cfg.s3_endpoint.split("://", 1)[-1]
                parts.append(f"ENDPOINT '{_sql(endpoint)}'")
                if cfg.s3_endpoint.startswith("http://"):
                    parts.append("USE_SSL false")
            con.execute(f"CREATE OR REPLACE SECRET lake_s3 (TYPE S3, {', '.join(parts)})")
        # A relative data_path moves with the project folder, but the catalog remembers the absolute
        # path it was made with; override it so a moved or renamed folder still opens.
        override = ", OVERRIDE_DATA_PATH true" if cfg.relative_data else ""
        con.execute(f"ATTACH 'ducklake:{_sql(cfg.catalog)}' AS lake (DATA_PATH '{_sql(cfg.data_path)}'{override})")
        con.execute("USE lake")
        return cls(con, cfg)

    def snapshot(self) -> int:
        return self.con.execute("SELECT max(snapshot_id) FROM ducklake_snapshots('lake')").fetchone()[0]

    @contextmanager
    def transaction(self):
        self.con.execute("BEGIN")
        try:
            yield self
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        try:
            self.con.execute("COMMIT")
        except BaseException:
            try:  # a failed commit can leave the transaction open; end it so none of it survives
                self.con.execute("ROLLBACK")
            except duckdb.Error:
                pass  # the failed commit already ended the transaction
            raise

    def ensure_system_tables(self) -> None:
        self.con.execute("CREATE SCHEMA IF NOT EXISTS kdb")
        for ddl in SYSTEM_TABLES:
            self.con.execute(ddl)

    def close(self) -> None:
        self.con.close()


class Store:
    """Content-addressed bytes: the key is derived from the sha256, so a file is stored once."""

    def put(self, sha256: str, data: bytes) -> str:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError


def object_key(sha256: str) -> str:
    return f"media/sha256/{sha256}"


class LocalStore(Store):
    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, sha256: str, data: bytes) -> str:
        """Write the bytes unless the file there already hashes to sha256. A temp file and a rename mean
        a reader never sees part of a file, and a torn file left by an earlier crash is rewritten."""
        key = object_key(sha256)
        path = self.root / key
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return key

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()


class S3Store(Store):
    """An S3-compatible bucket such as RustFS or R2. Needs the s3 extra: kdb-tools[s3]."""

    def __init__(self, endpoint: str, bucket: str, key: str, secret: str):
        import boto3

        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=key, aws_secret_access_key=secret)

    def put(self, sha256: str, data: bytes) -> str:
        key = object_key(sha256)
        if not self.exists(key):
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True


def store_for(cfg: LakeConfig) -> Store:
    """The object store the config points at: a bucket for an s3:// data path, else a local directory."""
    if cfg.data_path.startswith("s3://"):
        bucket = cfg.data_path[len("s3://"):].split("/", 1)[0]
        return S3Store(cfg.s3_endpoint, bucket, _env(cfg.key_env), _env(cfg.secret_env))
    return LocalStore(Path(cfg.data_path))
