"""The checks as tool functions an agent can call, with results it can act on.

They depend on no agent framework. kdb_tools.agent wraps them as a Pydantic AI toolset and
kdb_tools.mcp serves them over MCP, so every agent sees the same tools and the same answers.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pdfplumber

from . import bundle as B
from . import ground as G
from . import onto as O
from . import sheet as S
from . import validate as V
from .actions import REGISTRY, Context, Failed, Plan  # importing the package registers every action
from .actions.objects import _ident
from .actions.types import table_columns
from .locate import find_quote

DOCUMENT_DIRS = ("papers", "documents")  # where a bundle keeps its source files
NEEDS_A_PERSON = "this action touches an active class; a person must submit it"
MAX_ROWS = 1000


class ToolInputError(ValueError):
    """A call the agent should correct; the message says how."""


@dataclass
class Match:
    found: bool
    boxes: list[list[float]] = field(default_factory=list)  # [x0, y0, x1, y1] in PDF points from the bottom left
    note: str = ""
    cells: list[str] = field(default_factory=list)  # spreadsheet cells, such as "DH!B4"


@dataclass
class Problem:
    rule: str
    level: str
    path: str
    message: str
    source_id: str | None = None


@dataclass
class Checked:
    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)


class KdbTools:
    """The tools for one kDB bundle."""

    def __init__(self, root: Path, profiles=None, ctx: Context | None = None):
        self.root = Path(root)
        self.profiles = profiles
        self.ctx = ctx  # who acts on which lake; None offers no lake tools

    def documents(self, suffixes: tuple[str, ...] = (".pdf",)) -> dict[str, Path]:
        """The bundle's source files with these suffixes, by id (the file name without its suffix)."""
        return {p.stem: p for d in DOCUMENT_DIRS for p in sorted((self.root / d).glob("*"))
                if p.is_file() and p.suffix.lower() in suffixes}

    def papers(self) -> list[str]:
        return sorted(self.documents())

    def _document(self, document: str, suffixes: tuple[str, ...], kind: str) -> Path:
        found = self.documents(suffixes)
        if document not in found:
            raise ToolInputError(f"there is no {kind} {document!r}; the {kind}s are: {', '.join(sorted(found)) or 'none'}")
        return found[document]

    def _locate(self, document: str, page: int, text: str, what: str) -> Match:
        pdf = self._document(document, (".pdf",), "PDF")
        with pdfplumber.open(pdf) as doc:
            if not 1 <= page <= len(doc.pages):
                raise ToolInputError(f"{document} has pages 1 to {len(doc.pages)}; there is no page {page}")
            boxes = find_quote(doc.pages[page - 1], text)
            if boxes:
                return Match(True, boxes)
            elsewhere = [n for n in range(1, len(doc.pages) + 1) if n != page and find_quote(doc.pages[n - 1], text)]
        if elsewhere:
            return Match(False, note=f"the {what} is not on page {page}; it is on page {', '.join(map(str, elsewhere))}")
        return Match(False, note=f"the {what} is not in {document}; copy it word for word from the page")

    def find_quote(self, document: str, page: int, text: str) -> Match:
        """Where a sentence is on a page of a PDF, as the checks will look for it."""
        return self._locate(document, page, text, "sentence")

    def find_value(self, document: str, page: int, value: str) -> Match:
        """Where a table value, such as 76.5, is on a page of a PDF."""
        return self._locate(document, page, value, "value")

    def find_cell(self, document: str, value: str, sheet: str | None = None) -> Match:
        """Every cell of a spreadsheet that holds the value, as the checks will read it."""
        path = self._document(document, S.SUFFIXES, "spreadsheet")
        try:
            cells = S.Workbook(path).find(value, sheet)
        except S.SheetError as exc:
            raise ToolInputError(str(exc)) from exc
        if cells:
            return Match(True, cells=cells)
        return Match(False, note=f"no cell in {document}{' sheet ' + sheet if sheet else ''} holds {value!r}")

    def check_pages(self, paths: list[str]) -> Checked:
        """okf-validate and okf-ground on the given pages only."""
        out = Checked()
        lake, store = (self.ctx.lake, self.ctx.store) if self.ctx else (None, None)
        for report in (V.validate(self.root, self.profiles, pages=paths, lake=lake),
                       G.ground(self.root, pages=paths, lake=lake, store=store)):
            for f in report.findings:
                (out.errors if f.level == "error" else out.warnings).append(Problem(f.rule, f.level, f.path, f.message, f.source_id))
        return out

    # --- the type layer and the lake: reads, then actions (design 7.4) ---

    def _context(self) -> Context:
        if self.ctx is None:
            raise ToolInputError("this kDB has no lake, so there are no classes, objects or actions")
        return self.ctx

    def _ontology(self) -> O.Ontology:
        return O.load_ontology(B.load(self.root))

    def _class(self, o: O.Ontology, iri: str) -> O.Klass:
        if iri not in o.classes:
            known = sorted(o.classes)
            raise ToolInputError(f"there is no class {iri!r}; the classes are: "
                                 f"{', '.join(known[:5]) or 'none'}{' and more' if len(known) > 5 else ''}")
        return o.classes[iri]

    def get_class(self, iri: str) -> dict:
        """One class: its status, supertypes, interfaces, binding, constraints and properties."""
        self._context()
        o = self._ontology()
        k = self._class(o, iri)
        props = {col: asdict(p) for col, p in O.property_columns(o, k).items()}
        return _plain({**asdict(k), "properties": props})

    def list_classes(self) -> list[dict]:
        """Every class with its status and bound table."""
        self._context()
        return [{"iri": k.iri, "status": k.status, "table": k.binding.table if k.binding else None}
                for k in sorted(self._ontology().classes.values(), key=lambda k: k.iri)]

    def _rows(self, k: O.Klass, where: dict[str, str], limit: int | None, iri: str | None = None) -> list[dict]:
        ctx = self._context()
        if k.binding is None:
            raise ToolInputError(f"{k.iri} has no table bound, so it has no objects")
        types = table_columns(ctx, k.binding.table)
        if types is None:
            raise ToolInputError(f"the table {k.binding.table} of {k.iri} does not exist yet")
        unknown = [c for c in where if c not in types]
        if unknown:
            raise ToolInputError(f"{', '.join(unknown)} is not a column of {k.binding.table}; the columns are: {', '.join(types)}")
        clauses, args = ["type = ?"], [k.iri]
        if iri is not None:
            clauses.append("iri = ?")
            args.append(iri)
        for col, value in where.items():
            clauses.append(f"CAST({_ident(col)} AS VARCHAR) = ?")
            args.append(str(value))
        sql = f"SELECT * FROM {_ident(k.binding.table)} WHERE {' AND '.join(clauses)} ORDER BY iri"
        if limit is not None:
            sql += f" LIMIT {limit}"
        cur = ctx.lake.con.execute(sql, args)
        names = [d[0] for d in cur.description]
        return [{c: _plain(v, types[c]) for c, v in zip(names, row)} for row in cur.fetchall()]

    def query_objects(self, class_iri: str, where: dict[str, str] | None = None, limit: int = 50) -> list[dict]:
        """The rows of a class's table whose columns equal the given values (compared as text), by iri."""
        self._context()
        if not 1 <= limit <= MAX_ROWS:
            raise ToolInputError(f"limit is between 1 and {MAX_ROWS}, not {limit}")
        return self._rows(self._class(self._ontology(), class_iri), where or {}, limit)

    def get_object(self, iri: str) -> dict | None:
        """The row with this iri from any class's bound table, or None."""
        self._context()
        o = self._ontology()
        for k in sorted(o.classes.values(), key=lambda k: k.iri):
            if k.binding is None or table_columns(self.ctx, k.binding.table) is None:
                continue
            found = self.ctx.lake.con.execute(f"SELECT type FROM {_ident(k.binding.table)} WHERE iri = ?", [iri]).fetchone()
            if found and found[0] in o.classes and o.classes[found[0]].binding:
                rows = self._rows(o.classes[found[0]], {}, 1, iri)
                if rows:
                    return rows[0]
        return None

    def actions(self) -> list[dict]:
        """Every action type with its version and the JSON schema of its parameters."""
        return [{"name": n, "version": a.version, "params": a.params.model_json_schema()}
                for n, a in sorted(REGISTRY.items())]

    def _action(self, name: str):
        if name not in REGISTRY:
            raise ToolInputError(f"there is no action {name!r}; the actions are: {', '.join(sorted(REGISTRY))}")
        return REGISTRY[name]()

    def dry_run(self, action: str, params: dict) -> dict:
        """The plan an action would carry out, changing nothing. Failed and Conflict carry every message."""
        return _plain(asdict(self._action(action).dry_run(self._context(), params)))

    def touches_active(self, plan: Plan) -> bool:
        """Whether the plan edits a Class page that is active before or after the edit, or creates a Class
        page that is active. Rows are instance data."""
        pages = {k.rel: k for k in self._ontology().classes.values()}
        for e in plan.page_edits:
            after = _frontmatter(e.after)
            if (k := pages.get(e.path)) is not None and "active" in (k.status, after.get("status")):
                return True
            if k is None and after.get("type") == "Class" and after.get("status") == "active":
                return True
        return False

    def submit(self, action: str, params: dict) -> dict:
        """Apply an action. An agent's action that edits an active Class page is refused: a person submits it.
        An agent registers files inside the kDB only."""
        ctx, act = self._context(), self._action(action)
        if ctx.model is not None and action == "RegisterMedia":
            _inside(ctx.root, params.get("path") if isinstance(params, dict) else None)
        plan = act.dry_run(ctx, params)
        if ctx.model is not None and self.touches_active(plan):
            raise Failed([NEEDS_A_PERSON])
        return {"action_id": act.submit(ctx, params, plan)}


def _frontmatter(text: str | None) -> dict:
    """A page's frontmatter, or {} when there is no page or no readable frontmatter."""
    try:
        fm = B.split_frontmatter(text)[0] if text is not None else None
    except B.FrontmatterError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _inside(root: Path, path) -> None:
    """Refuse a RegisterMedia path that resolves outside the kDB. A path that is not a string, or that
    cannot be resolved, is left to the action, which refuses it."""
    if not isinstance(path, str):
        return
    try:
        target = (Path(path) if Path(path).is_absolute() else root / path).resolve()
        outside = not target.is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return
    if outside:
        raise Failed([f"an agent registers files inside the kDB only; {path} is outside {root}"])


def _plain(value, duck_type: str = ""):
    """A value as JSON holds it: dates as ISO strings, a JSON column parsed, lists and dicts kept."""
    if value is None or isinstance(value, (bool, int, float, str)):
        if duck_type == "JSON" and isinstance(value, str):
            return json.loads(value)
        return value
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)
