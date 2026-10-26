"""The type actions: every change to a kDB's Class, Interface and Property pages goes through them.

Each action edits pages under ontology/<domain>/ and appends one line to log.md under the heading
of its own day (ctx.now in UTC). Only BindTable changes the lake: it creates or alters the class's
bound table. RevertAction undoes an earlier action by the same actor while nothing later has
touched what it edited. Governance follows design section 7.5: an active class keeps its IRI, key
and table; deprecate it and propose a replacement instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timezone
from typing import Literal

import yaml
from pydantic import BaseModel

from .. import bundle as B
from .. import onto as O
from .. import seed as S
from .. import units as U
from ..report import Report
from . import adopt  # noqa: F401  registers AdoptBundle, which RevertAction looks up
from .base import REGISTRY, ActionType, Failed, PageEdit, RowEdit, _ident, load_row_edits, register

LOG = "log.md"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BINDING_KEYS = ("table", "key", "title", "columns")


def active_message(iri: str) -> str:
    return f"{iri} is active; deprecate it and propose a replacement"


# --- pages ---

def render(fm: dict, body: str) -> str:
    """A page from frontmatter and the body split_frontmatter returned (it starts after the closing ---)."""
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n" + body


def read_page(ctx, rel: str) -> tuple[str, dict, str]:
    """A page's text, frontmatter and body; Failed when it is missing or its frontmatter is broken."""
    text = ctx.read(rel)
    if text is None:
        raise Failed([f"{rel} does not exist"])
    try:
        fm, body = B.split_frontmatter(text)
    except B.FrontmatterError as e:
        raise Failed([f"{rel}: {e}"])
    if fm is None:
        raise Failed([f"{rel} has no frontmatter"])
    return text, fm, body


def stamped(ctx, fm: dict) -> dict:
    fm["generated"] = {"by": ctx.actor, "at": S.stamp(ctx.now)}
    return fm


def edit_page(ctx, rel: str, change) -> PageEdit:
    """The edit that applies change(frontmatter) to a page and restamps it."""
    text, fm, body = read_page(ctx, rel)
    change(fm)
    return PageEdit(rel, text, render(stamped(ctx, fm), body))


def log_line(ctx, action: ActionType, line: str) -> PageEdit:
    """One line in log.md under the heading of the action's day, in UTC."""
    before = ctx.read(LOG)
    day = ctx.now.astimezone(timezone.utc).date().isoformat()
    return PageEdit(LOG, before, S.log_append(before, day, f"**{action.name}** ({ctx.actor}): {line}"))


def index_entry(ctx, domain: str, entry: str) -> PageEdit:
    rel = f"ontology/{domain}/index.md"
    before = ctx.read(rel)
    title = str(read_page(ctx, f"ontology/{domain}/ontology.md")[1].get("title", domain))
    title = title[: -len(" ontology")] if title.endswith(" ontology") else title
    return PageEdit(rel, before, S.add_index_entry(before, title, entry))


def _frontmatter(text: str | None) -> dict | None:
    try:
        fm = B.split_frontmatter(text)[0] if text is not None else None
    except B.FrontmatterError:
        return None
    return fm if isinstance(fm, dict) else None


def _identity(fm: dict) -> tuple:
    """What an active class keeps: its iri, its status, and the table and key of its binding."""
    b = fm.get("binding") if isinstance(fm.get("binding"), dict) else None
    return fm.get("iri"), fm.get("status"), b and b.get("table"), b and b.get("key", "iri")


def active_guard(now: str | None, then: str | None) -> str | None:
    """The active-class message when replacing page text now with then would delete an active class,
    take its status away, or change its iri, key or table; else None."""
    old = _frontmatter(now)
    if old is None or old.get("type") != "Class" or old.get("status") != "active":
        return None
    new = _frontmatter(then)
    if new is None or new.get("type") != "Class" or _identity(new) != _identity(old):
        return active_message(str(old.get("iri")))
    return None


# --- the ontology ---

def load(ctx) -> tuple[B.Bundle, O.Ontology]:
    b = B.load(ctx.root)
    return b, O.load_ontology(b)


def domain_faults(ctx, domain: str) -> list[str]:
    if not SLUG.fullmatch(domain):
        return [f"domain {domain!r} is not a slug of lowercase letters, digits and hyphens"]
    rel = f"ontology/{domain}/ontology.md"
    return [] if (ctx.root / rel).is_file() else [f"{rel} does not exist; seed the {domain} ontology first"]


def base_iri(ctx, domain: str) -> str | None:
    base = read_page(ctx, f"ontology/{domain}/ontology.md")[1].get("base_iri")
    return base if isinstance(base, str) and base.endswith("/") else None


def new_type_faults(ctx, domain: str, folder: str, slug: str) -> list[str]:
    """Why a new type page ontology/<domain>/<folder>/<slug>.md cannot be made: domain, slug, IRI."""
    if messages := domain_faults(ctx, domain):
        return messages
    if not SLUG.fullmatch(slug):
        return [f"slug {slug!r} is not lowercase letters, digits and hyphens"]
    base = base_iri(ctx, domain)
    if base is None:
        return [f"ontology/{domain}/ontology.md has no base_iri ending in /, so the new IRI cannot be made"]
    _, o = load(ctx)
    iri, rel = f"{base}{folder}/{slug}", f"ontology/{domain}/{folder}/{slug}.md"
    used = set(o.classes) | set(o.props) | set(o.interfaces) | set(o.terms)
    return [m for bad, m in ((iri in used, f"{iri} already exists"), ((ctx.root / rel).exists(), f"{rel} already exists"))
            if bad]


def find_class(ctx, iri: str) -> tuple[B.Bundle, O.Ontology, O.Klass]:
    b, o = load(ctx)
    k = o.classes.get(iri)
    if k is None:
        raise Failed([f"{iri} is not a Class in this bundle"])
    from ..validate import term_domain
    domain = term_domain(k.rel)
    if domain is None or (messages := domain_faults(ctx, domain)):
        raise Failed(messages if domain else [f"{k.rel} is not under ontology/<domain>/"])
    return b, o, k


def parse_binding(raw: dict) -> tuple[dict, list[str]]:
    """A binding as the page stores it, and what is wrong with its shape."""
    out: list[str] = []
    extra = sorted(set(raw) - set(BINDING_KEYS))
    if extra:
        out.append(f"binding has unknown keys: {', '.join(extra)}")
    table, key, title, cols = raw.get("table"), raw.get("key", "iri"), raw.get("title", "title"), raw.get("columns", {})
    if not (isinstance(table, str) and NAME.fullmatch(table)):
        out.append(f"binding table {table!r} is not a table name of letters, digits and underscores")
    for label, value in (("key", key), ("title", title)):
        if not isinstance(value, str):
            out.append(f"binding {label} is a column name")
    if not (isinstance(cols, dict) and all(isinstance(c, str) and isinstance(v, str) for c, v in cols.items())):
        out.append("binding columns maps each column name to a Property IRI")
        cols = {}
    out += [f"column {c!r} is not a column name of letters, digits and underscores" for c in cols if not NAME.fullmatch(c)]
    return {"table": table, "key": key, "title": title, "columns": dict(cols)}, out


def binding_faults(o: O.Ontology, iri: str, binding: dict, implements: list[str], subclass_of: list[str]) -> list[str]:
    """What onto.binding would report for a class with this binding, in its words."""
    out: list[str] = []
    for k in o.classes.values():
        if k.iri != iri and k.binding and k.binding.table == binding["table"]:
            out.append(f"table '{binding['table']}' is already bound by {k.rel}")
    for col, p in binding["columns"].items():
        if col in O.IMPLICIT_COLUMNS:
            out.append(f"column '{col}' is implicit and is not mapped")
        if p not in o.props:
            out.append(f"column '{col}' maps to '{p}', which is not a Property")
    if isinstance(binding["key"], str) and binding["key"] != "iri" and binding["key"] not in binding["columns"]:
        out.append(f"key column '{binding['key']}' is not in columns")
    if isinstance(binding["title"], str) and binding["title"] not in binding["columns"]:
        out.append(f"title column '{binding['title']}' is not in columns")
    parents = [o.classes[s] for s in subclass_of if s in o.classes]
    mapped = set(binding["columns"].values()) | {
        p for c in parents + [x for s in parents for x in O.supers(o, s)] if c.binding for p in c.binding.columns.values()}
    for i_iri in implements:
        iface = o.interfaces.get(i_iri)
        for p_iri, required in iface.properties if iface else []:
            if required and p_iri not in mapped:
                out.append(f"required property '{p_iri}' of interface '{i_iri}' is not mapped by a column")
    return out


def binding_dict(k: O.Klass) -> dict | None:
    b = k.binding
    return None if b is None else {"table": b.table, "key": b.key, "title": b.title, "columns": dict(b.columns)}


# --- ProposeClass and AddProperty ---

class ProposeClassParams(BaseModel):
    domain: str
    slug: str
    title: str
    description: str
    subclass_of: list[str] = []
    implements: list[str] = []
    binding: dict | None = None


@register
class ProposeClass(ActionType[ProposeClassParams]):
    """A new Class page, experimental, listed in the domain's index.md. BindTable makes its table."""

    name = "ProposeClass"
    version = 1
    params = ProposeClassParams

    def preconditions(self, ctx, p):
        if messages := new_type_faults(ctx, p.domain, "classes", p.slug):
            return messages
        _, o = load(ctx)
        out = [f"subclass_of '{s}' is not a class in this bundle" for s in p.subclass_of if s not in o.classes]
        out += [f"implements '{i}' is not an interface in this bundle" for i in p.implements if i not in o.interfaces]
        if p.binding is not None:
            binding, shape = parse_binding(p.binding)
            out += shape + binding_faults(o, self._iri(ctx, p), binding, p.implements, p.subclass_of)
        return out

    def _iri(self, ctx, p) -> str:
        return f"{base_iri(ctx, p.domain)}classes/{p.slug}"

    def edits(self, ctx, p):
        iri, rel = self._iri(ctx, p), f"ontology/{p.domain}/classes/{p.slug}.md"
        fm = {"type": "Class", "title": p.title, "description": p.description, "iri": iri, "status": "experimental",
              "subclass_of": list(p.subclass_of), "implements": list(p.implements)}
        if p.binding is not None:
            fm["binding"] = parse_binding(p.binding)[0]
        page = PageEdit(rel, None, render(stamped(ctx, fm), "\n" + p.description.strip() + "\n"))
        index = index_entry(ctx, p.domain, f"* [{p.title}](classes/{p.slug}.md) - a class")
        return [page, index, log_line(ctx, self, f"proposed class {iri}")], [], []


class AddPropertyParams(BaseModel):
    domain: str
    slug: str
    title: str
    datatype: str
    unit: str | None = None
    required: bool = False
    description: str


@register
class AddProperty(ActionType[AddPropertyParams]):
    """A new Property page, listed in the domain's index.md."""

    name = "AddProperty"
    version = 1
    params = AddPropertyParams

    def preconditions(self, ctx, p):
        if messages := new_type_faults(ctx, p.domain, "properties", p.slug):
            return messages
        if p.datatype not in O.DATATYPES:
            return [f"datatype '{p.datatype}' is not one of {', '.join(O.DATATYPES)}"]
        if p.unit is not None and p.datatype not in O.NUMERIC:
            return [f"a unit needs a numeric datatype, not '{p.datatype}'"]
        if p.unit is not None and p.unit not in U.UNITS:
            return [f"unit '{p.unit}' is not in the unit table, for example unit:M"]
        return []

    def edits(self, ctx, p):
        iri, rel = f"{base_iri(ctx, p.domain)}properties/{p.slug}", f"ontology/{p.domain}/properties/{p.slug}.md"
        fm = {"type": "Property", "title": p.title, "description": p.description, "iri": iri,
              "datatype": p.datatype, "required": p.required}
        if p.unit is not None:
            fm["unit"] = p.unit
        page = PageEdit(rel, None, render(stamped(ctx, fm), "\n" + p.description.strip() + "\n"))
        index = index_entry(ctx, p.domain, f"* [{p.title}](properties/{p.slug}.md) - a property")
        return [page, index, log_line(ctx, self, f"added property {iri}")], [], []


# --- ImplementInterface, AddConstraint, ReviseConstraint ---

class ImplementInterfaceParams(BaseModel):
    class_iri: str
    interface_iri: str
    column_map: dict[str, str]  # column name -> property IRI, as in a binding's columns


@register
class ImplementInterface(ActionType[ImplementInterfaceParams]):
    """The class implements one more interface; column_map adds the columns that carry its properties."""

    name = "ImplementInterface"
    version = 1
    params = ImplementInterfaceParams

    def preconditions(self, ctx, p):
        _, o, k = find_class(ctx, p.class_iri)
        iface = o.interfaces.get(p.interface_iri)
        if iface is None:
            return [f"{p.interface_iri} is not an Interface in this bundle"]
        if p.interface_iri in k.implements:
            return [f"{p.class_iri} already implements {p.interface_iri}"]
        own = {x for x, _ in iface.properties}
        out = [f"column '{c}' maps to '{x}', which is not a property of {p.interface_iri}"
               for c, x in p.column_map.items() if x not in own]
        if k.binding is None:
            return out + ([f"{p.class_iri} has no binding; bind a table with BindTable first"] if p.column_map else [])
        out += [f"column '{c}' already maps to '{k.binding.columns[c]}'"
                for c, x in p.column_map.items() if k.binding.columns.get(c, x) != x]
        if out:
            return out
        binding = self._binding(k, p)
        _, shape = parse_binding(binding)
        return shape or binding_faults(o, k.iri, binding, k.implements + [p.interface_iri], k.subclass_of)

    def _binding(self, k, p) -> dict:
        binding = binding_dict(k)
        binding["columns"] = {**binding["columns"], **p.column_map}
        return binding

    def edits(self, ctx, p):
        _, _, k = find_class(ctx, p.class_iri)

        def change(fm):
            fm["implements"] = list(k.implements) + [p.interface_iri]
            if k.binding is not None:
                fm["binding"] = self._binding(k, p)
        return [edit_page(ctx, k.rel, change), log_line(ctx, self, f"{k.iri} implements {p.interface_iri}")], [], []


def constraints_of(ctx, k: O.Klass) -> list[dict]:
    value = read_page(ctx, k.rel)[1].get("constraints", [])
    if not isinstance(value, list) or not all(isinstance(c, dict) for c in value):
        raise Failed([f"{k.rel}: constraints is not a list of mappings; fix the page first"])
    return value


class AddConstraintParams(BaseModel):
    class_iri: str
    constraint: dict


@register
class AddConstraint(ActionType[AddConstraintParams]):
    """A new constraint on a class, at version 1. A property has at most one constraint per class."""

    name = "AddConstraint"
    version = 1
    params = AddConstraintParams

    def preconditions(self, ctx, p):
        b, _, k = find_class(ctx, p.class_iri)
        prop = p.constraint.get("property")
        if any(c.get("property") == prop for c in constraints_of(ctx, k)):
            return [f"{k.iri} already has a constraint on {prop}; change it with ReviseConstraint"]
        return O.constraint_faults(b, k, {**p.constraint, "version": 1})

    def edits(self, ctx, p):
        _, _, k = find_class(ctx, p.class_iri)
        old = constraints_of(ctx, k)

        def change(fm):
            fm["constraints"] = old + [{**p.constraint, "version": 1}]
        line = f"constrained {p.constraint.get('property')} on {k.iri}"
        return [edit_page(ctx, k.rel, change), log_line(ctx, self, line)], [], []


class ReviseConstraintParams(BaseModel):
    class_iri: str
    property: str
    constraint: dict
    reason: str


@register
class ReviseConstraint(ActionType[ReviseConstraintParams]):
    """A new version of the constraint on one property; the old one moves to the page's superseded list."""

    name = "ReviseConstraint"
    version = 1
    params = ReviseConstraintParams

    def _find(self, ctx, p) -> tuple[O.Klass, list[dict], int]:
        _, _, k = find_class(ctx, p.class_iri)
        old = constraints_of(ctx, k)
        at = next((i for i, c in enumerate(old) if c.get("property") == p.property), None)
        if at is None:
            raise Failed([f"{k.iri} has no constraint on {p.property}; add one with AddConstraint"])
        return k, old, at

    def _new(self, old: dict, p) -> dict:
        v = old.get("version")
        return {**p.constraint, "property": p.property, "version": (v if isinstance(v, int) else 0) + 1}

    def preconditions(self, ctx, p):
        if not p.reason.strip():
            return ["give the reason for the revision"]
        if p.constraint.get("property", p.property) != p.property:
            return [f"the constraint's property {p.constraint['property']} is not {p.property}"]
        k, old, at = self._find(ctx, p)
        b, _ = load(ctx)
        return O.constraint_faults(b, k, self._new(old[at], p))

    def edits(self, ctx, p):
        k, old, at = self._find(ctx, p)
        superseded = read_page(ctx, k.rel)[1].get("superseded", [])

        def change(fm):
            fm["constraints"] = old[:at] + [self._new(old[at], p)] + old[at + 1:]
            fm["superseded"] = (superseded if isinstance(superseded, list) else []) + [{**old[at], "reason": p.reason}]
        line = f"revised the constraint on {p.property} of {k.iri}: {p.reason}"
        return [edit_page(ctx, k.rel, change), log_line(ctx, self, line)], [], []


# --- BindTable ---

def table_columns(ctx, table: str) -> dict[str, str] | None:
    """The bound table's columns and DuckDB types, or None when the table does not exist."""
    rows = ctx.lake.con.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_catalog = 'lake' "
        "AND table_schema = 'main' AND table_name = ? ORDER BY ordinal_position", [table]).fetchall()
    return {c: "TIMESTAMPTZ" if t == "TIMESTAMP WITH TIME ZONE" else t for c, t in rows} or None


def schema_changes(ctx, o: O.Ontology, k: O.Klass, binding: dict, migration: str, move: dict[str, str]):
    """The SQL that makes the lake table match binding, and the messages that block it."""
    out: list[str] = []
    want = {c: "VARCHAR" for c in O.IMPLICIT_COLUMNS}
    for col, p in binding["columns"].items():
        datatype = o.props[p].datatype
        if datatype not in O.DUCKDB_TYPES:
            out.append(f"property '{p}' has datatype '{datatype}', which has no column type")
        want[col] = O.DUCKDB_TYPES.get(datatype, "VARCHAR")
    table, sql = binding["table"], []
    t = _ident(table)
    old_table = k.binding.table if k.binding else None
    have = table_columns(ctx, table)
    source = table
    if old_table and old_table != table and (was := table_columns(ctx, old_table)) is not None:
        if have is not None:
            return [], out + [f"tables {old_table} and {table} both exist; drop one before binding {table}"]
        sql.append(f"ALTER TABLE {_ident(old_table)} RENAME TO {t}")
        have, source = was, old_table
    if have is None:
        sql.append(f"CREATE TABLE IF NOT EXISTS {t} ({', '.join(f'{_ident(c)} {ty}' for c, ty in want.items())})")
        return sql, out
    rows = ctx.lake.con.execute(f"SELECT count(*) FROM {_ident(source)}").fetchone()[0]
    for old, new in move.items():
        if old not in have:
            out.append(f"move: {old} is not a column of {table}")
        elif new not in want or new in have:
            out.append(f"move: {new} must be a new column of the binding")
        elif have[old] != want[new]:
            out.append(f"move keeps the type of {old} ({have[old]}); bind again with migration cast to make it {want[new]}")
        else:
            sql.append(f"ALTER TABLE {t} RENAME COLUMN {_ident(old)} TO {_ident(new)}")
            have[new] = have.pop(old)
    for col in [c for c in have if c not in want]:
        if rows and migration != "drop":
            out.append(f"column {col} of {table} has {rows} rows and is not in the binding; "
                       "give migration drop, or move it to a new column")
        sql.append(f"ALTER TABLE {t} DROP COLUMN {_ident(col)}")
    for col in [c for c in want if c in have and have[c] != want[c]]:
        if rows and migration not in ("cast", "drop"):
            out.append(f"column {col} of {table} has {rows} rows and changes type from {have[col]} to {want[col]}; "
                       "give migration cast (or drop)")
            continue
        if rows and migration == "cast":
            bad = ctx.lake.con.execute(f"SELECT count(*) FROM {_ident(source)} WHERE {_ident(col)} IS NOT NULL "
                                       f"AND TRY_CAST({_ident(col)} AS {want[col]}) IS NULL").fetchone()[0]
            if bad:
                out.append(f"{bad} values of column {col} of {table} do not cast to {want[col]}")
            tmp = _ident(f"__kdb_cast_{col}")
            sql += [f"ALTER TABLE {t} ADD COLUMN {tmp} {want[col]}",
                    f"UPDATE {t} SET {tmp} = CAST({_ident(col)} AS {want[col]})",
                    f"ALTER TABLE {t} DROP COLUMN {_ident(col)}",
                    f"ALTER TABLE {t} RENAME COLUMN {tmp} TO {_ident(col)}"]
        else:
            sql += [f"ALTER TABLE {t} DROP COLUMN {_ident(col)}", f"ALTER TABLE {t} ADD COLUMN {_ident(col)} {want[col]}"]
    sql += [f"ALTER TABLE {t} ADD COLUMN {_ident(c)} {ty}" for c, ty in want.items() if c not in have]
    return sql, out


class BindTableParams(BaseModel):
    class_iri: str
    binding: dict
    migration: Literal["none", "drop", "move", "cast"] = "none"
    move: dict[str, str] = {}


@register
class BindTable(ActionType[BindTableParams]):
    """The class's binding, and the lake table made to match it.

    A change that drops or retypes a column of a table with rows needs a migration: drop loses the
    values, move renames a column (old name to new), cast converts the values in place."""

    name = "BindTable"
    version = 1
    params = BindTableParams

    def _compute(self, ctx, p):
        """The class, its new binding, the schema SQL and the blocking messages; preconditions, statements
        and edits share it."""
        _, o, k = find_class(ctx, p.class_iri)
        binding, shape = parse_binding(p.binding)
        if shape:
            return o, k, binding, [], shape
        if k.status == "active" and k.binding is not None and \
                (k.binding.table, k.binding.key) != (binding["table"], binding["key"]):
            return o, k, binding, [], [active_message(k.iri)]
        out = binding_faults(o, k.iri, binding, k.implements, k.subclass_of)
        if p.move and p.migration != "move":
            out.append("move needs migration move")
        if out:
            return o, k, binding, [], out
        sql, out = schema_changes(ctx, o, k, binding, p.migration, p.move)
        return o, k, binding, sql, out

    def preconditions(self, ctx, p):
        return self._compute(ctx, p)[4]

    def statements(self, ctx, p):
        return self._compute(ctx, p)[3]

    def edits(self, ctx, p):
        _, k, binding, _, _ = self._compute(ctx, p)

        def change(fm):
            fm["binding"] = binding
        page = edit_page(ctx, k.rel, change) if binding_dict(k) != binding else None
        line = log_line(ctx, self, f"bound {k.iri} to table {binding['table']}")
        return [e for e in (page, line) if e is not None], [], []


# --- PromoteClass, DeprecateClass ---

class ClassParams(BaseModel):
    class_iri: str


@register
class PromoteClass(ActionType[ClassParams]):
    """experimental to active. An active class keeps its IRI, key and table from then on."""

    name = "PromoteClass"
    version = 1
    params = ClassParams

    def preconditions(self, ctx, p):
        _, _, k = find_class(ctx, p.class_iri)
        return [] if k.status == "experimental" else [f"{k.iri} is {k.status}; only an experimental class is promoted"]

    def edits(self, ctx, p):
        _, _, k = find_class(ctx, p.class_iri)
        return [edit_page(ctx, k.rel, lambda fm: fm.update(status="active")),
                log_line(ctx, self, f"promoted {k.iri} to active")], [], []


class DeprecateClassParams(BaseModel):
    class_iri: str
    reason: str
    deadline: date
    replacement: str


@register
class DeprecateClass(ActionType[DeprecateClassParams]):
    """The class is deprecated, with a reason, a deadline and the class that replaces it."""

    name = "DeprecateClass"
    version = 1
    params = DeprecateClassParams

    def preconditions(self, ctx, p):
        _, o, k = find_class(ctx, p.class_iri)
        out = [f"{k.iri} is already deprecated"] if k.status == "deprecated" else []
        if not p.reason.strip():
            out.append("give the reason for the deprecation")
        if p.replacement == k.iri or p.replacement not in o.classes:
            out.append(f"replacement {p.replacement} is not another Class in this bundle")
        return out

    def edits(self, ctx, p):
        _, _, k = find_class(ctx, p.class_iri)

        def change(fm):
            fm["status"] = "deprecated"
            fm["deprecated"] = {"reason": p.reason, "deadline": p.deadline.isoformat(), "replacement": p.replacement}
        line = f"deprecated {k.iri} for {p.replacement} by {p.deadline.isoformat()}: {p.reason}"
        return [edit_page(ctx, k.rel, change), log_line(ctx, self, line)], [], []


# --- SeedOntology ---

class SeedParams(BaseModel):
    spec: dict  # the seed spec itself, so the log row holds what was seeded


@register
class SeedOntology(ActionType[SeedParams]):
    """The seeder's page writes as one action. Nothing to change gives an action with no page edits.

    Highlights for the quotes go into annotated/*.pdf, which are not pages, so after_commit writes
    them. Every way of submitting the action (okf-seed, MCP, the agent toolset) gets them."""

    name = "SeedOntology"
    version = 1
    params = SeedParams

    def edits(self, ctx, p):
        domain = p.spec.get("domain")
        if not isinstance(domain, str) or not SLUG.fullmatch(domain):
            raise Failed([f"the seed spec is malformed: domain {domain!r} is not a slug"])
        report = Report(S.TOOL)
        try:
            s = S.compute(ctx.root, domain, p.spec, report, S.stamp(ctx.now))
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            raise Failed([f"the seed spec is malformed: {type(e).__name__}: {e}"])
        if report.errors or s is None:
            raise Failed([f"{f.rule} {f.path}: {f.message}" for f in report.findings if f.level == "error"])
        pages = [PageEdit(rel, ctx.read(rel), text) for rel, text in sorted(s.pages.items())]
        pages = [e for e in pages if e.before != e.after]
        if blocked := [m for e in pages if (m := active_guard(e.before, e.after))]:
            raise Failed(blocked)
        if pages:
            pages.append(log_line(ctx, self, f"seeded the {domain} ontology, {len(s.written)} pages written"))
        return pages, [], []

    def after_commit(self, ctx, p):
        """Highlight each quote in annotated/<arxiv-id>.pdf. Quotes already highlighted are skipped,
        so a rerun only finishes what an earlier run left."""
        s = S.compute(ctx.root, p.spec["domain"], p.spec, Report(S.TOOL), S.stamp(ctx.now))
        if s is not None:
            S.apply_marks(ctx.root, s.marks)


# --- RevertAction ---

class RevertParams(BaseModel):
    action_id: str


@register
class RevertAction(ActionType[RevertParams]):
    """Undo an earlier action: its pages get their before text back, its row edits run backwards.

    Only the actor who made it may revert it, and only while no later action edited one of its pages
    or rows. A later action that was reverted does not count, nor does the revert that undid it.
    A revert never deletes an active class, takes its status away or changes its iri, key or table.
    log.md is the exception to restoring: it only grows, so the revert adds its own line and keeps
    the earlier ones."""

    name = "RevertAction"
    version = 1
    params = RevertParams

    def _row(self, ctx, aid: str):
        return ctx.lake.con.execute('SELECT action_type, actor, "at", page_edits, row_edits, reverted_by '
                                    "FROM kdb.action_log WHERE action_id = ?", [aid]).fetchone()

    def preconditions(self, ctx, p):
        row = self._row(ctx, p.action_id)
        if row is None:
            return [f"no logged action has the id {p.action_id}"]
        kind, actor, at, pages, rows, reverted_by = row
        cls = REGISTRY.get(kind)
        if cls is not None and cls.adopts_existing:
            return [f"{p.action_id} is an {kind}, which cannot be reverted: it took in pages that existed before it, "
                    "so there is no earlier text to restore"]
        if kind == "BindTable":
            return [f"{p.action_id} is a BindTable, which changed the lake schema, and a revert does not undo that; "
                    "run BindTable again with the binding you want"]
        if kind == self.name:
            return [f"{p.action_id} is itself a revert; run the action it reverted again instead"]
        if reverted_by is not None:
            return [f"{p.action_id} is already reverted by {reverted_by}"]
        if actor != ctx.actor:
            return [f"{p.action_id} was made by {actor}; only {actor} can revert it"]
        mine_pages = {e["path"] for e in _json(pages) if e["path"] != LOG}
        mine_rows = {(r["table"], r["iri"]) for r in _json(rows)}
        out = []
        later = ctx.lake.con.execute(
            'SELECT action_id, action_type, page_edits, row_edits, reverts, reverted_by FROM kdb.action_log '
            'WHERE "at" > ? OR ("at" = ? AND action_id > ?) ORDER BY "at", action_id', [at, at, p.action_id]).fetchall()
        ids = {row[0] for row in later}
        for lid, lkind, lpages, lrows, lreverts, lreverted_by in later:
            if lreverted_by is not None or lreverts in ids:
                continue  # undone, or the revert of a later action: the pair cancels out
            shared = sorted(mine_pages & {e["path"] for e in _json(lpages)})
            theirs = {(r["table"], r["iri"]) for r in _json(lrows)}
            shared += [f"row {iri} of {table}" for table, iri in sorted(mine_rows & theirs)]
            if shared:
                out.append(f"the later action {lid} ({lkind}) also edited {', '.join(shared)}; revert it first")
        if out:
            return out  # the later edits explain any change on disk
        for e in _json(pages):
            data = ctx.path(e["path"]).read_bytes() if ctx.path(e["path"]).exists() else None
            sha = None if data is None else hashlib.sha256(data).hexdigest()
            if e["path"] != LOG and sha != e["after_sha256"]:
                out.append(f"{e['path']} changed since {p.action_id} wrote it")
            elif e["path"] != LOG and (m := active_guard(ctx.read(e["path"]), e["before_text"])):
                out.append(m)  # design 7.5: an active class is deprecated, never deleted, demoted or renamed
        return out

    def edits(self, ctx, p):
        kind, _, _, pages, rows, _ = self._row(ctx, p.action_id)
        edits = [PageEdit(e["path"], ctx.read(e["path"]), e["before_text"]) for e in _json(pages) if e["path"] != LOG]
        edits.append(log_line(ctx, self, f"reverted {p.action_id} ({kind})"))
        back = {"insert": "delete", "delete": "insert", "update": "update"}
        undo = [RowEdit(r["table"], r["iri"], back[r["op"]], r["after"], r["before"])
                for r in reversed(load_row_edits(rows))]
        return edits, undo, []

    def reverts(self, ctx, p):
        return p.action_id


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or [])
