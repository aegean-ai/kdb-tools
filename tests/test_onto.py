"""The type layer: IRIs, Class, Interface and Property pages, and table bindings."""

from conftest import domain, errors, interface, klass, ontology, prop, term

from kdb_tools import onto as O
from kdb_tools import bundle as B
from kdb_tools.validate import validate

CORE = "https://kdb.aegean.ai/core/"


def core(bundle):
    domain(bundle, "core", CORE)
    prop(bundle, "core", "title", "string")
    prop(bundle, "core", "length", "double", unit="unit:M")


def check(bundle):
    return errors(validate(bundle.root, []))


def rule_paths(bundle, rule):
    return sorted(f.path for f in validate(bundle.root, []).findings if f.rule == rule and f.level == "error")


def test_clean_core_and_class(bundle):
    core(bundle)
    klass(bundle, "core", "rod", table="rods", columns={"title": "title", "length_m": "length"})
    assert check(bundle) == []
    assert not [f for f in validate(bundle.root, []).findings if f.level == "warning"]


def test_iri_outside_base(bundle):
    core(bundle)
    klass(bundle, "core", "rod", iri="https://example.org/x")
    assert rule_paths(bundle, "onto.iri") == ["ontology/core/classes/rod.md"]


def test_iri_missing(bundle):
    core(bundle)
    bundle.add("ontology/core/classes/rod.md", {"type": "Class", "title": "Rod", "status": "active",
                                                 "generated": {"by": "t", "at": "2026-11-02T18:00:00Z"}}, body="Scope.\n")
    assert "onto.iri" in check(bundle)


def test_iri_used_twice(bundle):
    core(bundle)
    klass(bundle, "core", "rod")
    klass(bundle, "core", "bar", iri=CORE + "classes/rod")
    assert rule_paths(bundle, "onto.iri") == ["ontology/core/classes/bar.md", "ontology/core/classes/rod.md"]


def test_domain_without_base_iri(bundle):
    ontology(bundle, "core")
    klass(bundle, "core", "rod", iri=CORE + "classes/rod")
    assert "onto.iri" in check(bundle)


def test_subclass_cycle(bundle):
    core(bundle)
    klass(bundle, "core", "a", subclass_of=["b"])
    klass(bundle, "core", "b", subclass_of=["a"])
    assert rule_paths(bundle, "onto.class") == ["ontology/core/classes/a.md", "ontology/core/classes/b.md"]


def test_unknown_subclass_of_and_implements(bundle):
    core(bundle)
    klass(bundle, "core", "a", subclass_of=["nope"], implements=["nada"])
    assert check(bundle) == ["onto.class"]


def test_bad_status(bundle):
    core(bundle)
    klass(bundle, "core", "a", status="draft")
    assert check(bundle) == ["onto.class"]


def test_deprecated_block(bundle):
    core(bundle)
    dep = {"reason": "old", "deadline": "2027-01-31", "replacement": CORE + "classes/b"}
    klass(bundle, "core", "b")
    klass(bundle, "core", "a", status="deprecated", deprecated=dep)
    assert check(bundle) == []
    klass(bundle, "core", "c", status="deprecated")
    klass(bundle, "core", "d", status="active", deprecated=dep)
    assert rule_paths(bundle, "onto.class") == ["ontology/core/classes/c.md", "ontology/core/classes/d.md"]


def test_unit_on_string(bundle):
    core(bundle)
    prop(bundle, "core", "name", "string", unit="unit:M")
    assert check(bundle) == ["onto.property"]


def test_unknown_datatype_and_unit(bundle):
    core(bundle)
    prop(bundle, "core", "a", "float")
    prop(bundle, "core", "b", "double", unit="metre")
    assert rule_paths(bundle, "onto.property") == ["ontology/core/properties/a.md", "ontology/core/properties/b.md"]


def test_every_datatype_is_accepted(bundle):
    core(bundle)
    for dt in O.DATATYPES:
        prop(bundle, "core", f"p-{dt}".replace("_", "-"), dt)
    assert check(bundle) == []


def test_column_to_unknown_property(bundle):
    core(bundle)
    klass(bundle, "core", "rod", table="rods", columns={"title": "title", "x": CORE + "properties/nope"})
    assert check(bundle) == ["onto.binding"]


def test_key_and_title_columns(bundle):
    core(bundle)
    klass(bundle, "core", "rod", table="rods", columns={"length_m": "length"}, key="id", title="title")
    assert [f.message for f in validate(bundle.root, []).findings if f.rule == "onto.binding"] == [
        "key column 'id' is not in columns", "title column 'title' is not in columns"]


def test_implicit_columns_not_mapped(bundle):
    core(bundle)
    klass(bundle, "core", "rod", table="rods", columns={"title": "title", "team": "title"})
    assert check(bundle) == ["onto.binding"]


def test_two_classes_one_table(bundle):
    core(bundle)
    klass(bundle, "core", "a", table="rods", columns={"title": "title"})
    klass(bundle, "core", "b", table="rods", columns={"title": "title"})
    assert rule_paths(bundle, "onto.binding") == ["ontology/core/classes/b.md"]


def test_interface_property_unmapped(bundle):
    core(bundle)
    prop(bundle, "core", "axis", "string")
    interface(bundle, "core", "joint", properties=[("axis", True), ("length", False)])
    klass(bundle, "core", "hinge", table="hinges", columns={"title": "title"}, implements=["joint"])
    assert check(bundle) == ["onto.binding"]
    klass(bundle, "core", "hinge", table="hinges", columns={"title": "title", "axis": "axis"}, implements=["joint"])
    assert check(bundle) == []


def test_interface_property_mapped_by_superclass(bundle):
    core(bundle)
    prop(bundle, "core", "axis", "string")
    interface(bundle, "core", "joint", properties=[("axis", True)])
    klass(bundle, "core", "base", table="bases", columns={"title": "title", "axis": "axis"})
    klass(bundle, "core", "hinge", table="hinges", columns={"title": "title"}, implements=["joint"], subclass_of=["base"])
    assert check(bundle) == []


def test_link_target(bundle):
    core(bundle)
    klass(bundle, "core", "rod")
    interface(bundle, "core", "joint", links=[{"name": "parent", "target": CORE + "classes/rod", "cardinality": "many-to-one", "required": True}])
    assert check(bundle) == []
    interface(bundle, "core", "bad", links=[{"name": "p", "target": CORE + "classes/nope", "cardinality": "many-to-one", "required": True}])
    assert check(bundle) == ["onto.link"]


def test_type_pages_skip_entity_and_uncited(bundle):
    core(bundle)
    prop(bundle, "core", "rod", "string", title="Rod")
    klass(bundle, "core", "rod", title="Rod")
    findings = validate(bundle.root, []).findings
    assert not [f for f in findings if f.rule in ("project.entity", "project.uncited")]


def test_term_iri_checked_when_present(bundle):
    domain(bundle, "robotics", "https://kdb.aegean.ai/robotics/")
    path, fm = term("Grasping", iri="https://example.org/grasping")
    bundle.add(path, fm)
    assert "onto.iri" in check(bundle)


def test_load_ontology_and_property_columns(bundle):
    core(bundle)
    prop(bundle, "core", "axis", "string")
    interface(bundle, "core", "joint", properties=[("axis", True)])
    klass(bundle, "core", "base", table="bases", columns={"title": "title"})
    klass(bundle, "core", "hinge", table="hinges", columns={"len": "length"}, implements=["joint"], subclass_of=["base"])
    o = O.load_ontology(B.load(bundle.root))
    assert set(o.classes) == {CORE + "classes/base", CORE + "classes/hinge"}
    assert o.props[CORE + "properties/length"].unit == "unit:M"
    cols = O.property_columns(o, o.classes[CORE + "classes/hinge"])
    assert set(cols) == {"len", "title", "axis"}
    b = B.load(bundle.root)
    assert O.load_ontology(b) is O.load_ontology(b)


def test_malformed_values_are_findings_not_crashes(bundle):
    core(bundle)
    klass(bundle, "core", "a", iri=["x"])
    klass(bundle, "core", "b", subclass_of=[["x"]], implements=[{"k": 1}])
    interface(bundle, "core", "j1", links=[{"name": "p", "target": ["x"]}])
    interface(bundle, "core", "j2")
    bundle.add("ontology/core/interfaces/j3.md", {"type": "Interface", "title": "J3", "iri": CORE + "interfaces/j3",
               "properties": [{"property": ["x"]}, 5], "links": 7,
               "generated": {"by": "t", "at": "2026-11-02T18:00:00Z"}}, body="Scope.\n")
    bundle.add("ontology/core/interfaces/j4.md", {"type": "Interface", "title": "J4", "iri": CORE + "interfaces/j4",
               "properties": 3, "generated": {"by": "t", "at": "2026-11-02T18:00:00Z"}}, body="Scope.\n")
    rules_seen = {(f.rule, f.path) for f in validate(bundle.root, []).findings if f.level == "error"}
    assert ("onto.iri", "ontology/core/classes/a.md") in rules_seen
    assert ("onto.class", "ontology/core/classes/b.md") in rules_seen
    assert ("onto.link", "ontology/core/interfaces/j1.md") in rules_seen
    assert ("onto.property", "ontology/core/interfaces/j3.md") in rules_seen
