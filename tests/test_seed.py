import hashlib

import yaml
from conftest import make_pdf

from kdb_tools.ground import ground
from kdb_tools.seed import main, seed
from kdb_tools.validate import validate

SENT = "We count pick-and-place as one skill since it is typically performed in one motion."
LIST = "The dataset includes skills such as pick-and-place, pushing, and more."


def spec(tmp_path, quote=SENT):
    s = {
        "domain": "robotics",
        "papers": {"bridge": "2308.12952"},
        "terms": [
            {"slug": "manipulation", "kind": "tasks", "pref_label": "Robot manipulation", "alt_labels": ["manipulation"],
             "scope": [{"text": "Robot manipulation covers skills such as pick-and-place and pushing.",
                        "quote": {"paper": "bridge", "page": 1, "text": LIST}}]},
            {"slug": "pick-and-place", "kind": "tasks", "pref_label": "Pick and place", "alt_labels": ["PnP"],
             "broader": ["manipulation"],
             "related_match": ["http://www.ease-crc.org/ont/SOMA.owl#PickingUp"],
             "scope": [{"text": "Pick-and-place is counted as one skill.",
                        "quote": {"paper": "bridge", "page": 2, "text": quote}}]},
        ],
    }
    path = tmp_path / "seed.yaml"
    path.write_text(yaml.safe_dump(s, sort_keys=False))
    return path


def paper(bundle):
    make_pdf(bundle.root / "papers" / "2308.12952.pdf", [[(72, 700, LIST)], [(72, 500, SENT)]])


def digest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_seeding_writes_term_pages_that_pass_both_checks(bundle, tmp_path):
    paper(bundle)
    report = seed(bundle.root, spec(tmp_path))
    assert [f.rule for f in report.findings] == []
    page = (bundle.root / "ontology/robotics/tasks/pick-and-place.md").read_text()
    fm = yaml.safe_load(page.split("\n---\n", 1)[0][4:])
    assert fm["type"] == "Term" and fm["pref_label"] == "Pick and place"
    assert fm["broader"] == ["/ontology/robotics/tasks/manipulation.md"]
    assert fm["sources"][0]["page"] == 2 and fm["sources"][0]["resource"] == "/papers/2308.12952.pdf"
    assert errors(validate(bundle.root)) == []
    assert [f.rule for f in ground(bundle.root).findings] == []


def test_a_quote_that_is_not_in_the_paper_is_reported_and_its_term_is_not_written(bundle, tmp_path):
    paper(bundle)
    report = seed(bundle.root, spec(tmp_path, quote="A sentence the paper does not contain."))
    assert [f.rule for f in report.findings] == ["seed.quote"]
    assert "pick-and-place" in report.findings[0].message
    assert not (bundle.root / "ontology/robotics/tasks/pick-and-place.md").exists()


def test_a_missing_paper_is_reported(bundle, tmp_path):
    report = seed(bundle.root, spec(tmp_path))
    assert "seed.paper" in [f.rule for f in report.findings]
    assert "papers/2308.12952.pdf" in report.findings[0].message


def test_seeding_twice_changes_nothing(bundle, tmp_path):
    paper(bundle)
    s = spec(tmp_path)
    seed(bundle.root, s)
    before = digest(bundle.root)
    seed(bundle.root, s)
    assert digest(bundle.root) == before


def test_a_seed_that_extends_another_writes_its_ontology_page_and_links_upward(bundle, tmp_path):
    paper(bundle)
    general = {"domain": "datasets", "title": "Datasets", "papers": {"bridge": "2308.12952"}, "terms": [
        {"slug": "human-collected", "kind": "collection", "pref_label": "Human-collected",
         "scope": [{"text": "Human-collected data.", "quote": {"paper": "bridge", "page": 2, "text": SENT}}]}]}
    special = {"domain": "vla-datasets", "title": "VLA datasets", "extends": ["datasets"], "papers": {"bridge": "2308.12952"},
               "terms": [{"slug": "teleoperation", "kind": "collection", "pref_label": "Teleoperation",
                          "broader": ["datasets/collection/human-collected"],
                          "scope": [{"text": "Teleoperated data.", "quote": {"paper": "bridge", "page": 1, "text": LIST}}]}]}
    for name, s in (("g.yaml", general), ("s.yaml", special)):
        (tmp_path / name).write_text(yaml.safe_dump(s, sort_keys=False))
        assert [f.rule for f in seed(bundle.root, tmp_path / name).findings] == []
    onto = yaml.safe_load((bundle.root / "ontology/vla-datasets/ontology.md").read_text().split("\n---\n", 1)[0][4:])
    assert onto["type"] == "Ontology" and onto["extends"] == ["/ontology/datasets/ontology.md"]
    tele = yaml.safe_load((bundle.root / "ontology/vla-datasets/collection/teleoperation.md").read_text().split("\n---\n", 1)[0][4:])
    assert tele["broader"] == ["/ontology/datasets/collection/human-collected.md"]
    assert errors(validate(bundle.root)) == []


def errors(report):
    return sorted({f.rule for f in report.findings if f.level == "error"})


def test_an_arxiv_id_that_yaml_reads_as_a_number_is_reported(bundle, tmp_path):
    path = spec(tmp_path)
    path.write_text(path.read_text().replace("'2308.12952'", "2306.03310"))
    report = seed(bundle.root, path)
    assert [f.rule for f in report.findings] == ["seed.spec"]
    assert "quote" in report.findings[0].message


def fm_of(path):
    return yaml.safe_load(path.read_text().split("\n---\n", 1)[0][4:])


def lake_config(tmp_path):
    """A kdb.toml with a [lake] above the bundle: okf-seed runs only as an action, which needs one."""
    (tmp_path / "kdb.toml").write_text(f'[lake]\ncatalog = "{tmp_path / "meta.ducklake"}"\n'
                                       f'data_path = "{tmp_path / "data"}/"\nteam = "t"\n'
                                       'iri_base = "https://data.aegean.ai/vla/t/"\n')


def test_core_seed_writes_the_type_pages_and_validates(bundle, tmp_path):
    lake_config(tmp_path)
    assert main([str(bundle.root), "--core"]) == 0
    core = bundle.root / "ontology/core"
    assert fm_of(core / "ontology.md")["base_iri"] == "https://kdb.aegean.ai/core/"
    assert sorted(p.stem for p in (core / "properties").iterdir()) == sorted(
        ["title", "description", "aliases", "evidence", "mass", "length", "angle", "media_type", "sha256"])
    assert sorted(p.stem for p in (core / "interfaces").iterdir()) == sorted(
        ["link", "joint", "actuator", "gripper", "robot", "twin", "observation", "media-artifact", "document"])
    assert sorted(p.stem for p in (core / "classes").iterdir()) == sorted(
        ["revolute-joint", "continuous-joint", "prismatic-joint", "fixed-joint", "floating-joint", "planar-joint"])
    mass = fm_of(core / "properties/mass.md")
    assert (mass["iri"], mass["datatype"], mass["unit"]) == ("https://kdb.aegean.ai/core/properties/mass", "double", "unit:KiloGM")
    assert fm_of(core / "properties/title.md")["required"] is True
    joint = fm_of(core / "interfaces/joint.md")
    assert [(l["name"], l["target"], l["cardinality"], l["required"]) for l in joint["links"]] == [
        ("parent", "https://kdb.aegean.ai/core/interfaces/link", "many-to-one", True),
        ("child", "https://kdb.aegean.ai/core/interfaces/link", "many-to-one", True)]
    revolute = fm_of(core / "classes/revolute-joint.md")
    assert revolute["implements"] == ["https://kdb.aegean.ai/core/interfaces/joint"] and "binding" not in revolute
    assert errors(validate(bundle.root)) == []


def test_core_seed_twice_changes_nothing(bundle, tmp_path):
    lake_config(tmp_path)
    assert main([str(bundle.root), "--core"]) == 0
    before = digest(bundle.root)
    assert main([str(bundle.root), "--core"]) == 0
    assert digest(bundle.root) == before


def test_seeded_terms_get_an_iri_from_the_base_iri(bundle, tmp_path):
    paper(bundle)
    path = spec(tmp_path)
    s = yaml.safe_load(path.read_text())
    s["base_iri"] = "https://kdb.aegean.ai/robotics/"
    path.write_text(yaml.safe_dump(s, sort_keys=False))
    assert [f.rule for f in seed(bundle.root, path).findings] == []
    fm = fm_of(bundle.root / "ontology/robotics/tasks/pick-and-place.md")
    assert fm["iri"] == "https://kdb.aegean.ai/robotics/tasks/pick-and-place"
    assert errors(validate(bundle.root)) == []


def test_type_entries_without_a_base_iri_are_reported(bundle, tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump({"domain": "x", "properties": [
        {"slug": "a", "title": "A", "datatype": "string", "scope": "Text."}]}))
    assert [f.rule for f in seed(bundle.root, path).findings] == ["seed.spec"]
