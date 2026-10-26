"""The instance actions: media files, and the objects (papers, results, datasets) in bound tables.

The kDB holds types; instances are lake rows. RegisterMedia adds a file to kdb.media and puts its
bytes in the object store only after the action commits, so a failed action leaves no orphan
(design 6.1). UpsertObjects inserts or updates rows of a class's bound table and DeleteObjects
removes them. Every row's IRI starts with the lake's iri_base, which names the team, so one team
never writes another's rows. Values are stored in their declared column types and logged so that a
revert gets the same Python values back. None of them writes a page, and one that changes nothing
logs nothing.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .. import media as M
from .. import onto as O
from ..lake import object_key
from .base import ActionType, Failed, RowEdit, _ident, register
from .types import find_class, table_columns

MEDIA = "kdb.media"


# --- RegisterMedia ---

class RegisterMediaParams(BaseModel):
    path: str  # absolute, or relative to the bundle root
    origin: str  # where the file came from: a URL, an upload, a run
    annotates: str | None = None  # the sha256 of the original, for an annotated copy


@register
class RegisterMedia(ActionType[RegisterMediaParams]):
    """One file in kdb.media. Registering the same bytes again changes and logs nothing, but still
    puts the bytes, so a rerun repairs a put that failed after the commit."""

    name = "RegisterMedia"
    version = 1
    params = RegisterMediaParams
    logs_no_op = False

    def _read(self, ctx, p) -> tuple[bytes, str]:
        path = Path(p.path) if Path(p.path).is_absolute() else ctx.root / p.path
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise Failed([f"{p.path} is not a file"])
        except OSError as e:
            raise Failed([f"{p.path} could not be read: {e.strerror or e}"])
        except ValueError:  # such as an embedded NUL byte
            raise Failed([f"{p.path!r} is not a usable path"])
        return data, M.sha256_of(data)

    def preconditions(self, ctx, p):
        _, sha = self._read(ctx, p)
        out = [] if p.origin.strip() else ["give the origin of the file: a URL, an upload or a run"]
        if p.annotates is not None and M.lookup(ctx.lake, p.annotates) is None:
            out.append(f"annotates {p.annotates} is not a registered sha256; register the original first")
        old = M.lookup(ctx.lake, sha)
        if old is not None and p.annotates is not None and old["annotates"] != p.annotates:
            out.append(f"{sha} is already registered with annotates {old['annotates']}")
        return out

    def edits(self, ctx, p):
        data, sha = self._read(ctx, p)
        if M.lookup(ctx.lake, sha) is not None:
            return [], [], []
        name = Path(p.path).name
        row = {"media_type": M.media_type(name), "bytes": len(data), "object_key": object_key(sha), "name": name,
               "origin": p.origin, "annotates": p.annotates, "team": ctx.team, "registered_by": ctx.actor}
        return [], [RowEdit(MEDIA, sha, "insert", None, row)], [p.origin]

    def after_commit(self, ctx, p):
        data, sha = self._read(ctx, p)
        if M.lookup(ctx.lake, sha) is None:
            raise Failed([f"{p.path} changed after it was registered; register it again"])
        ctx.store.put(sha, data)


# --- values in their declared types ---

def canonical(value) -> str:
    """The JSON text a JSON column holds: one spelling per value, so equal values compare equal."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _strings(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def coerce(datatype: str, v):
    """The value in the Python type its column stores, and None; or None and what is wrong with it."""
    if v is None:
        return None, None
    if datatype in ("string", "iri"):
        return (v, None) if isinstance(v, str) else (None, "a string")
    if datatype == "integer":
        ok = isinstance(v, int) and not isinstance(v, bool) and -2 ** 63 <= v < 2 ** 63
        return (v, None) if ok else (None, "an integer that fits in 64 bits")
    if datatype == "double":
        try:
            f = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        except OverflowError:
            f = None
        return (f, None) if f is not None and math.isfinite(f) else (None, "a finite number")
    if datatype == "boolean":
        return (v, None) if isinstance(v, bool) else (None, "true or false")
    if datatype == "date":
        if isinstance(v, date) and not isinstance(v, datetime):
            return v, None
        try:
            return date.fromisoformat(v), None
        except (TypeError, ValueError):
            return None, "a date such as 2026-10-25"
    if datatype == "timestamp":
        try:
            t = v if isinstance(v, datetime) else datetime.fromisoformat(v)
        except (TypeError, ValueError):
            t = None
        if t is None or t.tzinfo is None:
            return None, "a time with a time zone, such as 2026-10-25T12:00:00Z"
        return t.astimezone(timezone.utc), None
    if datatype in ("iri_list", "string_list"):
        return (list(v), None) if _strings(v) else (None, "a list of strings")
    if datatype == "media_ref_list":
        if not isinstance(v, list):
            return None, "a list of MediaRefs"
        try:
            refs = [M.MediaRef.model_validate(x).model_dump(exclude_none=True) for x in v]
            return canonical(refs), None
        except ValueError as e:
            return None, f"a list of MediaRefs ({M.first_error(e)})"
    if datatype == "json":
        try:
            return canonical(v), None
        except (TypeError, ValueError):
            return None, "a JSON value"
    return None, f"of a datatype ({datatype!r}) that has no column type"


def _normal(duck_type: str, v):
    """A value as read from the lake, in the form coerce gives: UTC times and canonical JSON text."""
    if v is None:
        return None
    if duck_type == "JSON":
        return canonical(json.loads(v))
    if duck_type == "TIMESTAMPTZ":
        return v.astimezone(timezone.utc)
    return v


def read_row(ctx, table: str, iri: str) -> dict | None:
    """A bound table's row by IRI, without the iri column, values normalized; None when absent."""
    types = table_columns(ctx, table) or {}
    cur = ctx.lake.con.execute(f"SELECT * FROM {_ident(table)} WHERE iri = ?", [iri])
    names = [d[0] for d in cur.description]
    found = cur.fetchone()
    if found is None:
        return None
    return {c: _normal(types.get(c, ""), v) for c, v in zip(names, found) if c != "iri"}


def foreign(ctx, iri: str) -> str | None:
    """Why an IRI is not this team's to write, or None."""
    if iri.startswith(ctx.iri_base):
        return None
    return f"{iri} is not under this team's iri_base {ctx.iri_base} (team {ctx.team})"


def bound_table(ctx, class_iri: str):
    """The bundle, ontology, class and its existing table; Failed when there is none."""
    b, o, k = find_class(ctx, class_iri)
    if k.binding is None:
        raise Failed([f"{class_iri} has no binding; bind a table with BindTable first"])
    if table_columns(ctx, k.binding.table) is None:
        raise Failed([f"table {k.binding.table} does not exist; bind it with BindTable first"])
    return b, o, k


def required_properties(o: O.Ontology, k: O.Klass) -> set[str]:
    """The property IRIs a row must give: required on their page, or by an interface of the class."""
    out = {p.iri for p in o.props.values() if p.required}
    for c in [k] + O.supers(o, k):
        for i_iri in c.implements:
            iface = o.interfaces.get(i_iri)
            out |= {p for p, required in (iface.properties if iface else []) if required}
    return out


# --- UpsertObjects ---

class UpsertParams(BaseModel):
    class_iri: str
    rows: list[dict]  # iri plus columns. On an update a column left out keeps its value and null clears it


@register
class UpsertObjects(ActionType[UpsertParams]):
    """Insert new rows of a class's table and update changed ones; an unchanged row is no edit.

    An update is a patch: a column left out keeps its stored value, an explicit null clears it, and
    the required properties are checked on the merged row. An insert gives every required property."""

    name = "UpsertObjects"
    version = 1
    params = UpsertParams
    logs_no_op = False

    def _compute(self, ctx, p) -> tuple[list[RowEdit], list[str], list[str]]:
        """The row edits, the sha256s cited, and the messages that block the action."""
        b, o, k = bound_table(ctx, p.class_iri)
        table, columns = k.binding.table, k.binding.columns
        required = required_properties(o, k)
        known = set(o.terms) | set(o.classes) | set(o.interfaces) | set(o.props)
        bases = [x for x in O._bases(b).values() if x]
        registered: dict[str, dict | None] = {}
        edits, cited, out, seen = [], set(), [], set()
        for i, raw in enumerate(p.rows):
            iri = raw.get("iri")
            if not isinstance(iri, str) or not iri:
                out.append(f"row {i}: needs an iri")
                continue
            msgs = []
            if iri in seen:
                msgs.append("given twice")
            seen.add(iri)
            if foreign(ctx, iri):
                msgs.append(f"the IRI is not under this team's iri_base {ctx.iri_base} (team {ctx.team})")
            for key, want in (("type", p.class_iri), ("team", ctx.team)):
                if key in raw and raw[key] != want:
                    msgs.append(f"{key} is {raw[key]!r}, not {want!r}; it is set for you")
            msgs += [f"{c} is not a column of {table}" for c in raw if c not in columns and c not in O.IMPLICIT_COLUMNS]
            old = None if msgs else read_row(ctx, table, iri)
            if old is not None and old.get("team") != ctx.team:
                msgs.append(f"the row belongs to team {old.get('team')}")
            values = {"type": p.class_iri, "team": ctx.team}
            for col, p_iri in columns.items():
                v = raw[col] if col in raw else (old or {}).get(col)
                if v is None:
                    if p_iri in required:
                        msgs.append(f"required property {p_iri} (column {col}) has no value")
                    values[col] = None
                    continue
                if col not in raw:
                    values[col] = v  # kept as stored, and checked when it was written
                    continue
                prop = o.props.get(p_iri)
                value, wrong = coerce(prop.datatype if prop else "", v)
                if wrong:
                    msgs.append(f"{col} is {v!r}, which is not {wrong}")
                    continue
                values[col] = value
                datatype = prop.datatype
                if datatype in ("iri", "iri_list"):
                    for x in [value] if datatype == "iri" else value:
                        if any(x.startswith(base) for base in bases) and x not in known:
                            msgs.append(f"{col} names {x}, which is not a Term in this bundle")
                if datatype == "media_ref_list":
                    for n, ref in enumerate(M.MediaRef.model_validate(x) for x in json.loads(value)):
                        if ref.sha256 not in registered:
                            registered[ref.sha256] = M.lookup(ctx.lake, ref.sha256)
                        media = registered[ref.sha256]
                        if media is None:
                            msgs.append(f"{col} cites {ref.sha256}, which is not registered in kdb.media; "
                                        "register the file with RegisterMedia first")
                        elif why := M.unchecked(ref, media):
                            msgs.append(f"{col}[{n}] cannot be grounded: {why}")
                        cited.add(ref.sha256)
            if msgs:
                out += [f"row {iri}: {m}" for m in msgs]
            elif old is None:
                edits.append(RowEdit(table, iri, "insert", None, values))
            elif old != values:
                edits.append(RowEdit(table, iri, "update", old, values))
        return edits, sorted(cited), out

    def preconditions(self, ctx, p):
        return self._compute(ctx, p)[2]

    def edits(self, ctx, p):
        edits, cited, _ = self._compute(ctx, p)
        return [], edits, cited


# --- DeleteObjects ---

class DeleteParams(BaseModel):
    class_iri: str
    iris: list[str]
    reason: str


@register
class DeleteObjects(ActionType[DeleteParams]):
    """Remove rows of a class's table, by IRI, with a reason. A revert puts them back."""

    name = "DeleteObjects"
    version = 1
    params = DeleteParams
    logs_no_op = False

    def preconditions(self, ctx, p):
        _, _, k = bound_table(ctx, p.class_iri)
        out = [] if p.reason.strip() else ["give the reason for the deletion"]
        for i, iri in enumerate(p.iris):
            if iri in p.iris[:i]:
                out.append(f"{iri} is given twice")
            elif m := foreign(ctx, iri):
                out.append(m)
            elif (old := read_row(ctx, k.binding.table, iri)) is None:
                out.append(f"{iri} is not a row of {k.binding.table}")
            elif old.get("team") != ctx.team:
                out.append(f"{iri} belongs to team {old.get('team')}")
        return out

    def edits(self, ctx, p):
        _, _, k = bound_table(ctx, p.class_iri)
        table = k.binding.table
        return [], [RowEdit(table, iri, "delete", read_row(ctx, table, iri), None) for iri in p.iris], []
