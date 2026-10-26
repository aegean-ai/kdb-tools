"""The lakehouse module: a DuckLake catalog, the system tables, and the object store."""

import hashlib

import pytest

from kdb_tools.config import ConfigError
from kdb_tools.lake import Lake, LakeConfig, LocalStore, load_config, store_for


def _cfg(tmp_path):
    return LakeConfig(catalog=tmp_path / "meta.ducklake", data_path=str(tmp_path / "data") + "/", team="team-1",
                      iri_base="https://data.aegean.ai/vla/team-1/")


def _media(n):
    return f"INSERT INTO kdb.media VALUES ('{n * 32}', 'application/pdf', 9, 'media/sha256/x', 'a.pdf', 'upload', NULL, 'team-1', 'a1')"


def test_local_lake_round_trip(tmp_path):
    lake = Lake.open(_cfg(tmp_path))
    lake.ensure_system_tables()
    s0 = lake.snapshot()
    with lake.transaction():
        lake.con.execute(_media("ab"))
    assert lake.snapshot() > s0
    assert lake.con.execute("SELECT count(*) FROM kdb.media").fetchone()[0] == 1
    lake.close()


def test_ensure_system_tables_is_idempotent_and_has_columns(tmp_path):
    lake = Lake.open(_cfg(tmp_path))
    lake.ensure_system_tables()
    lake.ensure_system_tables()
    cols = [r[0] for r in lake.con.execute("DESCRIBE kdb.action_log").fetchall()]
    assert cols == ["action_id", "action_type", "action_version", "at", "actor", "model", "team", "params",
                    "page_edits", "row_edits", "evidence", "reverts", "reverted_by"]
    cols = [r[0] for r in lake.con.execute("DESCRIBE kdb.media").fetchall()]
    assert cols == ["sha256", "media_type", "bytes", "object_key", "name", "origin", "annotates", "team",
                    "registered_by"]
    lake.close()


def test_transaction_rolls_back(tmp_path):
    lake = Lake.open(_cfg(tmp_path))
    lake.ensure_system_tables()
    s0 = lake.snapshot()
    with pytest.raises(RuntimeError):
        with lake.transaction():
            lake.con.execute(_media("cd"))
            raise RuntimeError("boom")
    assert lake.con.execute("SELECT count(*) FROM kdb.media").fetchone()[0] == 0
    assert lake.snapshot() == s0
    lake.close()


def test_lake_reopens_with_data(tmp_path):
    cfg = _cfg(tmp_path)
    lake = Lake.open(cfg)
    lake.ensure_system_tables()
    with lake.transaction():
        lake.con.execute(_media("ef"))
    lake.close()
    again = Lake.open(cfg)
    assert again.con.execute("SELECT count(*) FROM kdb.media").fetchone()[0] == 1
    again.close()


def _write(tmp_path, body):
    p = tmp_path / "kdb.toml"
    p.write_text(body)
    return p


def test_config_loads_lake_table(tmp_path):
    p = _write(tmp_path, '[lake]\ncatalog = "meta.ducklake"\ndata_path = "data/"\nteam = "t"\n'
                         'iri_base = "https://x/y/"\n')
    cfg = load_config(p)
    assert cfg.team == "t" and cfg.iri_base == "https://x/y/" and cfg.s3_endpoint is None


def test_config_none_without_lake_table(tmp_path):
    assert load_config(_write(tmp_path, 'model = "m"\n')) is None


@pytest.mark.parametrize("name,env", [("secret", "secret_env"), ("key", "key_env")])
def test_config_refuses_inline_secret(tmp_path, name, env):
    p = _write(tmp_path, f'[lake]\ncatalog = "m"\ndata_path = "d/"\nteam = "t"\niri_base = "https://x/"\n'
                         f'{name} = "hunter2"\n')
    with pytest.raises(ConfigError, match=env):
        load_config(p)


def test_config_requires_trailing_slash_on_iri_base(tmp_path):
    p = _write(tmp_path, '[lake]\ncatalog = "m"\ndata_path = "d/"\nteam = "t"\niri_base = "https://x/y"\n')
    with pytest.raises(ConfigError, match="iri_base"):
        load_config(p)


def test_local_store_put_get(tmp_path):
    st = LocalStore(tmp_path)
    digest = hashlib.sha256(b"x").hexdigest()
    key = st.put(digest, b"x")
    assert key == "media/sha256/" + digest and st.get(key) == b"x"
    assert st.exists(key) and not st.exists("media/sha256/nope")


def test_store_for_local_when_no_endpoint(tmp_path):
    assert isinstance(store_for(_cfg(tmp_path)), LocalStore)


def test_config_resolves_relative_paths_against_the_kdb_toml_folder(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    p = _write(project, '[lake]\ncatalog = "lake/catalog.ducklake"\ndata_path = "lake/data/"\nteam = "t"\n'
                        'iri_base = "https://x/y/"\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = load_config(p)
    assert cfg.catalog == project / "lake" / "catalog.ducklake"
    assert cfg.data_path == str(project / "lake" / "data") + "/"


def test_config_keeps_absolute_and_s3_paths(tmp_path):
    p = _write(tmp_path, f'[lake]\ncatalog = "{tmp_path / "c.ducklake"}"\ndata_path = "s3://kdb/lake/"\nteam = "t"\n'
                         'iri_base = "https://x/y/"\ns3_endpoint = "http://localhost:9000"\n')
    cfg = load_config(p)
    assert cfg.catalog == tmp_path / "c.ducklake" and cfg.data_path == "s3://kdb/lake/"


def test_the_compose_file_runs_on_a_laptop_without_setup():
    """Local default credentials, and ports open only on this machine, so the defaults reach no one else.

    The data is a named volume: RustFS runs as a non-root user, and a host folder that Docker creates on
    Linux belongs to root, so RustFS could not write to it (found in the live RustFS check)."""
    import yaml

    from kdb_tools.lake import COMPOSE

    spec = yaml.safe_load(COMPOSE.read_text())
    service = spec["services"]["rustfs"]
    mounts = [v.split(":", 1)[0] for v in service["volumes"]]
    assert mounts and all(m in spec["volumes"] for m in mounts)
    assert all(p.startswith("127.0.0.1:") for p in service["ports"])
    env = service["environment"]
    assert env["RUSTFS_ACCESS_KEY"].startswith("${RUSTFS_ACCESS_KEY:-")
    assert env["RUSTFS_SECRET_KEY"].startswith("${RUSTFS_SECRET_KEY:-")


def test_a_local_lake_still_opens_after_its_project_folder_moves(tmp_path):
    old = tmp_path / "old"
    (old / "lake").mkdir(parents=True)
    _write(old, '[lake]\ncatalog = "lake/meta.ducklake"\ndata_path = "lake/data/"\nteam = "t"\niri_base = "https://x/y/"\n')
    lake = Lake.open(load_config(old / "kdb.toml"))
    lake.con.execute("SET ducklake_default_data_inlining_row_limit = 0")  # write Parquet files, not inlined rows
    lake.con.execute("CREATE TABLE t AS SELECT range AS x FROM range(100)")
    lake.close()
    new = tmp_path / "new"
    old.rename(new)
    lake = Lake.open(load_config(new / "kdb.toml"))
    try:
        assert lake.con.execute("SELECT count(*) FROM t").fetchone()[0] == 100
        lake.con.execute("INSERT INTO t VALUES (100)")
        assert lake.con.execute("SELECT max(x) FROM t").fetchone()[0] == 100
    finally:
        lake.close()
    assert list((new / "lake" / "data").rglob("*.parquet"))


def test_a_lake_path_with_an_apostrophe_opens(tmp_path):
    (tmp_path / "o'brien").mkdir()
    lake = Lake.open(_cfg(tmp_path / "o'brien"))
    try:
        lake.ensure_system_tables()
        lake.con.execute(_media("ab"))
        assert lake.con.execute("SELECT count(*) FROM kdb.media").fetchone()[0] == 1
    finally:
        lake.close()


def test_a_missing_lake_key_is_named(tmp_path):
    p = _write(tmp_path, '[lake]\ncatalog = "m"\nteam = "t"\n')
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert str(e.value) == f"the [lake] table of {p} needs data_path and iri_base"


def test_put_repairs_a_torn_file(tmp_path):
    st = LocalStore(tmp_path)
    data = b"%PDF-1.4 the whole file"
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path / "media" / "sha256" / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(data[:5])  # a write that stopped part way
    st.put(digest, data)
    assert path.read_bytes() == data
    assert [p.name for p in path.parent.iterdir()] == [digest]  # no temp file left behind
