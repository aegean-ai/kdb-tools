"""Units and class constraints: the unit table, check_value and the onto.constraint rule."""

import pytest
from conftest import domain, errors, klass, prop

from kdb_tools import units as U
from kdb_tools.formula import FormulaError, evaluate
from kdb_tools.validate import validate

CORE = "https://kdb.aegean.ai/core/"
LEN = CORE + "properties/length"


def test_to_si_and_kind():
    assert U.to_si(305, "unit:MilliM") == pytest.approx(0.305)
    assert U.kind("unit:RPM") is None
    assert U.kind("unit:N-CentiM") == "torque"
    assert len(U.UNITS) == 29


def test_check_value_tolerance_and_bounds():
    c = {"nominal": 0.305, "tolerance": 0.0005, "unit": "unit:M"}
    assert U.check_value(c, 305.4, "unit:MilliM") is True
    assert U.check_value(c, 305.6, "unit:MilliM") is False
    assert U.check_value(c, 305.0, "unit:N") is False
    b = {"bounds": [300, 310], "unit": "unit:MilliM"}
    assert U.check_value(b, 0.31, "unit:M") is True
    assert U.check_value(b, 0.3101, "unit:M") is False


def good(**over):
    c = {"property": LEN, "nominal": 0.305, "tolerance": 0.0005, "unit": "unit:M",
         "evidence": ["sha256:aa"], "tolerance_evidence": ["act-1"], "version": 1}
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


def build(bundle, constraints, slug="rod"):
    domain(bundle, "core", CORE)
    prop(bundle, "core", "title", "string")
    prop(bundle, "core", "length", "double", unit="unit:M")
    prop(bundle, "core", "mass", "double", unit="unit:KiloGM")
    klass(bundle, "core", slug, table="rods", columns={"title": "title", "length_m": "length"}, constraints=constraints)
    return errors(validate(bundle.root, []))


def test_clean_constraint(bundle):
    assert build(bundle, [good(), good(tolerance=None, bounds=[300, 310], version=2, unit="unit:MilliM", nominal=305)]) == []


def test_property_not_in_class(bundle):
    assert build(bundle, [good(property=CORE + "properties/mass", unit="unit:KiloGM")]) == ["onto.constraint"]


def test_unknown_property(bundle):
    assert build(bundle, [good(property="https://x/none")]) == ["onto.constraint"]


def test_inherited_property_is_allowed(bundle):
    domain(bundle, "core", CORE)
    prop(bundle, "core", "title", "string")
    prop(bundle, "core", "length", "double", unit="unit:M")
    klass(bundle, "core", "base", table="bases", columns={"title": "title", "length_m": "length"})
    klass(bundle, "core", "rod", subclass_of=["base"], constraints=[good()])
    assert errors(validate(bundle.root, [])) == []


@pytest.mark.parametrize("bad", [
    {"unit": "unit:KiloGM"}, {"unit": "unit:RPM"}, {"unit": None},
    {"bounds": [0.3, 0.31]}, {"tolerance": None}, {"tolerance": -0.1},
    {"tolerance": None, "bounds": [0.31, 0.3]}, {"tolerance": None, "bounds": [0.3]},
    {"tolerance": None, "bounds": [0.3, "x"]},
    {"evidence": []}, {"evidence": [""]}, {"evidence": "sha"}, {"evidence": None},
    {"tolerance_evidence": []}, {"tolerance_evidence": [3]},
    {"version": 0}, {"version": "1"}, {"version": True}, {"version": 1.5}, {"version": None},
    {"nominal": None}, {"nominal": "x"}, {"tolerance": "x"},
    {"property": ["a"]}, {"unit": ["unit:M"]}, {"evidence": [["a"]]},
])
def test_bad_constraint(bundle, bad):
    assert build(bundle, [good(**bad)]) == ["onto.constraint"]


def test_constraint_not_a_mapping(bundle):
    assert build(bundle, ["x"]) == ["onto.constraint"]


def test_constraints_not_a_list(bundle):
    assert build(bundle, {"a": 1}) == ["onto.constraint"]


def test_formula_within_tolerance(bundle):
    ok = good(nominal=None, formula="/ontology/core/classes/rod.md#length * 2", property=LEN)
    assert build(bundle, [good(), ok]) == []


@pytest.mark.parametrize("f", ["1 +", "/ontology/core/classes/none.md#length", "/ontology/core/classes/rod.md#nope",
                               "__import__('os')", "1 / 0", 7, ["a"]])
def test_formula_must_evaluate(bundle, f):
    assert build(bundle, [good(), good(nominal=None, formula=f)]) == ["onto.constraint"]


def test_formula_stated_value_outside_tolerance(bundle):
    c = good(formula="/ontology/core/classes/rod.md#length * 2", nominal=0.5)
    assert build(bundle, [good(), c]) == ["onto.constraint"]


def test_formula_evaluate_api(bundle):
    from kdb_tools import bundle as B
    build(bundle, [good()])
    b = B.load(bundle.root)
    assert evaluate(b, "2 * (3 + 1) ** 2", lambda p, n: 0) == 32
    assert evaluate(b, "/ontology/core/classes/rod.md#x + 1", lambda p, n: 4) == 5
    with pytest.raises(FormulaError):
        evaluate(b, "1 +", lambda p, n: 0)


def test_formula_matching_stated_nominal_is_clean(bundle):
    c = good(formula="/ontology/core/classes/rod.md#length * 2", nominal=0.61)
    assert build(bundle, [good(), c]) == []


def test_property_unit_must_be_in_the_table(bundle):
    domain(bundle, "core", CORE)
    prop(bundle, "core", "rate", "double", unit="unit:RPM")
    assert errors(validate(bundle.root, [])) == ["onto.property"]


@pytest.mark.parametrize("bounds,expected", [([0.6, 0.62], []), ([0.62, 0.6], ["onto.constraint"]),
                                              ([0.6], ["onto.constraint"]), ("x", ["onto.constraint"])])
def test_bounds_with_formula_reference_does_not_crash(bundle, bounds, expected):
    c = good(nominal=None, tolerance=None, bounds=bounds, formula="/ontology/core/classes/rod.md#length * 2")
    assert build(bundle, [good(), c]) == expected


@pytest.mark.parametrize("f", ["1e308 * 10", "1e308 * 10 - 1e308 * 10", "(-1) ** 0.5"])
def test_a_formula_that_overflows_to_infinity_or_nan_is_an_error(bundle, f):
    from kdb_tools import bundle as B
    b = B.load(bundle.root)
    with pytest.raises(FormulaError, match="finite real number"):
        evaluate(b, f, lambda p, n: 0)
