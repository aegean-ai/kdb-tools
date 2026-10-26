"""A project adds its own rules and seed specs to the core through a profile."""

from importlib import metadata

import yaml
from conftest import concept

from kdb_tools import profile as P
from kdb_tools.seed import seed
from kdb_tools.validate import main, validate


def flag_models(b, page, report):
    if (page.frontmatter or {}).get("type") == "Model":
        report.error("test.model", page.rel, "a test rule fired")


def test_a_profile_rule_runs_on_every_page(bundle):
    bundle.add("models/openvla.md", concept())
    report = validate(bundle.root, profiles=[P.Profile("test", check_page=flag_models)])
    assert [(f.rule, f.path) for f in report.findings] == [("test.model", "models/openvla.md")]


def test_without_a_profile_only_the_core_rules_run(bundle):
    bundle.add("results/openvla-libero.md", concept(type="Result", title="OpenVLA on LIBERO"))
    assert validate(bundle.root, profiles=[]).findings == []


def test_installed_profiles_come_from_entry_points(monkeypatch):
    prof = P.Profile("fake")

    class EP:
        name = "fake"

        def load(self):
            return prof

    monkeypatch.setattr(metadata, "entry_points", lambda group: [EP()] if group == P.GROUP else [])
    assert P.installed() == [prof]
    assert P.installed("fake") == [prof]
    assert P.installed("other") == []


def test_validate_main_uses_the_installed_profiles(bundle, monkeypatch, capsys):
    monkeypatch.setattr(P, "installed", lambda name=None: [P.Profile("test", check_page=flag_models)])
    bundle.add("models/openvla.md", concept())
    assert main([str(bundle.root)]) == 1
    assert "test.model" in capsys.readouterr().out


def test_seed_writes_the_specs_of_the_given_profiles(bundle, tmp_path):
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    (seeds / "tiny.yaml").write_text(yaml.safe_dump({"domain": "tiny", "title": "Tiny", "papers": {}, "terms": []}))
    report = seed(bundle.root, profiles=[P.Profile("test", seeds=seeds)])
    assert report.findings == [] and (bundle.root / "ontology/tiny/ontology.md").exists()


def _lake(tmp_path):
    from kdb_tools.lake import Lake, LakeConfig

    cfg = LakeConfig(catalog=tmp_path / "meta.ducklake", data_path=str(tmp_path / "data") + "/", team="team-1",
                     iri_base="https://data.aegean.ai/vla/team-1/")
    lake = Lake.open(cfg)
    lake.ensure_system_tables()
    return cfg, lake


def test_row_rules_run_only_with_a_lake(bundle, tmp_path):
    from kdb_tools.onto import Ontology

    calls = []

    def rows(ontology, lake, report):
        calls.append((ontology, lake))
        report.error("test.row", "results/https://data.aegean.ai/vla/team-1/r1", "a row rule fired")

    prof = P.Profile("test", check_rows=rows)
    assert validate(bundle.root, profiles=[prof]).findings == [] and calls == []
    cfg, lake = _lake(tmp_path)
    try:
        report = validate(bundle.root, profiles=[prof], lake=lake)
    finally:
        lake.close()
    assert [(f.rule, f.path) for f in report.findings if f.rule == "test.row"] == \
        [("test.row", "results/https://data.aegean.ai/vla/team-1/r1")]
    assert isinstance(calls[0][0], Ontology) and calls[0][1] is lake


def test_a_lake_knows_its_config(tmp_path):
    cfg, lake = _lake(tmp_path)
    try:
        assert lake.config is cfg
    finally:
        lake.close()


def test_the_ontology_carries_the_extends_graph(bundle):
    from conftest import ontology

    from kdb_tools import bundle as B
    from kdb_tools.onto import load_ontology

    ontology(bundle, "datasets")
    ontology(bundle, "vla-datasets", extends=["datasets"])
    o = load_ontology(B.load(bundle.root))
    assert o.ancestors == {"datasets": set(), "vla-datasets": {"datasets"}}
