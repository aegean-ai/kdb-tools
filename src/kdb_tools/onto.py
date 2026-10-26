"""The type layer: Class, Interface and Property pages with IRIs and table bindings.

A domain's Ontology page declares a base_iri. Every Class, Interface and Property page under
ontology/<domain>/ has an iri that starts with it. A Class may bind to a lake table; its
binding maps column names to Property IRIs. okf-validate checks all of it with the onto.* rules.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from . import bundle as B
from . import units as U
from .formula import FormulaError, evaluate, is_number
from .report import Report

DATATYPES = (
    "string", "integer", "double", "boolean", "date", "timestamp", "iri",
    "iri_list", "string_list", "media_ref_list", "json",
)
DUCKDB_TYPES = dict(zip(DATATYPES, (
    "VARCHAR", "BIGINT", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMPTZ", "VARCHAR",
    "VARCHAR[]", "VARCHAR[]", "JSON", "JSON",
)))  # the lake column type of each datatype
NUMERIC = ("integer", "double")
STATUSES = ("experimental", "active", "deprecated")
IMPLICIT_COLUMNS = ("iri", "type", "team")
DEPRECATED_KEYS = ("reason", "deadline", "replacement")
TYPES = ("Class", "Interface", "Property")


@dataclass
class Prop:
    iri: str
    datatype: str
    unit: str | None
    required: bool
    rel: str


@dataclass
class Binding:
    table: str
    key: str
    title: str
    columns: dict[str, str]


@dataclass
class Klass:
    iri: str
    rel: str
    status: str
    subclass_of: list[str]
    implements: list[str]
    binding: Binding | None
    constraints: list[dict] = field(default_factory=list)


@dataclass
class Iface:
    iri: str
    rel: str
    properties: list[tuple[str, bool]]
    links: list[dict]


@dataclass
class Ontology:
    classes: dict[str, Klass] = field(default_factory=dict)
    interfaces: dict[str, Iface] = field(default_factory=dict)
    props: dict[str, Prop] = field(default_factory=dict)
    terms: dict[str, str] = field(default_factory=dict)  # Term IRI -> page rel
    ancestors: dict[str, set[str]] = field(default_factory=dict)  # domain -> the domains it extends


def _strings(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _binding(value) -> Binding | None:
    if not isinstance(value, dict):
        return None
    cols = value.get("columns")
    cols = {str(k): v for k, v in cols.items() if isinstance(v, str)} if isinstance(cols, dict) else {}
    return Binding(str(value.get("table", "")), str(value.get("key", "iri")), str(value.get("title", "title")), cols)


def load_ontology(b: B.Bundle, report: Report | None = None) -> Ontology:
    """Parse the type pages leniently; computed once per bundle. check_ontology reports the faults.

    The first page to use an IRI wins, so a duplicate does not replace the original."""
    found = getattr(b, "_ontology", None)
    if found is None:
        found = b._ontology = _parse(b)
    return found


def _parse(b: B.Bundle) -> Ontology:
    from .validate import ancestors
    o = Ontology(ancestors=ancestors(b))
    for page in sorted(b.concepts, key=lambda p: p.rel):
        fm = page.frontmatter or {}
        kind, iri = fm.get("type"), fm.get("iri")
        if not isinstance(iri, str):
            continue
        if kind == "Term":
            o.terms.setdefault(iri, page.rel)
        elif kind == "Property" and iri not in o.props:
            unit = fm.get("unit")
            o.props[iri] = Prop(iri, str(fm.get("datatype", "")), unit if isinstance(unit, str) else None,
                                fm.get("required") is True, page.rel)
        elif kind == "Class" and iri not in o.classes:
            constraints = fm.get("constraints")
            o.classes[iri] = Klass(iri, page.rel, str(fm.get("status", "")), _strings(fm.get("subclass_of")),
                                   _strings(fm.get("implements")), _binding(fm.get("binding")),
                                   [c for c in constraints if isinstance(c, dict)] if isinstance(constraints, list) else [])
        elif kind == "Interface" and iri not in o.interfaces:
            entries = fm.get("properties")
            props = [(p["property"], p.get("required") is True) for p in entries if isinstance(p, dict) and isinstance(p.get("property"), str)] \
                if isinstance(entries, list) else []
            links = fm.get("links")
            o.interfaces[iri] = Iface(iri, page.rel, props, [x for x in links if isinstance(x, dict)] if isinstance(links, list) else [])
    return o


def supers(o: Ontology, k: Klass) -> list[Klass]:
    """The class's superclasses, nearest first, each once; a cycle does not loop."""
    out, seen, stack = [], {k.iri}, list(k.subclass_of)
    while stack:
        iri = stack.pop(0)
        parent = o.classes.get(iri)
        if parent is not None and iri not in seen:
            seen.add(iri)
            out.append(parent)
            stack.extend(parent.subclass_of)
    return out


def property_columns(o: Ontology, k: Klass) -> dict[str, Prop]:
    """The class's columns with the properties it inherits from subclass_of and implements.

    A bound property is keyed by its column name, the class's own binding first. An inherited
    property with no column on this class is keyed by the last segment of its IRI."""
    out: dict[str, Prop] = {}
    mapped = set()
    for c in [k] + supers(o, k):
        if c.binding:
            for col, iri in c.binding.columns.items():
                if col not in out and iri in o.props:
                    out[col] = o.props[iri]
                    mapped.add(iri)
    for c in [k] + supers(o, k):
        for i_iri in c.implements:
            iface = o.interfaces.get(i_iri)
            for p_iri, _ in iface.properties if iface else []:
                if p_iri in o.props and p_iri not in mapped:
                    out.setdefault(p_iri.rsplit("/", 1)[-1], o.props[p_iri])
                    mapped.add(p_iri)
    return out


def _bases(b: B.Bundle) -> dict[str, str | None]:
    """Each declared domain mapped to its base_iri, or None when the Ontology page has none."""
    from .validate import term_domain
    out: dict[str, str | None] = {}
    for page in b.concepts:
        fm = page.frontmatter or {}
        domain = term_domain(page.rel)
        if fm.get("type") == "Ontology" and domain is not None:
            base = fm.get("base_iri")
            out[domain] = base if isinstance(base, str) else None
    return out


def _check_iris(b: B.Bundle, bases: dict[str, str | None], report: Report) -> None:
    from .validate import term_domain
    for domain, base in sorted(bases.items()):
        if base is not None and not base.endswith("/"):
            report.error("onto.iri", f"ontology/{domain}/ontology.md", f"base_iri '{base}' must end with /")
    users: dict[str, list[str]] = defaultdict(list)
    for page in b.concepts:
        fm = page.frontmatter or {}
        if fm.get("type") not in TYPES + ("Term",):
            continue
        iri, is_type = fm.get("iri"), fm.get("type") in TYPES
        if not isinstance(iri, str) or not iri:
            if is_type:
                report.error("onto.iri", page.rel, "a Class, Interface or Property page needs an iri")
            continue
        users[iri].append(page.rel)
        domain = term_domain(page.rel)
        base = bases.get(domain)
        if base is None:
            if is_type:
                report.error("onto.iri", page.rel, f"the {domain} ontology has no base_iri, so this iri cannot be checked")
        elif not iri.startswith(base):
            report.error("onto.iri", page.rel, f"iri '{iri}' does not start with the {domain} base_iri '{base}'")
    for iri, rels in sorted(users.items()):
        if len(rels) > 1:
            for rel in rels:
                report.error("onto.iri", rel, f"iri '{iri}' is also used by {', '.join(r for r in rels if r != rel)}")


def _check_properties(o: Ontology, b: B.Bundle, report: Report) -> None:
    for page in b.concepts:
        fm = page.frontmatter or {}
        if fm.get("type") != "Property":
            continue
        datatype, unit = fm.get("datatype"), fm.get("unit")
        if datatype not in DATATYPES:
            report.error("onto.property", page.rel, f"datatype '{datatype}' is not one of {', '.join(DATATYPES)}")
        if unit is not None:
            if datatype not in NUMERIC:
                report.error("onto.property", page.rel, f"a unit needs a numeric datatype, not '{datatype}'")
            elif not (isinstance(unit, str) and unit in U.UNITS):
                report.error("onto.property", page.rel, f"unit '{unit}' is not in the unit table, for example unit:M")


def _check_classes(o: Ontology, b: B.Bundle, report: Report) -> None:
    for page in b.concepts:
        fm = page.frontmatter or {}
        if fm.get("type") != "Class":
            continue
        k = o.classes.get(fm["iri"]) if isinstance(fm.get("iri"), str) else None
        status = fm.get("status")
        if status not in STATUSES:
            report.error("onto.class", page.rel, f"status '{status}' is not one of {', '.join(STATUSES)}")
        dep = fm.get("deprecated")
        if status == "deprecated":
            if not isinstance(dep, dict) or any(not dep.get(key) for key in DEPRECATED_KEYS):
                report.error("onto.class", page.rel, "a deprecated class needs a deprecated block with reason, deadline and replacement")
        elif dep is not None:
            report.error("onto.class", page.rel, "a deprecated block belongs only on a class whose status is deprecated")
        for field_name, known in (("subclass_of", o.classes), ("implements", o.interfaces)):
            value = fm.get(field_name, [])
            if not isinstance(value, list):
                report.error("onto.class", page.rel, f"{field_name} is a list of IRIs")
                continue
            for iri in value:
                if not isinstance(iri, str):
                    report.error("onto.class", page.rel, f"{field_name} holds '{iri}', which is not an IRI string")
                elif iri not in known:
                    report.error("onto.class", page.rel, f"{field_name} '{iri}' is not a {'class' if field_name == 'subclass_of' else 'interface'} in this bundle")
        if k is not None and k.rel == page.rel and any(k.iri in parent.subclass_of for parent in [k] + supers(o, k)):
            report.error("onto.class", page.rel, "subclass_of leads back to this class")


def _check_bindings(o: Ontology, report: Report) -> None:
    tables: dict[str, str] = {}
    for k in sorted(o.classes.values(), key=lambda c: c.rel):
        bind = k.binding
        if bind is None:
            continue
        if not bind.table:
            report.error("onto.binding", k.rel, "binding needs a table")
        elif bind.table in tables:
            report.error("onto.binding", k.rel, f"table '{bind.table}' is already bound by {tables[bind.table]}")
        else:
            tables[bind.table] = k.rel
        for col, iri in bind.columns.items():
            if col in IMPLICIT_COLUMNS:
                report.error("onto.binding", k.rel, f"column '{col}' is implicit and is not mapped")
            if iri not in o.props:
                report.error("onto.binding", k.rel, f"column '{col}' maps to '{iri}', which is not a Property")
        if bind.key != "iri" and bind.key not in bind.columns:
            report.error("onto.binding", k.rel, f"key column '{bind.key}' is not in columns")
        if bind.title not in bind.columns:
            report.error("onto.binding", k.rel, f"title column '{bind.title}' is not in columns")
        mapped = {iri for c in [k] + supers(o, k) if c.binding for iri in c.binding.columns.values()}
        for i_iri in k.implements:
            iface = o.interfaces.get(i_iri)
            for p_iri, required in iface.properties if iface else []:
                if required and p_iri not in mapped:
                    report.error("onto.binding", k.rel, f"required property '{p_iri}' of interface '{i_iri}' is not mapped by a column")


def _check_interfaces(o: Ontology, b: B.Bundle, report: Report) -> None:
    for page in b.concepts:
        fm = page.frontmatter or {}
        if fm.get("type") != "Interface":
            continue
        entries, links = fm.get("properties"), fm.get("links")
        for entry in entries if isinstance(entries, list) else []:
            iri = entry.get("property") if isinstance(entry, dict) else None
            if not isinstance(iri, str) or iri not in o.props:
                report.error("onto.property", page.rel, f"interface property '{iri}' is not a Property in this bundle")
        for link in links if isinstance(links, list) else []:
            target = link.get("target") if isinstance(link, dict) else None
            if not isinstance(target, str) or (target not in o.classes and target not in o.interfaces):
                report.error("onto.link", page.rel, f"link target '{target}' is not a class or interface in this bundle")


def _evidence(c: dict, key: str) -> str | None:
    value = c.get(key)
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v.strip() for v in value):
        return f"{key} is a non-empty list of non-empty strings"
    return None


def _number_or_none(c: dict, key: str) -> str | None:
    return None if is_number(c.get(key)) else f"{key} is a number"


def _faults(c: dict, columns: dict[str, Prop], lookup, b: B.Bundle) -> list[str]:
    """Everything wrong with one constraint, as messages."""
    out: list[str] = []
    iri, unit = c.get("property"), c.get("unit")
    prop = next((p for p in columns.values() if p.iri == iri), None) if isinstance(iri, str) else None
    if prop is None:
        out.append(f"property '{iri}' is not a column or inherited property of this class")
    if not (isinstance(unit, str) and unit in U.UNITS):
        out.append(f"unit '{unit}' is not in the unit table")
    elif prop is not None and U.kind(unit) != U.kind(prop.unit):
        out.append(f"unit '{unit}' is a {U.kind(unit)}, but the property's unit is a {U.kind(prop.unit) or 'none'}")
    has_formula = "formula" in c
    if has_formula:
        if not isinstance(c["formula"], str):
            out.append("formula is a string")
    if "nominal" in c or not has_formula:
        out += [m for m in (_number_or_none(c, "nominal"),) if m]
    tol, bounds = "tolerance" in c, "bounds" in c
    ok_shape = False
    if tol == bounds:
        out.append("give exactly one of tolerance and bounds")
    elif tol:
        if not is_number(c["tolerance"]):
            out.append("tolerance is a number")
        elif c["tolerance"] < 0:
            out.append("tolerance is not negative")
        else:
            ok_shape = True
    else:
        bnds = c["bounds"]
        if not (isinstance(bnds, list) and len(bnds) == 2 and all(is_number(x) for x in bnds)):
            out.append("bounds is a list of two numbers")
        elif not bnds[0] < bnds[1]:
            out.append("bounds must increase: lower, then upper")
        else:
            ok_shape = True
    out += [m for m in (_evidence(c, "evidence"), _evidence(c, "tolerance_evidence")) if m]
    v = c.get("version")
    if not (isinstance(v, int) and not isinstance(v, bool) and v > 0):
        out.append("version is a positive integer")
    if has_formula and isinstance(c["formula"], str):
        try:
            value = evaluate(b, c["formula"], lookup)
        except FormulaError as exc:
            out.append(f"formula: {exc}")
        else:
            if ok_shape and not out and not U.check_value(
                    {**c, "nominal": c["nominal"] if is_number(c.get("nominal")) else value}, value, unit):
                out.append(f"formula gives {value:g} {unit}, outside the stated tolerance")
    return out


def _lookup(o: Ontology):
    """The formula lookup: a named constraint's nominal on another Class page."""
    def lookup(rel: str, name: str) -> float:
        k = next((k for k in o.classes.values() if k.rel == rel), None)
        if k is None:
            raise FormulaError(f"/{rel} is not a Class page")
        for c in k.constraints:
            p = c.get("property")
            if isinstance(p, str) and p.rsplit("/", 1)[-1] == name and is_number(c.get("nominal")):
                return float(c["nominal"])
        raise FormulaError(f"/{rel} has no constraint {name} with a number nominal")
    return lookup


def constraint_faults(b: B.Bundle, k: Klass, c: dict) -> list[str]:
    """Everything wrong with constraint c on class k, as the onto.constraint rule words it."""
    o = load_ontology(b)
    return _faults(c, property_columns(o, k), _lookup(o), b)


def _check_constraints(o: Ontology, b: B.Bundle, report: Report) -> None:
    for page in b.concepts:
        fm = page.frontmatter or {}
        if fm.get("type") != "Class" or "constraints" not in fm:
            continue
        entries = fm["constraints"]
        if not isinstance(entries, list):
            report.error("onto.constraint", page.rel, "constraints is a list of mappings")
            continue
        k = o.classes.get(fm["iri"]) if isinstance(fm.get("iri"), str) else None
        columns = property_columns(o, k) if k is not None and k.rel == page.rel else {}
        for i, c in enumerate(entries, 1):
            if not isinstance(c, dict):
                report.error("onto.constraint", page.rel, f"constraint {i} is a mapping")
                continue
            for msg in _faults(c, columns, _lookup(o), b):
                report.error("onto.constraint", page.rel, f"constraint {i}: {msg}")


def check_ontology(b: B.Bundle, report: Report) -> None:
    o = load_ontology(b)
    _check_iris(b, _bases(b), report)
    _check_properties(o, b, report)
    _check_classes(o, b, report)
    _check_bindings(o, report)
    _check_interfaces(o, b, report)
    _check_constraints(o, b, report)
